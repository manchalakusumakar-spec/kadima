"""Shared guarded exit operation. Caller owns lifetime lock and executor RLock.

Only broker-confirmed KADIMA buys minus KADIMA sells establish ownership.
Manual/adopted holdings, dry-run records, product mismatches and unknown orders
never authorize an exit. Order acknowledgement is not closure.
"""
from datetime import datetime
import math

from . import angel_orders, execution_ledger, storage
from .priority_sessions import exit_due
from .utils import now_ist


def owned_positions(positions, ledger, broker, book, at):
    if broker is None or book is None:
        raise ValueError('broker_books_unavailable')
    if any(o.status not in execution_ledger.TERMINAL for o in book):
        raise ValueError('broker_pending_orders_require_reconciliation')
    if any(r['state'] != 'terminal' for r in ledger):
        raise ValueError('local_pending_order_requires_reconciliation')
    live = {}
    for row in broker:
        qty = float(row.get('netqty'))
        if not math.isfinite(qty) or not qty.is_integer():
            raise ValueError('broker_quantity_invalid')
        if qty == 0:
            continue
        key = (row.get('exchange'), str(row.get('symboltoken')), row.get('producttype'))
        if key in live:
            raise ValueError('duplicate_broker_contract')
        live[key] = (int(qty), row.get('tradingsymbol'))
    selected = []
    keys = [(p.exchange, p.token) for p in positions.values() if p.status != 'closed']
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate_local_contract')
    for p in positions.values():
        if p.status != 'open' or type(p.remaining_qty) is not int or p.remaining_qty <= 0:
            continue
        if not exit_due(p.exchange, at):
            continue
        rows = [r for r in ledger if r['signal_id'] == p.signal_id]
        if not rows or any(r['dry_run'] or not r['order_id'] for r in rows):
            continue
        bought = sum(r['filled'] for r in rows if r['side'] == 'BUY')
        sold = sum(r['filled'] for r in rows if r['side'] == 'SELL')
        if bought <= 0 or bought - sold != p.remaining_qty:
            continue
        if live.get((p.exchange, p.token, 'CARRYFORWARD' if getattr(p, 'kind', None) == 'cas_blast' else 'INTRADAY')) != (p.remaining_qty, p.symbol):
            raise ValueError('owned_broker_quantity_or_product_changed')
        selected.append(p)
    return selected


def cycle(executor):
    from .auto_executor import fetch_broker_positions
    from .cas_orders import fetch_quote
    from .cas_engine import fresh
    with executor._execution_lock:
        if not execution_ledger.refresh(executor.state.positions):
            raise ValueError('execution_fill_refresh_failed')
        book = angel_orders.fetch_order_book()
        broker = fetch_broker_positions()
        ledger = [dict(r) for r in execution_ledger.connection().execute('SELECT * FROM execution_orders')]
        due = owned_positions(executor.state.positions, ledger, broker, book, now_ist())
        results = []
        for pos in due:
            # A source-timestamped executable bid; never fall back to entry/peak.
            quote = fetch_quote(pos.exchange, pos.token, pos.symbol, 0, pos.direction, for_exit=True)
            if quote is None:
                results.append(dict(signal_id=pos.signal_id, ok=False, reason='fresh_exit_bid_unavailable'))
                continue
            # Re-read both books just before submission, under the same writer lock.
            current = owned_positions(executor.state.positions, ledger, fetch_broker_positions(),
                                      angel_orders.fetch_order_book(), now_ist())
            if pos not in current:
                results.append(dict(signal_id=pos.signal_id, ok=False, reason='ownership_changed'))
                continue
            if not fresh(quote.quote_at, now_ist()):
                results.append(dict(signal_id=pos.signal_id, ok=False, reason='exit_bid_expired_during_broker_check'))
                continue
            results.append(dict(signal_id=pos.signal_id,
                **executor._close_position(pos, quote.bid, 'owned_eod_watchdog')))
            # One unresolved submission at a time, including ambiguous responses.
            if execution_ledger.pending():
                break
        return dict(ok=all(r.get('ok') for r in results), inspected_positions=len(broker),
                    due_owned_positions=len(due), results=results,
                    closure_verified=False, checked_at=now_ist().isoformat())


def restore(executor):
    for raw in storage.load_open_positions():
        from .auto_executor import Position
        for key in ('entry_ts', 'exit_ts'):
            raw[key] = datetime.fromisoformat(raw[key]) if raw.get(key) else None
        raw['trail_active'] = bool(raw['trail_active'])
        executor.state.positions[str(raw['signal_id'])] = Position(**raw)
    # No adoption and no new entry thread in the fallback process.
    executor._restored = True
