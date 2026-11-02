"""Capital protection added after the 19-Oct-2026 review. No new order types.

B  entry_basis()        manual carry-forward positions get a real entry price
C  broker_stop_plan()   broker second-line stop - SHADOW ONLY in this build
D  monitor_interval()   3 s checks when a position is near its stop, else 10 s
E  reentry_signal()     re-enter after a loss stop-out (max 2 per signal per day)
F  watch_rejections()   every rejected order (KADIMA or manual) alerted with reason

Each feature reads its own off/shadow/live switch from trend_rules.json
(trend_rules.feature). Shadow logs the decision and changes nothing.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta

from . import audit, storage, trend_rules
from .utils import now_ist

FAST_SEC = 3.0
SLOW_SEC = 10.0
DEFAULT_NOISE_PCT = 3.0
MAX_REENTRIES = 2
REENTRY_COOLDOWN = timedelta(minutes=5)
WATCH_EVERY_SEC = 30.0
KADIMA_TAG = re.compile(r'^K[0-9a-f]{15}$')
ENTRY_FIELDS = ('buyavgprice', 'totalbuyavgprice', 'cfbuyavgprice', 'netprice')

_last_watch = {'at': 0.0}
_last_plan: dict = {}
_logged: set = set()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _table(sql):
    c = storage.conn()
    c.execute(sql)
    return c


# ------------------------------------------------------------------ B
def entry_basis(row):
    """Entry price for a broker position row. Overnight carry-forward rows carry
    buyavgprice = 0; the price lives in totalbuyavgprice / cfbuyavgprice."""
    first = _num(row.get('buyavgprice'))
    found = next(((k, _num(row.get(k))) for k in ENTRY_FIELDS if _num(row.get(k))), (None, None))
    if first is None and found[1] is not None:
        audit.log('adoption_entry_basis', symbol=row.get('tradingsymbol'),
                  product=row.get('producttype'), field=found[0], price=found[1],
                  mode=trend_rules.feature('cf_protect'),
                  fields={k: row.get(k) for k in ENTRY_FIELDS})
    if trend_rules.feature('cf_protect') == 'live':
        return found[1] or 0
    return first or 0


# ------------------------------------------------------------------ D
def _near_stop(executor, pos, now):
    ltp = executor.state.premium_tracker.latest(pos.token, 30.0)
    stop = getattr(pos, 'effective_stop', None)
    if not _num(ltp) or not _num(stop):
        return False
    noise = trend_rules.premium_noise_pct(
        trend_rules.tracker_samples(executor.state.premium_tracker, pos.token), now)
    noise = noise if _num(noise) else DEFAULT_NOISE_PCT
    return ltp - stop <= ltp * 2 * noise / 100


def monitor_interval(executor, now=None):
    mode = trend_rules.feature('monitor')
    if mode == 'off':
        return SLOW_SEC
    now = now or now_ist()
    try:
        near = [p.signal_id for p in list(executor.state.positions.values())
                if p.status == 'open' and p.remaining_qty > 0 and getattr(p, 'kind', None) != 'cas'
                and _near_stop(executor, p, now)]
    except Exception:  # noqa: BLE001 - never break the monitor loop
        return SLOW_SEC
    if near and mode == 'live':
        return FAST_SEC
    return SLOW_SEC


# ------------------------------------------------------------------ C (shadow only)
def broker_stop_plan(pos, ltp, tracker, now=None):
    """Log the broker STOPLOSS_LIMIT KADIMA would keep one noise-width behind the
    software stop. Never sends, modifies or cancels anything in this build: live
    use needs reconciliation of broker-side fills and a 1-lot market-hours test."""
    if trend_rules.feature('broker_stop') == 'off':
        return None
    now = now or now_ist()
    stop, entry = getattr(pos, 'effective_stop', None), getattr(pos, 'entry_price', None)
    if not _num(stop) or not _num(entry) or getattr(pos, 'status', None) != 'open':
        return None
    noise = trend_rules.premium_noise_pct(trend_rules.tracker_samples(tracker, pos.token), now)
    width = stop * (noise if _num(noise) else DEFAULT_NOISE_PCT) / 100
    trigger = max(stop - width, entry * (1 - trend_rules.HARD_CAP))
    trigger = math.floor(trigger * 20) / 20
    plan = dict(trigger=trigger, limit=max(0.05, round(trigger - 0.10, 2)))
    last = _last_plan.get(pos.signal_id)
    if last is None or plan['trigger'] - last['trigger'] >= width:
        _last_plan[pos.signal_id] = plan
        audit.log('broker_stop_shadow', signal_id=pos.signal_id, symbol=pos.symbol,
                  action='place' if last is None else 'modify', ltp=ltp,
                  software_stop=round(stop, 2), **plan)
    return plan


# ------------------------------------------------------------------ E
def _reentry_conn():
    return _table('CREATE TABLE IF NOT EXISTS protection_reentry ('
                  'origin INTEGER NOT NULL, sid INTEGER PRIMARY KEY, day TEXT NOT NULL, '
                  'created_at TEXT NOT NULL)')


def _origin(c, sid):
    row = c.execute('SELECT origin FROM protection_reentry WHERE sid=?', (sid,)).fetchone()
    return row[0] if row else sid


def reentry_signal(kind, index_name, direction, sid, tier, score, seen, now=None):
    """New signal id for a re-entry, or None. Called only when `sid` was already
    submitted. Conditions: last position of this signal chain closed at a loss,
    >= 5 min ago, today; fewer than 2 re-entries; trend/band rules live (they then
    gate the re-entry like any entry: with the trend, spot in the stop-side band half)."""
    mode = trend_rules.feature('reentry')
    if kind != 'gamma_blast' or mode == 'off':
        return None
    now = now or now_ist()
    day = now.date().isoformat()
    c = _reentry_conn()
    origin = _origin(c, sid)
    chain = [origin] + [r[0] for r in c.execute(
        'SELECT sid FROM protection_reentry WHERE origin=? ORDER BY sid', (origin,))]
    last = chain[-1]
    if not seen(last):
        return last if mode == 'live' and trend_rules.live() else None   # minted, not yet sent
    used = len(chain) - 1
    row = c.execute('SELECT status, entry_price, exit_price, exit_ts FROM positions '
                    'WHERE signal_id=?', (last,)).fetchone()
    why = None
    if used >= MAX_REENTRIES:
        why = 'max_reentries_used'
    elif row is None or row['status'] != 'closed':
        why = 'previous_position_not_closed'
    elif not (_num(row['exit_price']) and _num(row['entry_price'])
              and row['exit_price'] < row['entry_price']):
        why = 'previous_exit_not_a_loss'
    else:
        try:
            closed = datetime.fromisoformat(row['exit_ts'])
        except (TypeError, ValueError):
            closed = None
        if closed is None or closed.date() != now.date():
            why = 'previous_exit_not_today'
        elif now - closed < REENTRY_COOLDOWN:
            why = 'cooldown'
    if why:
        if why == 'max_reentries_used' and (origin, last) not in _logged:
            _logged.add((origin, last))
            audit.log('reentry_refused', origin=origin, reason=why, mode=mode)
        return None
    if mode != 'live' or not trend_rules.live():
        if (origin, last) not in _logged:
            _logged.add((origin, last))
            audit.log('reentry_would_allow', origin=origin, index=index_name, direction=direction,
                      previous=last, used=used, mode=mode, trend_rules=trend_rules.mode())
        return None
    payload = json.dumps(dict(reentry_of=origin, previous=last, attempt=used + 1))
    new = storage.insert_signal(index_name, tier, score, direction, payload, kind='gamma_blast')
    with c:
        c.execute('INSERT INTO protection_reentry VALUES (?,?,?,?)', (origin, new, day, now.isoformat()))
    audit.log('reentry_signal', origin=origin, sid=new, attempt=used + 1, index=index_name,
              direction=direction)
    return new


# ------------------------------------------------------------------ F
def _rejections_conn():
    return _table('CREATE TABLE IF NOT EXISTS protection_rejections ('
                  'order_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, payload TEXT NOT NULL)')


def classify(raw, kadima_order_ids=()):
    tag = str(raw.get('ordertag') or '')
    oid = str(raw.get('orderid') or raw.get('orderId') or '')
    return 'KADIMA' if KADIMA_TAG.match(tag) or oid in kadima_order_ids else 'MANUAL'


def watch_rejections(fetch=None, now=None, force=False):
    """Alert once per rejected order with the broker's reason. Read-only."""
    if trend_rules.feature('watchdog') == 'off':
        return []
    clock = time.monotonic()
    if not force and clock - _last_watch['at'] < WATCH_EVERY_SEC:
        return []
    hm = (now or now_ist()).strftime('%H:%M')
    if not force and not ('09:00' <= hm <= '16:00'):   # session plus post-close orders
        return []
    _last_watch['at'] = clock
    from . import angel_orders
    book = (fetch or angel_orders.fetch_order_book)()
    if book is None:
        return []
    now = now or now_ist()
    c = _rejections_conn()
    try:
        from . import execution_ledger
        ours = {r[0] for r in execution_ledger.connection().execute(
            'SELECT order_id FROM execution_orders WHERE order_id IS NOT NULL')}
    except Exception:  # noqa: BLE001
        ours = set()
    new = []
    for o in book:
        if o.status != 'rejected':
            continue
        if c.execute('SELECT 1 FROM protection_rejections WHERE order_id=?', (o.order_id,)).fetchone():
            continue
        raw = o.raw or {}
        item = dict(order_id=o.order_id, source=classify(raw, ours),
                    symbol=raw.get('tradingsymbol'), side=raw.get('transactiontype'),
                    product=raw.get('producttype'), quantity=raw.get('quantity'),
                    price=raw.get('price'), reason=str(raw.get('text') or '')[:300],
                    time=raw.get('updatetime') or raw.get('exchtime'))
        with c:
            c.execute('INSERT INTO protection_rejections VALUES (?,?,?)',
                      (o.order_id, now.isoformat(), json.dumps(item, default=str)))
        new.append(item)
        audit.log('order_rejected', **item)
        if trend_rules.feature('watchdog') == 'live':
            try:
                from . import alerts
                alerts.enqueue(f"REJECTED {item['source']} {item['side']} {item['symbol']} "
                               f"{item['product']} qty {item['quantity']} @ {item['price']}: "
                               f"{item['reason'] or 'no reason given'}", key='rejected:' + o.order_id)
            except Exception:  # noqa: BLE001
                pass
    if new:
        try:
            from . import celestial
            recent = [json.loads(r[0]) for r in c.execute(
                'SELECT payload FROM protection_rejections ORDER BY seen_at DESC LIMIT 20')]
            celestial.health('rejections', dict(items=recent))
        except Exception:  # noqa: BLE001
            pass
    return new
