"""Account for subsequent manual fills only when book deltas explain net quantity.

No orders are submitted. A baseline is saved for a verified long intraday
position. Later same-side external fills can resize/close it atomically. Mixed
buy/sell episodes, revised books, missing orders and reversals stay unresolved.
"""
import copy
import json
import math
from . import celestial, execution_ledger, storage
from .angel_orders import product_for
from .utils import now_ist


def connection():
    c = celestial.connection()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS celestial_external_cursors (
        signal_id INTEGER PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS celestial_external_fills (
        signal_id INTEGER NOT NULL, order_id TEXT NOT NULL, cumulative_qty INTEGER NOT NULL,
        quantity INTEGER NOT NULL, price REAL NOT NULL, pnl REAL NOT NULL, ts TEXT NOT NULL,
        PRIMARY KEY(signal_id, order_id, cumulative_qty));
    ''')
    return c


def reconcile(executor, broker_rows, book):
    c = connection()
    unresolved = set()
    owned = {r[0] for r in execution_ledger.connection().execute('SELECT order_id FROM execution_orders')}
    live = {}
    try:
        for r in broker_rows:
            key = str(r['exchange']), str(r['symboltoken']), str(r['producttype'])
            qty = float(r['netqty'])
            if key in live or not math.isfinite(qty) or not qty.is_integer():
                return {str(p.signal_id) for p in executor.state.positions.values()}
            live[key] = int(qty)
    except (KeyError, ValueError, TypeError):
        return {str(p.signal_id) for p in executor.state.positions.values()}
    for original in list(executor.state.positions.values()):
        if original.status != 'open' or execution_ledger.pending(original.signal_id):
            continue
        key = original.exchange, original.token, product_for(original)
        current_qty = live.get(key, 0)
        if current_qty < 0 or current_qty % original.lot_size:
            continue
        orders = [o for o in book if o.order_id not in owned and
                  (o.raw.get('exchange'), str(o.raw.get('symboltoken')), o.raw.get('producttype')) == key]
        if any(o.status not in execution_ledger.TERMINAL for o in orders):
            continue
        current = {o.order_id: dict(q=o.filled_qty, n=o.filled_qty*(o.avg_price or 0),
                                  side=o.raw.get('transactiontype')) for o in orders}
        if len(current) != len(orders):
            unresolved.add(str(original.signal_id))
            continue
        day = now_ist().date().isoformat()
        saved = c.execute('SELECT day,payload FROM celestial_external_cursors WHERE signal_id=?',
                          (original.signal_id,)).fetchone()
        if saved is None or saved[0] != day:
            if current_qty == original.remaining_qty:
                with c:
                    c.execute('INSERT OR REPLACE INTO celestial_external_cursors VALUES (?,?,?)',
                              (original.signal_id, day, json.dumps(current)))
            continue
        previous = json.loads(saved[1])
        if set(previous) - set(current):
            unresolved.add(str(original.signal_id))
            continue
        changes, valid = [], True
        for oid, row in current.items():
            old = previous.get(oid, dict(q=0, n=0, side=row['side']))
            delta, amount = row['q'] - old['q'], row['n'] - old['n']
            if (delta < 0 or row['side'] != old['side'] or row['side'] not in ('BUY','SELL')
                    or not math.isfinite(amount) or (delta == 0 and abs(amount) > .01)
                    or (delta > 0 and amount <= 0)):
                valid = False
                break
            if delta:
                changes.append((oid, row, delta, amount))
        if not valid or (changes and len({r['side'] for _,r,_,_ in changes}) != 1):
            unresolved.add(str(original.signal_id))
            continue
        if not changes:
            continue
        buying = changes[0][1]['side'] == 'BUY'
        delta_qty = sum(q for _,_,q,_ in changes)
        expected = original.remaining_qty + (delta_qty if buying else -delta_qty)
        if expected != current_qty or expected < 0:
            unresolved.add(str(original.signal_id))
            continue
        pos = copy.copy(original)
        amount = sum(n for _,_,_,n in changes)
        if buying:
            pos.entry_price = (pos.entry_price*pos.remaining_qty + amount) / expected
            # Existing protection never loosens after an external scale-in.
            pos.structural_stop = max(pos.structural_stop or 0, pos.entry_price*.95)
            pos.effective_stop = max(pos.effective_stop or 0, pos.structural_stop)
            pos.peak_price = max(pos.peak_price or 0, pos.entry_price)
            pos.lots = expected // pos.lot_size
            pos.allocated_capital = expected * pos.entry_price
        else:
            pos.exit_price = amount / delta_qty
            pos.exit_ts = now_ist()
            if expected == 0:
                pos.status = 'closed'
                pos.exit_reason = 'external_fill_reconciled'
        pos.remaining_qty = expected
        with c:
            for oid, row, qty, notional in changes:
                pnl = 0 if buying else notional - pos.entry_price*qty
                c.execute('INSERT INTO celestial_external_fills VALUES (?,?,?,?,?,?,?)',
                          (pos.signal_id, oid, row['q'], qty, notional/qty, pnl, now_ist().isoformat()))
            storage.upsert_position(pos, _connection=c)
            c.execute('INSERT OR REPLACE INTO celestial_external_cursors VALUES (?,?,?)',
                      (pos.signal_id, day, json.dumps(current)))
        original.__dict__.update(pos.__dict__)
        if expected == 0:
            executor.state.positions.pop(str(pos.signal_id), None)
    return unresolved


def realized_today():
    return connection().execute('SELECT COALESCE(SUM(pnl),0) FROM celestial_external_fills WHERE ts LIKE ?',
                                (now_ist().date().isoformat()+'%',)).fetchone()[0]
