"""Durable order intents and cumulative broker fills. No assumed live fills.

One engine writer is required (enforced by the systemd engine service).
The caller serializes entry, exit and reconciliation using an RLock.
"""
from __future__ import annotations

import copy
import math
import uuid
from datetime import datetime

from . import angel_orders, audit, storage, alerts
from .utils import now_ist

TERMINAL = {'complete', 'completed', 'filled', 'cancelled', 'canceled', 'rejected'}


def connection():
    c = storage.conn()
    c.execute("PRAGMA synchronous=FULL")
    c.executescript('''
    CREATE TABLE IF NOT EXISTS execution_orders (
        intent TEXT PRIMARY KEY, signal_id INTEGER NOT NULL, side TEXT NOT NULL,
        quantity INTEGER NOT NULL, order_id TEXT, state TEXT NOT NULL,
        filled INTEGER NOT NULL DEFAULT 0, notional REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0);
    CREATE UNIQUE INDEX IF NOT EXISTS execution_one_pending
        ON execution_orders(signal_id) WHERE state != 'terminal';
    CREATE TABLE IF NOT EXISTS execution_fills (
        intent TEXT NOT NULL, cumulative_qty INTEGER NOT NULL,
        quantity INTEGER NOT NULL, price REAL NOT NULL, pnl REAL NOT NULL,
        ts TEXT NOT NULL, dry_run INTEGER NOT NULL,
        PRIMARY KEY(intent, cumulative_qty));
    ''')
    return c


def pending(signal_id=None):
    c = connection()
    sql = "SELECT * FROM execution_orders WHERE state != 'terminal'"
    args = ()
    if signal_id is not None:
        sql += ' AND signal_id=?'
        args = (signal_id,)
    return [dict(r) for r in c.execute(sql, args)]


def seen(signal_id):
    return connection().execute('SELECT 1 FROM execution_orders WHERE signal_id=?',
                                (signal_id,)).fetchone() is not None


def request_entry_exit(pos, reason):
    """Latch an exit before cancelling a BUY; never sell an unknown final quantity.

    Cancellation acceptance is not a fill/cancel confirmation. The monitor keeps
    refreshing the order book and exits the final owned quantity once terminal.
    exit_reason is persisted with the position and survives restart and rebound.
    """
    rows = pending(pos.signal_id)
    if len(rows) != 1 or rows[0]['side'] != 'BUY':
        return {'ok': False, 'reason': 'entry_order_unresolved', 'pending': True}
    pos.exit_reason = pos.exit_reason or reason
    storage.upsert_position(pos)
    order_id = rows[0]['order_id']
    if order_id:
        angel_orders.cancel_order(order_id)
    audit.log('entry_exit_queued', signal_id=pos.signal_id, order_id=order_id,
              owned_qty=pos.remaining_qty, reason=pos.exit_reason)
    alerts.enqueue(f"Exit queued for {pos.symbol}; waiting for entry cancellation "
                   f"and final filled quantity. Check broker if delayed.",
                   key=f"entry-exit:{pos.signal_id}")
    return {'ok': True, 'reason': 'exit_queued_awaiting_entry_terminal',
            'pending': True, 'order_id': order_id}


def submit(pos, side, quantity, price):
    """Persist before sending; ambiguous responses retain a blocking intent."""
    c = connection()
    if pending(pos.signal_id):
        return {'ok': False, 'reason': 'order_already_pending'}
    intent = 'K' + uuid.uuid4().hex[:15]
    previous = pos.status
    pos.status = 'entry_pending' if side == 'BUY' else 'exit_pending'
    with c:
        c.execute('INSERT INTO execution_orders '
                  '(intent,signal_id,side,quantity,state,created_at) VALUES (?,?,?,?,?,?)',
                  (intent, pos.signal_id, side, quantity, 'submitting', now_ist().isoformat()))
        storage.upsert_position(pos, _connection=c)
    try:
        result = angel_orders.place_limit_order(tradingsymbol=pos.symbol,
            symboltoken=pos.token, exchange=pos.exchange, transactiontype=side,
            quantity=quantity, price=price, ordertag=intent, signal_id=pos.signal_id,
            producttype=angel_orders.product_for(pos))
    except Exception:
        # Includes unexpected wrapper errors after an HTTP request may have left.
        audit.log('order_submission_unknown', signal_id=pos.signal_id, intent=intent)
        return {'ok': False, 'reason': 'submission_unknown', 'pending': True}
    if not result.ok and not result.uncertain:
        pos.status = 'closed' if side == 'BUY' else previous
        with c:
            c.execute("UPDATE execution_orders SET state='terminal' WHERE intent=?", (intent,))
            storage.upsert_position(pos, _connection=c)
        return {'ok': False, 'reason': result.reason}
    if side == 'BUY':
        pos.order_id = result.order_id
    else:
        pos.exit_order_id = result.order_id
    with c:
        c.execute('UPDATE execution_orders SET state=?,order_id=?,dry_run=? WHERE intent=?',
                  ('pending' if result.ok else 'unknown', result.order_id,
                   int(result.dry_run), intent))
        storage.upsert_position(pos, _connection=c)
    if result.dry_run:
        row = dict(c.execute('SELECT * FROM execution_orders WHERE intent=?', (intent,)).fetchone())
        apply_fill(pos, row, angel_orders.OrderStatus(result.order_id, 'complete', quantity, price, {}))
    audit.log('execution_order_submitted', signal_id=pos.signal_id,
              intent=intent, side=side, order_id=result.order_id, pending=not result.dry_run)
    return {'ok': bool(result.ok), 'pending': not result.dry_run,
            'reason': 'awaiting_broker_fill' if result.ok else 'submission_unknown',
            'order_id': result.order_id}


def apply_fill(pos, row, status):
    """Commit position, incremental fills and cumulative cursor atomically."""
    c = connection()
    # Always reload cursor; duplicate broker snapshots/restarts are harmless.
    row = dict(c.execute('SELECT * FROM execution_orders WHERE intent=?', (row['intent'],)).fetchone())
    original = pos
    pos = copy.copy(pos)
    qty = status.filled_qty
    avg = status.avg_price or 0
    if (qty < row['filled'] or qty > row['quantity'] or
        not math.isfinite(avg) or (qty and avg <= 0)):
        raise ValueError('invalid or regressing broker fill')
    notional = qty * avg
    delta = qty - row['filled']
    amount = notional - row['notional']
    if delta == 0 and abs(amount) > 0.01:
        raise ValueError('broker revised previously accounted average price')
    if delta and amount <= 0:
        raise ValueError('invalid incremental fill notional')
    terminal = status.status in TERMINAL
    if status.status in {'complete', 'completed', 'filled'} and qty != row['quantity']:
        raise ValueError('complete order has inconsistent filled quantity')
    if row['side'] == 'SELL' and delta > pos.remaining_qty:
        raise ValueError('sell fill exceeds locally tracked quantity')
    pnl = 0.0
    if delta:
        if row['side'] == 'BUY':
            pos.entry_price = notional / qty
            pos.remaining_qty += delta
            pos.peak_price = max(pos.peak_price or 0, pos.entry_price)
            pos.order_id = status.order_id
        else:
            pos.remaining_qty -= delta
            pnl = amount - pos.entry_price * delta
            pos.exit_price = avg
            pos.exit_ts = now_ist()
            pos.exit_order_id = status.order_id
    if terminal:
        pos.status = 'open' if pos.remaining_qty > 0 else 'closed'
    else:
        pos.status = 'entry_pending' if row['side'] == 'BUY' else 'exit_pending'
    with c:
        if delta:
            c.execute('INSERT INTO execution_fills VALUES (?,?,?,?,?,?,?)',
                (row['intent'], qty, delta, amount/delta, pnl,
                 now_ist().isoformat(), row['dry_run']))
        c.execute('UPDATE execution_orders SET filled=?,notional=?,state=?,order_id=? WHERE intent=?',
            (qty, notional, 'terminal' if terminal else 'pending', status.order_id, row['intent']))
        if row['side'] == 'SELL' and qty:
            sold = c.execute("SELECT SUM(f.quantity*f.price),SUM(f.quantity) "
                "FROM execution_fills f JOIN execution_orders e ON e.intent=f.intent "
                "WHERE e.signal_id=? AND e.side='SELL'", (pos.signal_id,)).fetchone()
            if sold[1]:
                pos.exit_price = sold[0] / sold[1]
        storage.upsert_position(pos, _connection=c)
    original.__dict__.update(pos.__dict__)
    if delta:
        audit.log('execution_fill_confirmed', signal_id=pos.signal_id, side=row['side'],
                  quantity=delta, remaining_qty=pos.remaining_qty, price=amount/delta,
                  pnl_rupees=pnl, dry_run=bool(row['dry_run']))
        alerts.enqueue(f"{row['side']} fill {pos.symbol}: {delta} @ {amount/delta:.2f}; "
                       f"remaining {pos.remaining_qty} | signal #{pos.signal_id}",
                       key=f"fill:{row['intent']}:{qty}")


def refresh(positions, ltp_fn=None):
    """ltp_fn(exchange, token) -> fresh LTP or None; used to re-price exits."""
    rows = pending()
    if not rows:
        return True
    book = angel_orders.fetch_order_book()
    if book is None:
        return False
    healthy = True
    for row in rows:
        pos = positions.get(str(row['signal_id']))
        if pos is None:
            healthy = False
            continue
        matches = [s for s in book if (row['order_id'] and s.order_id == row['order_id'])
                   or (not row['order_id'] and s.raw.get('ordertag') == row['intent'])]
        if len(matches) != 1:
            healthy = False
            audit.log('execution_order_unresolved', signal_id=pos.signal_id, intent=row['intent'])
            continue
        status = matches[0]
        # Validate contract and side when supplied by the broker.
        if any(status.raw.get(k) is not None and str(status.raw[k]) != str(v)
               for k, v in [('symboltoken', pos.token), ('exchange', pos.exchange),
                            ('transactiontype', row['side']),
                            ('producttype', angel_orders.product_for(pos))]):
            healthy = False
            continue
        try:
            apply_fill(pos, row, status)
        except (ValueError, TypeError):
            audit.log('execution_fill_inconsistent', signal_id=pos.signal_id, intent=row['intent'])
            healthy = False
            continue
        # Bound entry exposure: cancel the unfilled remainder after 30 seconds.
        # Cancellation acknowledgement is not terminal; await the next order book.
        age = (now_ist() - datetime.fromisoformat(row['created_at'])).total_seconds()
        if row['side'] == 'BUY' and pos.status == 'entry_pending' and (age >= 30 or pos.remaining_qty > 0 or pos.exit_reason):
            angel_orders.cancel_order(status.order_id)
        if row['side'] == 'SELL' and pos.status == 'exit_pending':
            _reprice_exit(pos, row, status, age, ltp_fn)
        elif row['side'] == 'SELL':
            _REPRICE.pop(row['intent'], None)
    return healthy


# ------------------------------------------------------------ exit re-pricing
# 2026-09-29: an unfilled exit LIMIT used to sit forever (alert only) while the
# premium kept falling. Now it is walked down to fresh LTP minus a widening
# buffer, via modify (same order id, so fills keep reconciling normally).
REPRICE_FIRST_AFTER_SEC = 15
REPRICE_EVERY_SEC = 15
REPRICE_BUFFERS = (0.025, 0.04, 0.06)   # attempt 1, 2, 3+ (last one repeats)
_REPRICE: dict = {}                      # intent -> {'attempts', 'last', 'alerted'}


def _reprice_exit(pos, row, status, age, ltp_fn):
    state = _REPRICE.setdefault(row['intent'], dict(attempts=0, last=None, alerted=False))
    now = now_ist()
    due = (age >= REPRICE_FIRST_AFTER_SEC and
           (state['last'] is None or (now - state['last']).total_seconds() >= REPRICE_EVERY_SEC))
    if not due:
        return
    ltp = None
    if callable(ltp_fn):
        try:
            ltp = ltp_fn(pos.exchange, pos.token)
        except Exception:                           # noqa: BLE001
            ltp = None
    state['last'] = now
    if ltp is None or not math.isfinite(ltp) or ltp <= 0:
        audit.log('execution_exit_reprice_no_quote', signal_id=pos.signal_id,
                  order_id=status.order_id, age_seconds=round(age))
        alerts.enqueue(f"Exit still pending: {pos.symbol}, remaining {pos.remaining_qty}; "
                       f"no fresh quote to re-price. Check broker order {status.order_id}",
                       key=f"pending-exit:{status.order_id}")
        return
    buffer = REPRICE_BUFFERS[min(state['attempts'], len(REPRICE_BUFFERS) - 1)]
    new_price = max(0.05, round(ltp * (1 - buffer), 1))
    try:
        current = float(status.raw.get('price'))
    except (TypeError, ValueError):
        current = None
    if current is not None and math.isfinite(current) and new_price >= current:
        return      # existing limit is already at or below the re-priced level
    result = angel_orders.modify_order(status.order_id, price=new_price,
        quantity=row['quantity'], contract=dict(tradingsymbol=pos.symbol,
        symboltoken=pos.token, exchange=pos.exchange, producttype=angel_orders.product_for(pos)))
    state['attempts'] += 1
    audit.log('execution_exit_repriced', signal_id=pos.signal_id, order_id=status.order_id,
              attempt=state['attempts'], ltp=ltp, buffer_pct=buffer * 100,
              old_price=current, new_price=new_price, accepted=result.ok, reason=result.reason)
    if state['attempts'] >= len(REPRICE_BUFFERS) and not state['alerted']:
        state['alerted'] = True
        alerts.enqueue(f"\U0001F6A8 EXIT NOT FILLING: {pos.symbol} remaining {pos.remaining_qty} "
                       f"after {state['attempts']} re-prices (now {new_price}). "
                       f"Order {status.order_id} - check broker NOW.",
                       key=f"exit-not-filling:{status.order_id}", bypass=True)

