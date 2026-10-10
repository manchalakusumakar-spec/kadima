"""Durable order intents and cumulative broker fills. No assumed live fills.

One engine writer is required (enforced by the systemd engine service).
The caller serializes entry, exit and reconciliation using an RLock.
"""
from __future__ import annotations

import copy
import math
import uuid
from datetime import datetime

from . import angel_orders, audit, config, storage, alerts
from .utils import now_ist

TERMINAL = {'complete', 'completed', 'filled', 'cancelled', 'canceled', 'rejected'}

# 2026-10-10. Outcome notes on terminal rows:
#   delivery_failed  - the request never reached Angel One (rate limited, no
#                      session). Safe to re-send; bounded by seen() below.
#   unknown_timeout  - the outcome was ambiguous and the order was still not in
#                      Angel One's book after UNKNOWN_ORDER_TIMEOUT_SEC. Never
#                      re-sent, because it may yet appear; watched for late arrival.
#   expired_previous_day - an unresolved row from an earlier session (the day's
#                      order book no longer contains it).
DELIVERY_FAILED = 'delivery_failed'
UNKNOWN_TIMEOUT = 'unknown_timeout'
EXPIRED_PREVIOUS_DAY = 'expired_previous_day'
RETRYABLE_REASONS = {'rate_limited', 'session_unavailable'}
_LATE_ALERTED: set = set()


def _setting(name, default):
    value = getattr(config, name, default)
    return value if type(value) in (int, float) and value >= 0 else default


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
    columns = {r['name'] for r in c.execute('PRAGMA table_info(execution_orders)')}
    if 'price' not in columns:
        c.execute('ALTER TABLE execution_orders ADD COLUMN price REAL')
    if 'note' not in columns:
        c.execute('ALTER TABLE execution_orders ADD COLUMN note TEXT')
    return c


def pending(signal_id=None):
    c = connection()
    sql = "SELECT * FROM execution_orders WHERE state != 'terminal'"
    args = ()
    if signal_id is not None:
        sql += ' AND signal_id=?'
        args = (signal_id,)
    return [dict(r) for r in c.execute(sql, args)]


def seen(signal_id, now=None):
    """True once a signal must not be submitted again.

    A signal may be re-sent only while every earlier attempt was a delivery
    failure (never reached Angel One), fewer than ORDER_RETRY_ATTEMPTS have been
    made, and the first attempt is within ORDER_RETRY_WINDOW_SEC. Anything that
    reached the broker, or whose outcome was ambiguous, ends the signal."""
    rows = [dict(r) for r in connection().execute(
        'SELECT created_at, note FROM execution_orders WHERE signal_id=? ORDER BY created_at',
        (signal_id,))]
    if not rows:
        return False
    if any(r['note'] != DELIVERY_FAILED for r in rows):
        return True
    if len(rows) >= _setting('ORDER_RETRY_ATTEMPTS', 3):
        return True
    try:
        first = datetime.fromisoformat(rows[0]['created_at'])
        age = ((now or now_ist()) - first).total_seconds()
    except (TypeError, ValueError):
        return True
    return not 0 <= age <= _setting('ORDER_RETRY_WINDOW_SEC', 45)


def retry_price_ok(signal_id, price):
    """A re-send may not pay more than ORDER_RETRY_MAX_DRIFT_PCT above the first
    attempt's limit price. First attempts are always allowed."""
    row = connection().execute(
        'SELECT price FROM execution_orders WHERE signal_id=? AND price IS NOT NULL '
        'ORDER BY created_at LIMIT 1', (signal_id,)).fetchone()
    if row is None:
        return True
    try:
        first = float(row['price'])
        return math.isfinite(first) and first > 0 and \
            price <= first * (1 + _setting('ORDER_RETRY_MAX_DRIFT_PCT', 1.0) / 100.0) + 1e-9
    except (TypeError, ValueError):
        return False


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
                  '(intent,signal_id,side,quantity,state,created_at,price) VALUES (?,?,?,?,?,?,?)',
                  (intent, pos.signal_id, side, quantity, 'submitting', now_ist().isoformat(), price))
        storage.upsert_position(pos, _connection=c)
    try:
        result = angel_orders.place_limit_order(tradingsymbol=pos.symbol,
            symboltoken=pos.token, exchange=pos.exchange, transactiontype=side,
            quantity=quantity, price=price, ordertag=intent, signal_id=pos.signal_id)
    except Exception:
        # Includes unexpected wrapper errors after an HTTP request may have left.
        # refresh() resolves it against the order book or times it out.
        audit.log('order_submission_unknown', signal_id=pos.signal_id, intent=intent)
        return {'ok': False, 'reason': 'submission_unknown', 'pending': True}
    if not result.ok and not result.uncertain:
        pos.status = 'closed' if side == 'BUY' else previous
        retryable = result.reason in RETRYABLE_REASONS
        with c:
            c.execute("UPDATE execution_orders SET state='terminal',note=? WHERE intent=?",
                      (DELIVERY_FAILED if retryable else None, intent))
            storage.upsert_position(pos, _connection=c)
        if retryable:
            audit.log('execution_delivery_failed', signal_id=pos.signal_id, intent=intent,
                      side=side, reason=result.reason)
        return {'ok': False, 'reason': result.reason, 'retryable': retryable}
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


def _timed_out_today(now):
    return [dict(r) for r in connection().execute(
        'SELECT intent, signal_id FROM execution_orders WHERE note=? AND created_at >= ?',
        (UNKNOWN_TIMEOUT, now.date().isoformat()))]


def _watch_late_orders(book, now):
    """An order given up on may still reach the exchange. Alert loudly, once."""
    for row in _timed_out_today(now):
        if row['intent'] in _LATE_ALERTED:
            continue
        late = [s for s in book if s.raw.get('ordertag') == row['intent']]
        if late:
            _LATE_ALERTED.add(row['intent'])
            audit.log('execution_late_order_after_timeout', signal_id=row['signal_id'],
                      intent=row['intent'], order_id=late[0].order_id, status=late[0].status)
            alerts.enqueue(f"\U0001F6A8 Order {late[0].order_id} (signal #{row['signal_id']}) appeared "
                           f"in Angel One AFTER KADIMA timed it out ({late[0].status}). It is not "
                           f"managed by KADIMA - check the broker NOW.",
                           key=f"late-order:{row['intent']}", bypass=True)


def _expire(pos, row, note, now):
    """Mark an unresolvable row terminal and release its reservation.

    The local position keeps whatever quantity was already confirmed filled;
    nothing unconfirmed is assumed. A latched exit reason stays latched, so an
    exit whose SELL never reached the broker is sent again by the monitor."""
    with connection() as c:
        c.execute("UPDATE execution_orders SET state='terminal', note=? WHERE intent=?",
                  (note, row['intent']))
        if pos is not None:
            pos.status = 'open' if pos.remaining_qty > 0 else 'closed'
            storage.upsert_position(pos, _connection=c)
    audit.log('execution_order_expired', signal_id=row['signal_id'], intent=row['intent'],
              side=row['side'], note=note, order_id=row['order_id'],
              remaining_qty=getattr(pos, 'remaining_qty', None))
    if note == UNKNOWN_TIMEOUT:
        alerts.enqueue(f"Order for signal #{row['signal_id']} ({row['side']}) was not found in "
                       f"Angel One after {int(_setting('UNKNOWN_ORDER_TIMEOUT_SEC', 60))}s; "
                       f"KADIMA released it. Watching for a late arrival.",
                       key=f"unknown-timeout:{row['intent']}")


def refresh(positions, ltp_fn=None):
    """ltp_fn(exchange, token) -> fresh LTP or None; used to re-price exits.

    Returns False only for genuine inconsistencies (contract/side mismatch,
    impossible fills, ambiguous matches, a same-day order the executor does not
    track). An order not yet visible in the book is simply awaited, then
    expired after UNKNOWN_ORDER_TIMEOUT_SEC if its submission was ambiguous."""
    rows = pending()
    now = now_ist()
    watch = bool(_timed_out_today(now))
    if not rows and not watch:
        return True
    book = angel_orders.fetch_order_book()
    if book is None:
        # Only unresolved orders make an unreadable book a health problem.
        return not rows
    _watch_late_orders(book, now)
    healthy = True
    timeout = _setting('UNKNOWN_ORDER_TIMEOUT_SEC', 60)
    for row in rows:
        try:
            created = datetime.fromisoformat(row['created_at'])
        except (TypeError, ValueError):
            healthy = False
            continue
        previous_day = created.date() < now.date()
        pos = positions.get(str(row['signal_id']))
        matches = [s for s in book if (row['order_id'] and s.order_id == row['order_id'])
                   or s.raw.get('ordertag') == row['intent']]
        if pos is None:
            if previous_day and not matches:
                _expire(None, row, EXPIRED_PREVIOUS_DAY, now)
                continue
            healthy = False
            audit.log('execution_order_unresolved', signal_id=row['signal_id'],
                      intent=row['intent'], reason='position_not_tracked')
            continue
        if not matches:
            age = (now - created).total_seconds()
            if previous_day:
                _expire(pos, row, EXPIRED_PREVIOUS_DAY, now)
            elif not row['order_id'] and age >= timeout:
                _expire(pos, row, UNKNOWN_TIMEOUT, now)
            elif row['order_id'] and age >= timeout:
                # Angel accepted it (we hold its id) yet today's book lacks it.
                healthy = False
                audit.log('execution_order_unresolved', signal_id=pos.signal_id,
                          intent=row['intent'], reason='accepted_order_missing_from_book')
            continue
        if len({s.order_id for s in matches}) != 1:
            healthy = False
            audit.log('execution_order_unresolved', signal_id=pos.signal_id,
                      intent=row['intent'], reason='ambiguous_match')
            continue
        matches = matches[:1]
        if not row['order_id']:
            with connection() as c:
                c.execute('UPDATE execution_orders SET order_id=?, state=? WHERE intent=?',
                          (matches[0].order_id, 'pending', row['intent']))
            row['order_id'] = matches[0].order_id
            audit.log('execution_unknown_order_found', signal_id=pos.signal_id,
                      intent=row['intent'], order_id=matches[0].order_id)
        status = matches[0]
        # Validate contract and side when supplied by the broker.
        if any(status.raw.get(k) is not None and str(status.raw[k]) != str(v)
               for k, v in [('symboltoken', pos.token), ('exchange', pos.exchange),
                            ('transactiontype', row['side']), ('producttype', 'INTRADAY')]):
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
        symboltoken=pos.token, exchange=pos.exchange, producttype='INTRADAY'))
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

