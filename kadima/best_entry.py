"""BEST ENTRY v1 (trader 2026-10-26: "entry and exit to be fixed immediately" and
"i also want a best possible entry to avoid this stoploss issue").

Incident: NIFTY 27-Oct 22450 CE, bought 182.10, sold 172.30 (-5.4%, -Rs 637) at ~10:14,
then ran to 355.95. A good signal, a chase entry, and a stop inside normal premium noise.

ENTRY (auto entries of the kinds in `entry_kinds`; manual buys never touched)
A signal no longer means "buy now". The first time a signal reaches order submission,
its contract is ARMED at the current premium (arm price) and the order is held back.
Every later evaluation of the same, still-eligible signal (the engine re-evaluates it
each poll; every other gate runs again each time) asks:

  1. Pullback zone. zone ceiling = arm price - DIP_ATR x 1-min ATR (at least MIN_DIP_PCT).
  2. Dip held. premium touched the zone since arming AND has bounced BOUNCE_ATR x ATR
     off that low (a higher low), and is not more than CHASE_ATR x ATR above the zone.
        -> BUY full size; stop goes below the pullback low (see STOP).
  3. No spike chasing. never buy while the last completed 1-min candle's range is more
     than SPIKE_ATR x ATR and the premium is in the top third of it. -> wait
  4. No dip within WAIT_MIN minutes. -> BUY HALF size (1 lot stays 1 lot) at the current
     price, unless rule 3 blocks. At EXPIRE_MIN it buys half size even on a spike: the
     trader's rule is that an entry must never be missed (26-Oct 16:13).
  5. Strike check. delta above MAX_DELTA (deep ITM) is logged only (never skips).
  6. Entry order not filled: instead of the old cancel at 30 s, the BUY limit is walked
     up to fresh LTP + REPRICE_BUFFER every REPRICE_EVERY_SEC, never above the first
     limit + MAX_CHASE_PCT, for up to REPRICE_WINDOW_SEC; then the old cancel applies.
  Too little premium history (< MIN_CANDLES completed 1-min candles) -> buy as before
  (never block on missing data).

STOP (entries made through this module)
  stop = lowest of (engine stop, pullback low - LOW_BUFFER_ATR x ATR, entry - NOISE_5M_MULT
  x 5-min premium range), never below the HARD_CAP (35%) loss. Lots are re-sized by the
  existing allocation / Sniper checks AFTER this, so rupee risk stays inside the limits.

EXIT (loss side only, kinds in `exit_kinds`; profit side, trail, EOD, daily loss limit,
kill switch, CAS, MCX, manual trades: unchanged)
  a. Grace: for GRACE_MIN minutes after the fill, no loss exit unless the 35% hard cap.
  b. After grace, stop-type exits need a completed 1-min close below the stop, or a
     considerable breach (BREACH_ATR x ATR below it) on a tick.
  c. After grace, tick-type signal exits (opposite gamma/shield, OI reversed, news block)
     must persist PERSIST_SEC before they close a losing trade.

Config: DATA_DIR/best_entry.json, re-read every 30 s: {"mode": "off|shadow|live", ...}.
shadow logs every decision and changes nothing. Any internal error -> behave as before
(entry goes through as the engine decided, exit goes through).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from . import audit
from .config import DATA_DIR
from .utils import now_ist

MARK = 'BEST ENTRY v1'
DEFAULTS = {
    'mode': 'off',
    'entry_kinds': 'gamma_blast,bull_shield,bear_shield',
    'exit_kinds': 'gamma_blast,bull_shield,bear_shield',
    'dip_atr': 0.5,
    'min_dip_pct': 1.5,
    'bounce_atr': 0.25,
    'chase_atr': 0.5,
    'spike_atr': 2.0,
    'wait_min': 3.0,
    'expire_min': 6.0,
    'max_delta': 0.75,
    'min_candles': 3,
    'low_buffer_atr': 0.25,
    'noise_5m_mult': 1.0,
    'hard_cap': 0.35,
    'grace_min': 10.0,
    'breach_atr': 0.5,
    'persist_sec': 60,
    'reprice_every_sec': 5,
    'reprice_window_sec': 90,
    'reprice_buffer_pct': 1.0,
    'max_chase_pct': 4.0,
}
STOP_REASONS = frozenset({'structural_stop_breached', 'dynamic_profit_trail_breached'})
NEVER_HOLD = frozenset({'daily_loss_limit_breached', 'eod_force_close', 'mcx_session_exit',
                        'killswitch_engaged', 'manual_exit_dashboard', 'squareoff_dashboard'})

_cfg = {'at': 0.0, 'value': None}
_lock = threading.Lock()
_arms: dict = {}        # signal_id -> arm state
_done: set = set()      # signal ids whose arm expired today (skipped, not re-armed)
_persist: dict = {}     # (signal_id, reason) -> first seen datetime
_logged: dict = {}      # throttle: key -> last log monotonic


# ------------------------------------------------------------------ config
def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'best_entry.json')) as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k not in value:
                    continue
                d = DEFAULTS[k]
                if isinstance(d, str):
                    ok = isinstance(v, str)
                else:
                    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
                    if ok:
                        v = float(v) if isinstance(d, float) else int(v)
                if ok:
                    value[k] = v
        except (OSError, ValueError):
            pass
        if value['mode'] not in ('off', 'shadow', 'live'):
            value['mode'] = 'off'
        _cfg.update(at=now, value=value)
    return _cfg['value']


def mode():
    return config()['mode']


def _kinds(key):
    return {k.strip() for k in config()[key].split(',') if k.strip()}


def _log(event, throttle_key=None, every=30.0, **fields):
    if throttle_key is not None:
        now = time.monotonic()
        if now - _logged.get(throttle_key, -1e9) < every:
            return
        _logged[throttle_key] = now
    try:
        audit.log(event, mode=mode(), **fields)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ candles
def candles(samples, now, minutes=20):
    """Completed 1-min OHLC candles from (ts, ltp) samples, oldest first, plus the
    current (incomplete) candle or None."""
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=minutes)
    cur_min = now.replace(second=0, microsecond=0)
    buckets: dict = {}
    for ts, px in samples:
        if ts is None or px is None or px <= 0 or ts < start or ts > now:
            continue
        m = ts.replace(second=0, microsecond=0)
        b = buckets.get(m)
        if b is None:
            buckets[m] = [px, px, px, px]
        else:
            b[1] = max(b[1], px); b[2] = min(b[2], px); b[3] = px
    done = [(m, *buckets[m]) for m in sorted(buckets) if m < cur_min]
    cur = buckets.get(cur_min)
    return done, ((cur_min, *cur) if cur else None)


def atr1(done, n=5):
    ranges = [h - l for _, o, h, l, c in done[-n:]]
    return sum(ranges) / len(ranges) if ranges else None


def range5(done):
    last = done[-5:]
    if not last:
        return None
    return max(h for _, o, h, l, c in last) - min(l for _, o, h, l, c in last)


# ------------------------------------------------------------------ entry
def decide(*, kind, manual, signal_id, token, ltp, stop, delta, samples, now=None):
    """Entry decision at the point of order submission.
    Returns {'go': True, 'limit': float|None, 'stop': float|None, 'fraction': float, 'why': str}
    or {'go': False, 'block': reason}. Never raises."""
    try:
        return _decide(kind, manual, signal_id, token, ltp, stop, delta, samples,
                       now or now_ist())
    except Exception as exc:  # noqa: BLE001 - fail open: behave as before
        _log('best_entry_error', where='decide', error=str(exc)[:200])
        return {'go': True, 'limit': None, 'stop': None, 'fraction': 1.0, 'why': 'error'}


def _decide(kind, manual, sid, token, ltp, stop, delta, samples, now):
    passthrough = {'go': True, 'limit': None, 'stop': None, 'fraction': 1.0, 'why': 'not_applied'}
    m = mode()
    if m == 'off' or manual or kind not in _kinds('entry_kinds') or not ltp or ltp <= 0:
        return passthrough
    cfg = config()
    live = m == 'live'
    shadow_go = dict(passthrough, why='shadow')

    def out(res):
        if live:
            return res
        _log('best_entry_shadow', throttle_key=('sh', sid, res.get('block') or res.get('why')),
             signal_id=sid, would=res)
        return shadow_go

    if delta is not None and abs(delta) > cfg['max_delta']:
        _log('best_entry_deep_itm', throttle_key=('itm', sid), signal_id=sid,
             delta=round(abs(delta), 3), max_delta=cfg['max_delta'])   # note only

    done, cur = candles(samples or [], now)
    a = atr1(done)
    if len(done) < cfg['min_candles'] or not a or a <= 0:
        _log('best_entry_no_history', throttle_key=('nh', sid), signal_id=sid, candles=len(done))
        return passthrough
    with _lock:
        arm = _arms.get(sid)
        if arm is None or arm['token'] != token or arm['day'] != now.date():
            ceiling = min(ltp - cfg['dip_atr'] * a, ltp * (1 - cfg['min_dip_pct'] / 100))
            arm = dict(token=token, day=now.date(), at=now, price=ltp, atr=a,
                       ceiling=ceiling, low=ltp, touched=False)
            _arms[sid] = arm
            _log('best_entry_armed', signal_id=sid, token=token, price=ltp,
                 atr=round(a, 2), zone_ceiling=round(ceiling, 2))
        arm['low'] = min(arm['low'], ltp)
        if arm['low'] <= arm['ceiling']:
            arm['touched'] = True
    waited = (now - arm['at']).total_seconds() / 60.0

    spike = False
    if done:
        _, o, h, l, c = done[-1]
        rng = h - l
        if rng > cfg['spike_atr'] * a and ltp >= l + rng * 2 / 3:
            spike = True

    if arm['touched']:
        bounced = ltp >= arm['low'] + cfg['bounce_atr'] * a
        not_chasing = ltp <= arm['ceiling'] + cfg['chase_atr'] * a
        if bounced and not_chasing and not spike:
            new_stop = _stop(stop, ltp, arm['low'], a, done, cfg)
            _log('best_entry_go', signal_id=sid, why='pullback_held', arm_price=arm['price'],
                 low=arm['low'], ltp=ltp, stop=new_stop, waited_min=round(waited, 2))
            return out({'go': True, 'limit': None, 'stop': new_stop, 'fraction': 1.0,
                        'why': 'pullback_held', 'low': arm['low']})
    late = waited >= cfg['expire_min']          # never miss: buy even on a spike now
    if late or (waited >= cfg['wait_min'] and not spike):
        why = 'no_pullback_half_size' if not late else 'deadline_half_size'
        new_stop = _stop(stop, ltp, min(arm['low'], ltp), a, done, cfg)
        _log('best_entry_go', signal_id=sid, why=why, arm_price=arm['price'],
             ltp=ltp, stop=new_stop, waited_min=round(waited, 2), touched=arm['touched'])
        return out({'go': True, 'limit': None, 'stop': new_stop, 'fraction': 0.5,
                    'why': why, 'low': arm['low']})
    reason = ('best_entry_spike_wait' if spike else
              'best_entry_waiting_bounce' if arm['touched'] else 'best_entry_waiting_pullback')
    _log('best_entry_wait', throttle_key=('w', sid, reason), every=60.0, signal_id=sid,
         reason=reason, ltp=ltp, zone_ceiling=round(arm['ceiling'], 2), low=arm['low'],
         waited_min=round(waited, 2))
    return out({'go': False, 'block': reason})


def _stop(engine_stop, entry, low, a, done, cfg):
    cands = [low - cfg['low_buffer_atr'] * a]
    r5 = range5(done)
    if r5:
        cands.append(entry - cfg['noise_5m_mult'] * r5)
    if engine_stop and engine_stop > 0:
        cands.append(engine_stop)
    stop = min(cands)
    floor = entry * (1 - cfg['hard_cap'])
    stop = max(stop, floor)
    return round(stop, 2) if 0 < stop < entry else round(floor, 2)


def scale_lots(lots, verdict):
    """Half size on a no-pullback entry; a 1-lot trade stays 1 lot."""
    try:
        f = (verdict or {}).get('fraction', 1.0)
        if f >= 1.0 or lots <= 1:
            return lots
        return max(1, int(lots * f))
    except Exception:  # noqa: BLE001
        return lots


_reprice: dict = {}    # intent -> {'first', 'last', 'n'}


def entry_reprice(pos, row, status, age, ltp_fn):
    """Unfilled auto BUY: walk the limit up to the market instead of cancelling at 30 s.
    True = order kept alive (handled); False = old behaviour (cancel). Never raises."""
    try:
        return _entry_reprice(pos, row, status, age, ltp_fn)
    except Exception as exc:  # noqa: BLE001
        _log('best_entry_error', where='entry_reprice', error=str(exc)[:200])
        return False


def _entry_reprice(pos, row, status, age, ltp_fn):
    import math
    from . import angel_orders
    if mode() != 'live' or getattr(pos, 'kind', None) not in _kinds('entry_kinds'):
        return False
    cfg = config()
    if age >= cfg['reprice_window_sec'] or (status.filled_qty or 0) > 0:
        return False
    try:
        current = float(status.raw.get('price'))
    except (TypeError, ValueError, AttributeError):
        return False
    st = _reprice.setdefault(row['intent'], dict(first=current, last=None, n=0))
    now = time.monotonic()
    if st['last'] is not None and now - st['last'] < cfg['reprice_every_sec']:
        return True
    st['last'] = now
    ltp = ltp_fn(pos.exchange, pos.token) if callable(ltp_fn) else None
    if ltp is None or not math.isfinite(ltp) or ltp <= 0:
        return True
    cap = round(st['first'] * (1 + cfg['max_chase_pct'] / 100), 1)
    new = min(round(ltp * (1 + cfg['reprice_buffer_pct'] / 100), 1), cap)
    if new <= current:
        return True
    res = angel_orders.modify_order(status.order_id, price=new, side='BUY',
        quantity=row['quantity'], contract=dict(tradingsymbol=pos.symbol,
        symboltoken=pos.token, exchange=pos.exchange,
        producttype=angel_orders.product_for(pos)))
    st['n'] += 1
    _log('best_entry_entry_repriced', signal_id=pos.signal_id, order_id=status.order_id,
         attempt=st['n'], ltp=ltp, old_price=current, new_price=new, cap=cap,
         accepted=getattr(res, 'ok', None), reason=getattr(res, 'reason', None))
    return True


def after_submit(signal_id):
    with _lock:
        _arms.pop(signal_id, None)


# ------------------------------------------------------------------ exit
def filter_exit(pos, ltp, reason, now=None, samples=None):
    """Returns the reason to exit now, or None to hold. Never raises (fails to exit)."""
    try:
        return _filter(pos, ltp, reason, now or now_ist(), samples)
    except Exception as exc:  # noqa: BLE001 - fail to the original decision
        _log('best_entry_error', where='filter_exit', error=str(exc)[:200])
        return reason


def _filter(pos, ltp, reason, now, samples):
    if not reason or reason in NEVER_HOLD:
        return reason
    m = mode()
    if m == 'off' or getattr(pos, 'kind', None) not in _kinds('exit_kinds'):
        return reason
    entry = getattr(pos, 'entry_price', 0) or 0
    if entry <= 0 or ltp is None or ltp >= entry or getattr(pos, 'status', None) != 'open':
        return reason                         # profit side: unchanged
    cfg = config()
    sid = pos.signal_id
    if ltp <= entry * (1 - cfg['hard_cap']):
        return reason                         # hard cap always exits
    start = pos.entry_ts if isinstance(pos.entry_ts, datetime) else _dt(pos.entry_ts)
    hold = None
    if start is not None and now - start < timedelta(minutes=cfg['grace_min']):
        hold = 'grace'
    else:
        done, _ = candles(samples or [], now, minutes=10)
        a = atr1(done)
        if reason in STOP_REASONS:
            stop = (getattr(pos, 'structural_stop', None) if reason == 'structural_stop_breached'
                    else getattr(pos, 'effective_stop', None)) or 0
            closed_below = bool(done) and done[-1][4] < stop
            big = a is not None and ltp <= stop - cfg['breach_atr'] * a
            if not (closed_below or big or not done):
                hold = 'await_1m_close'
        else:
            key = (sid, reason)
            first, last = _persist.get(key, (now, now))
            if (now - last).total_seconds() > 30:     # signal went away; start again
                first = now
            _persist[key] = (first, now)
            if (now - first).total_seconds() < cfg['persist_sec']:
                hold = 'await_persist'
    if hold is None:
        _log('best_entry_exit_allowed', throttle_key=('xa', sid, reason), signal_id=sid,
             reason=reason, ltp=ltp, entry=entry)
        return reason
    _log('best_entry_exit_held', throttle_key=('xh', sid, reason, hold), signal_id=sid,
         reason=reason, hold=hold, ltp=ltp, entry=entry)
    return None if m == 'live' else reason


def _dt(v):
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
