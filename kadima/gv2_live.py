"""GAMMA V2 LIVE (2026-11-02, trader approved).

Executes Gamma v2 slot trades chosen by the gv2 watcher (/opt/kadima-gamma-v2/gv2.py).
The watcher writes one intent file per qualifying first-of-signal plan (rules G1/G2/BM);
this module, running inside the engine process, applies the live discipline and places
the order through the engine's own execution_ledger (same broker session, fills,
repricing, reconciliation). Exits for these positions are decided here, not by the
engine's generic gamma exit stack:

  G1/G2 : plan T1 -> exit all, plan stop -> exit all (the rule set's measured exits)
  BM    : 50% hard stop; at +100% book half; remainder trails 30% below peak
  all   : time exit 15:15 IST; engine EOD / daily-loss flatten also honoured

Live discipline (per trading day): one gv2 position at a time, full available cash,
next entry only after a winning exit, stop for the day after any loss, max 3 filled
trades, entries 09:15-13:30 (BM before 11:00), no PE on Thursday.

Switch: /var/lib/kadima-gamma-v2/config.json "live": true|false (re-read every intent).
Never raises into the engine.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time

from . import alerts, audit, capital_guard, execution_ledger, killswitch, storage
from .utils import in_market_hours, now_ist, past_signal_cutoff

HOME = os.environ.get('GV2_HOME', '/var/lib/kadima-gamma-v2')
INTENTS = os.path.join(HOME, 'intents')
MAX_INTENT_AGE_SEC = 60
MAX_CHASE = 1.05          # skip if premium already ran >5% above the plan entry
CASH_USE = 0.97           # full cash, small reserve for charges
MAX_TRADES = 3
ENTRY_START, ENTRY_END, BM_END, TIME_EXIT = '09:15', '13:30', '11:00', '15:15'

_lock = threading.RLock()
_state = {'day': None, 'm': {}}       # m: sid -> record
_started = False


# ------------------------------------------------------------------ state
def _state_path(day):
    return os.path.join(HOME, 'live-%s.json' % day)


def _load(day):
    if _state['day'] == day:
        return _state['m']
    try:
        with open(_state_path(day)) as f:
            m = json.load(f).get('managed', {})
    except (OSError, ValueError):
        m = {}
    _state['day'], _state['m'] = day, m
    return m


def _save():
    day = _state['day']
    if not day:
        return
    tmp = _state_path(day) + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'day': day, 'managed': _state['m']}, f, default=str)
    os.replace(tmp, _state_path(day))


def managed_ids():
    # lock-free on purpose: called from check_exits, possibly under engine locks
    try:
        return set(_load(now_ist().strftime('%Y-%m-%d')).keys())
    except Exception:  # noqa: BLE001
        return set()


def live_enabled():
    try:
        with open(os.path.join(HOME, 'config.json')) as f:
            return json.load(f).get('live') is True
    except (OSError, ValueError):
        return False


def _log(event, **kw):
    try:
        audit.log(event, **kw)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ entries
def _discipline_block(ex, m):
    for sid, r in m.items():
        pos = ex.state.positions.get(sid)
        if pos is not None and pos.status != 'closed' and (pos.remaining_qty > 0 or pos.status == 'entry_pending'):
            return 'gv2_slot_busy'
    done = [r for r in m.values() if r.get('filled')]
    if any(r.get('result') == 'loss' for r in done):
        return 'gv2_stopped_after_loss'
    if len(done) >= MAX_TRADES:
        return 'gv2_max_trades'
    if done and done[-1].get('result') not in ('win',):
        # last filled trade has no recorded winning exit (should be closed by now)
        return 'gv2_previous_not_won'
    return None


def _process(ex, it):
    from . import auto_executor as ae
    from .instrument_master import get_lot_size
    now = now_ist()
    day = now.strftime('%Y-%m-%d')
    hhmm = now.strftime('%H:%M')
    rules = it.get('rules') or []
    bm = 'BM' in rules
    if not live_enabled():
        return 'gv2_not_live'
    if it.get('day') != day or now.timestamp() - float(it.get('written_at', 0)) > MAX_INTENT_AGE_SEC:
        return 'gv2_intent_stale'
    if not (ENTRY_START <= hhmm < ENTRY_END) or (bm and hhmm >= BM_END):
        return 'gv2_outside_window'
    if killswitch.engaged() or not in_market_hours() or past_signal_cutoff():
        return 'gv2_engine_gate_closed'
    if getattr(ex, '_reconcile_blocked', True):
        return 'gv2_reconcile_blocked'
    from . import exit_manager
    if exit_manager.realized_pnl_today() <= -ae._daily_loss_limit():
        return 'gv2_daily_loss_limit'
    with _lock:
        m = _load(day)
        block = _discipline_block(ex, m)
    if block:
        return block
    exch, token, symbol = it.get('exchange'), str(it.get('token') or ''), it.get('symbol')
    entry, t1, sl = float(it.get('entry') or 0), float(it.get('t1') or 0), float(it.get('stop') or 0)
    if not (exch and token and symbol and entry > 0):
        return 'gv2_intent_incomplete'
    if not bm and not (t1 > entry > sl > 0):
        return 'gv2_plan_levels_invalid'
    if (exch, token) in getattr(ex, '_external_contracts', set()):
        return 'gv2_contract_held_outside'
    if any(p.token == token and p.status != 'closed' for p in ex.state.positions.values()):
        return 'gv2_contract_already_open'
    lot_size = get_lot_size(symbol, token)
    if not lot_size:
        return 'gv2_lot_size_unknown'
    ltp = ae._poll_ltp(exch, [token]).get(token)
    if not ltp or ltp <= 0:
        return 'gv2_no_fresh_quote'
    if ltp > entry * MAX_CHASE:
        return 'gv2_ran_away'
    if not bm and ltp <= sl:
        return 'gv2_below_plan_stop'
    funds = capital_guard.broker_available_funds()
    if not funds or funds <= 0:
        return 'gv2_funds_unavailable'
    limit = round(min(ltp * (1 + ae.LIMIT_ORDER_BUFFER_PCT / 100.0), entry * MAX_CHASE), 1)
    lots = int((funds * CASH_USE) // (limit * lot_size))
    if lots < 1:
        return 'gv2_lot_does_not_fit_cash'
    qty = lots * lot_size
    sid = int('9' + now.strftime('%y%m%d%H%M%S'))
    stop0 = round(limit * 0.5, 2) if bm else sl
    pos = ae.Position(signal_id=sid, kind='gamma_blast', index=it.get('und'), direction=it.get('direction'),
                      symbol=symbol, token=token, exchange=exch, entry_price=0.0, entry_ts=now,
                      lots=lots, lot_size=lot_size, remaining_qty=0, status='entry_pending',
                      structural_stop=stop0, effective_stop=stop0, allocated_capital=float(funds))
    with _lock:
      m[str(sid)] = dict(sid=sid, audit_id=it.get('audit_id'), rules=rules, bm=bm, symbol=symbol,
                       entry_plan=entry, t1=t1, sl=sl, limit=limit, qty=qty, at=now.isoformat(),
                       filled=False, result=None, half_done=False, peak=None)
      _save()
    with ex._execution_lock:
        with ex._lock:
            ex.state.positions[str(sid)] = pos
        result = execution_ledger.submit(pos, 'BUY', qty, limit)
        if pos.status == 'closed':
            ex.state.positions.pop(str(sid), None)
    ex._last_reconcile = 0.0
    with _lock:
        m[str(sid)]['submit'] = result
        _save()
    _log('gv2_live_entry', signal_id=sid, symbol=symbol, rules=rules, qty=qty, limit=limit,
         ltp=ltp, funds=round(funds, 2), result=result)
    alerts.enqueue('KADIMA v2 LIVE ENTRY %s %s qty %d @ %.2f | %s | %s' % (
        '/'.join(rules), symbol, qty, limit,
        ('BM: 50%% stop, half at +100%%, trail 30%%' if bm else 'T1 %.2f / SL %.2f' % (t1, sl)),
        (result or {}).get('reason')), key='gv2entry:%d' % sid)
    return 'gv2_submitted' if (result or {}).get('ok') or (result or {}).get('pending') else \
        'gv2_submit_failed:%s' % (result or {}).get('reason')


def _tick(ex):
    day = now_ist().strftime('%Y-%m-%d')
    d = os.path.join(INTENTS, day)
    for path in sorted(glob.glob(os.path.join(d, '*.json'))):
        try:
            with open(path) as f:
                it = json.load(f)
        except (OSError, ValueError):
            continue
        try:
            outcome = _process(ex, it)
        except Exception as exc:  # noqa: BLE001
            outcome = 'gv2_error:%s' % str(exc)[:160]
        it['outcome'], it['processed_at'] = outcome, now_ist().isoformat()
        try:
            with open(path + '.done', 'w') as f:
                json.dump(it, f, default=str)
            os.remove(path)
        except OSError:
            pass
        _log('gv2_live_intent', audit_id=it.get('audit_id'), symbol=it.get('symbol'),
             rules=it.get('rules'), outcome=outcome)


def _open_managed(ex):
    ids = managed_ids()
    return [p for k, p in list(ex.state.positions.items()) if k in ids and p.status != 'closed']


ENTRY_TIMEOUT_SEC = 5      # trader 2026-11-02: unfilled entry cancelled as fast as possible


def _entry_timeout():
    try:
        with open(os.path.join(HOME, 'config.json')) as f:
            return max(3.0, float(json.load(f).get('entry_timeout_sec', ENTRY_TIMEOUT_SEC)))
    except (OSError, ValueError, TypeError):
        return float(ENTRY_TIMEOUT_SEC)


def _entry_watch(ex, do_refresh):
    """Cancel the unfilled part of a gv2 BUY after ENTRY_TIMEOUT_SEC (no exit latch:
    a partial fill stays as the trade) and poll the order book fast while pending."""
    from . import angel_orders
    pend = [p for p in _open_managed(ex) if p.status == 'entry_pending']
    if not pend:
        return
    timeout = _entry_timeout()
    for p in pend:
        r = _state['m'].get(str(p.signal_id)) or {}
        age = (now_ist() - p.entry_ts).total_seconds() if p.entry_ts else 0
        if age < timeout or r.get('cancel_sent'):
            continue
        rows = execution_ledger.pending(p.signal_id)
        oid = rows[0]['order_id'] if len(rows) == 1 and rows[0]['side'] == 'BUY' else None
        if oid:
            res = angel_orders.cancel_order(oid)
            with _lock:
                r['cancel_sent'] = True
                _save()
            _log('gv2_entry_timeout_cancel', signal_id=p.signal_id, order_id=oid, age=round(age, 1),
                 filled=p.remaining_qty, result=str(res)[:120])
    if do_refresh:
        ex.refresh_orders()


def _loop(ex):
    last_exit_check = 0.0
    n = 0
    while True:
        try:
            _tick(ex)
            n += 1
            _entry_watch(ex, n % 2 == 0)
            if _open_managed(ex) and time.monotonic() - last_exit_check >= 3:
                last_exit_check = time.monotonic()
                ex.check_exits(kinds={'gamma_blast'})
        except Exception as exc:  # noqa: BLE001
            _log('gv2_live_loop_error', error=str(exc)[:200])
        time.sleep(1)


def start(ex):
    global _started
    if _started:
        return
    _started = True
    os.makedirs(INTENTS, exist_ok=True)
    threading.Thread(target=_loop, args=(ex,), daemon=True, name='gv2-live').start()
    _log('gv2_live_started', live=live_enabled())


# ------------------------------------------------------------------ exits
def _half_qty(pos):
    lots = int(pos.remaining_qty) // int(pos.lot_size or 1)
    return (lots // 2) * int(pos.lot_size) if lots >= 2 else 0


def exit_reason(ex, pos, ltp, now, urgent):
    """Called from AutoExecutor.check_exits for managed positions. Returns a close
    reason or None. May submit the BM half-booking itself."""
    from . import auto_executor as ae
    with _lock:
        m = _load(now.strftime('%Y-%m-%d'))
        r = m.get(str(pos.signal_id))
        if r is None:
            return None
        if pos.status == 'entry_pending' and pos.remaining_qty <= 0:
            return 'gv2_urgent_cancel_entry' if urgent else None
        if pos.status != 'open' or pos.remaining_qty <= 0 or not pos.entry_price:
            return None
        entry = float(pos.entry_price)
        if not r.get('filled'):
            r['filled'], r['fill_price'] = True, entry
        r['peak'] = max(float(r.get('peak') or 0), float(ltp), entry)
        reason = None
        if urgent:
            reason = 'gv2_engine_flatten'
        elif now.strftime('%H:%M') >= TIME_EXIT:
            reason = 'gv2_time_exit_1515'
        elif r.get('bm'):
            stop = entry * 0.5
            if not r.get('half_done') and ltp >= entry * 2.0:
                qty = _half_qty(pos)
                r['half_done'] = True
                if qty > 0 and not execution_ledger.pending(pos.signal_id):
                    limit = max(0.05, round(ltp * (1 - ae.LIMIT_ORDER_BUFFER_PCT / 100.0), 1))
                    res = execution_ledger.submit(pos, 'SELL', qty, limit)
                    r['half_submit'] = res
                    _log('gv2_bm_half_booked', signal_id=pos.signal_id, qty=qty, limit=limit, result=res)
            if r.get('half_done'):
                stop = max(stop, r['peak'] * 0.7)
            pos.structural_stop = pos.effective_stop = round(stop, 2)
            if ltp <= stop:
                reason = 'gv2_bm_trail' if r.get('half_done') else 'gv2_bm_stop'
        else:
            pos.structural_stop = pos.effective_stop = r['sl']
            if ltp >= r['t1']:
                reason = 'gv2_t1'
            elif ltp <= r['sl']:
                reason = 'gv2_stop'
        if reason:
            win = r.get('half_done') or ltp > entry
            r['result'] = 'win' if win else 'loss'
            r['exit_reason'], r['exit_ltp'], r['exit_at'] = reason, ltp, now.isoformat()
            _log('gv2_live_exit', signal_id=pos.signal_id, reason=reason, ltp=ltp, entry=entry,
                 result=r['result'])
            alerts.enqueue('KADIMA v2 LIVE EXIT %s %s @ ~%.2f (entry %.2f) -> %s' % (
                pos.symbol, reason, ltp, entry, r['result'].upper()), key='gv2exit:%s' % pos.signal_id)
        _save()
    try:
        storage.upsert_position(pos)
    except Exception:  # noqa: BLE001
        pass
    return reason
