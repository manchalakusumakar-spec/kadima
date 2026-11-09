"""WINDOW EXIT v1 (trader 2026-11-09): exits for trades entered 09:45-10:10 IST.

Trader's rules, chosen 09-Nov after the 21-26 Oct minute replay:
  Gamma Blast : keep the engine stop. Once the premium reaches T1 (+50%) the stop moves up
                to T1. After T2 (+100%) trail 25% below the peak. Close at 13:00.
  Scalp v2    : keep the engine stop. At T2 (+20%) book half and move the stop to entry.
                The rest runs (no ladder, no peak runner, no thesis exits, no no-progress
                timeout) and is closed at 13:00. A 1-lot scalp cannot book half: it exits
                whole at T2 (= the old +20% target).
Only positions whose entry time is in [09:45, 10:10) IST are touched. Everything else, and
the daily loss limit, EOD close, manual SL, BEST ENTRY and Celestial layers, is unchanged.

Config DATA_DIR/window_exit.json {"mode": "off|shadow|live"} (env KADIMA_WINDOW_EXIT wins).
off    : nothing happens.
shadow : logs window_exit_would_* events only; trading behaviour unchanged.
live   : the rules above replace the v2 ladder / runner / gamma ratchet for window trades.
Any error -> None, i.e. the existing exit logic runs.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import timezone, timedelta

from . import audit
from .config import DATA_DIR

MARK = 'WINDOW EXIT v1'
WINDOW = ((9, 45), (10, 10))     # entry time, start inclusive, end exclusive (IST)
CLOSE_AT = (13, 0)               # time exit (IST)
SCALP_T2 = 0.20                  # +20%: book half, stop to entry
GAMMA_T1 = 0.50                  # +50%: stop to T1
GAMMA_T2 = 1.00                  # +100%: trail starts
GAMMA_TRAIL = 0.25               # 25% below the peak
GAMMA_FALLBACK_STOP = 0.35       # only if a gamma position somehow has no structural stop
MODES = ('off', 'shadow', 'live')
IST = timezone(timedelta(hours=5, minutes=30))

_cfg = {'at': 0.0, 'value': None}
_lock = threading.RLock()
_mem: dict = {}


# ------------------------------------------------------------------ config
def mode():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = 'off'
        try:
            with open(os.path.join(DATA_DIR, 'window_exit.json')) as f:
                value = json.load(f).get('mode', 'off')
        except (OSError, ValueError, AttributeError):
            pass
        value = os.environ.get('KADIMA_WINDOW_EXIT', value)
        _cfg.update(at=now, value=value if value in MODES else 'off')
    return _cfg['value']


# ------------------------------------------------------------------ pure rules
def _ist(ts):
    if ts is None:
        return None
    return ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)


def in_window(entry_ts):
    t = _ist(entry_ts)
    if t is None:
        return False
    hm = (t.hour, t.minute)
    return WINDOW[0] <= hm < WINDOW[1]


def time_up(now):
    t = _ist(now)
    return t is not None and (t.hour, t.minute) >= CLOSE_AT


def scalp_stop(entry, peak, base, t2_hit=False):
    """(stop, t2_hit). Engine stop until T2 has traded; from then on the stop is entry."""
    hit = bool(t2_hit) or peak >= entry * (1 + SCALP_T2) - 1e-9
    return (max(base, entry) if hit else base), hit


def gamma_stop(entry, peak, base, previous=None):
    """Engine stop; T1 lock once +50% traded; 25% trail below the peak after +100%. Never down."""
    stop = base
    if peak >= entry * (1 + GAMMA_T1) - 1e-9:
        stop = max(stop, entry * (1 + GAMMA_T1))
    if peak >= entry * (1 + GAMMA_T2) - 1e-9:
        stop = max(stop, peak * (1 - GAMMA_TRAIL))
    return max(stop, previous or 0)


def half_quantity(remaining_qty, lot_size):
    """Whole lots only, at least 1 lot left. 0 = cannot book half."""
    try:
        lots = int(remaining_qty) // int(lot_size)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
    return (lots // 2) * int(lot_size) if lots >= 2 else 0


# ------------------------------------------------------------------ state
def _conn():
    from . import storage
    c = storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS window_exit_state (signal_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    return c


def _state(sid):
    with _lock:
        if sid not in _mem:
            row = _conn().execute('SELECT payload FROM window_exit_state WHERE signal_id=?', (int(sid),)).fetchone()
            _mem[sid] = json.loads(row[0]) if row else {}
        return _mem[sid]


def _save(sid, st):
    with _lock:
        _mem[sid] = st
        c = _conn()
        with c:
            c.execute('INSERT OR REPLACE INTO window_exit_state VALUES (?,?)', (int(sid), json.dumps(st)))


def _once(sid, st, key, event, **fields):
    logged = st.setdefault('logged', [])
    if key in logged:
        return
    logged.append(key)
    _save(sid, st)
    audit.log(event, signal_id=sid, mark=MARK, mode=mode(), **fields)


def _finite(*values):
    try:
        return all(v is not None and float(v) == float(v) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


def applies(pos):
    """A window trade of a covered kind (Scalp v2 needs its v2 plan)."""
    kind = getattr(pos, 'kind', None)
    if kind not in ('fast_momentum', 'gamma_blast') or not in_window(getattr(pos, 'entry_ts', None)):
        return False
    if kind == 'fast_momentum':
        try:
            from . import scalp_v2
            return bool(scalp_v2._plan(int(pos.signal_id)))
        except Exception:  # noqa: BLE001
            return False
    return True


def holds(pos):
    """True while the live window rules own this position (skip no-progress / sniper timeouts)."""
    try:
        return mode() == 'live' and applies(pos)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ executor hooks
def scalp_managed(pos, ltp, base, peak, now=None):
    """From scalp_v2.managed_stop. live: (base, stop, active, reason); shadow/off/other: None."""
    try:
        m = mode()
        if m == 'off' or not applies(pos):
            return None
        sid, entry = int(pos.signal_id), pos.entry_price
        if not _finite(entry, ltp, base, peak):
            return None
        from .utils import now_ist
        now = now or now_ist()
        st = _state(sid)
        stop, hit = scalp_stop(entry, peak, base, st.get('t2_hit'))
        if hit and not st.get('t2_hit'):
            st['t2_hit'] = True
            _save(sid, st)
            audit.log('window_exit_scalp_t2', signal_id=sid, mark=MARK, mode=m, ltp=round(ltp, 2), entry=entry)
        one_lot = half_quantity(pos.remaining_qty, pos.lot_size) <= 0 and not st.get('partial')
        reason = None
        if time_up(now):
            reason = 'window_exit_1300'
        elif hit and one_lot:
            reason = 'window_exit_t2_one_lot'
        elif hit and ltp < stop - 1e-9:
            reason = 'window_exit_entry_stop'
        if m == 'shadow':
            if reason:
                _once(sid, st, reason, 'window_exit_would_exit', reason=reason, ltp=round(ltp, 2))
            elif hit:
                _once(sid, st, 'stop_entry', 'window_exit_would_move_stop', stop=round(stop, 2))
            return None
        return base, stop, hit, reason
    except Exception as exc:  # noqa: BLE001
        audit.log('window_exit_error', where='scalp', error=type(exc).__name__)
        return None


def scalp_partial(executor, pos, ltp):
    """From scalp_v2.maybe_partial when holds(pos): book half once at T2. Never raises."""
    try:
        sid, entry = int(pos.signal_id), pos.entry_price
        st = _state(sid)
        if st.get('partial') or not _finite(entry, ltp) or getattr(pos, 'status', None) != 'open':
            return None
        if not (st.get('t2_hit') or ltp >= entry * (1 + SCALP_T2) - 1e-9):
            return None
        qty = half_quantity(pos.remaining_qty, pos.lot_size)
        if qty <= 0:
            return None   # 1 lot: scalp_managed exits it whole at T2
        from . import execution_ledger
        if execution_ledger.pending(sid):
            return None
        from .auto_executor import LIMIT_ORDER_BUFFER_PCT
        limit = max(0.05, round(ltp * (1 - LIMIT_ORDER_BUFFER_PCT / 100.0), 1))
        result = execution_ledger.submit(pos, 'SELL', qty, limit)
        st['partial_attempts'] = st.get('partial_attempts', 0) + 1
        if (result or {}).get('ok') or (result or {}).get('pending'):
            st['partial'] = 'submitted'
        elif st['partial_attempts'] >= 2:
            st['partial'] = 'failed'
        _save(sid, st)
        audit.log('window_exit_partial_booked', signal_id=sid, mark=MARK, ok=bool((result or {}).get('ok')),
                  reason=(result or {}).get('reason'), quantity=qty, limit=limit, ltp=round(ltp, 2))
        return result
    except Exception as exc:  # noqa: BLE001
        audit.log('window_exit_error', where='partial', error=type(exc).__name__)
        return None


def gamma_managed(pos, ltp, now=None):
    """From the executor for gamma_blast positions. live: (base, stop, active, reason); else None."""
    try:
        m = mode()
        if m == 'off' or getattr(pos, 'kind', None) != 'gamma_blast' or not applies(pos):
            return None
        sid, entry = int(pos.signal_id), pos.entry_price
        if not _finite(entry, ltp):
            return None
        from .utils import now_ist
        now = now or now_ist()
        st = _state(sid)
        if not _finite(st.get('base')):   # the engine stop as first seen; later trails don't move it
            base = pos.structural_stop if _finite(pos.structural_stop) and pos.structural_stop < entry \
                else entry * (1 - GAMMA_FALLBACK_STOP)
            st['base'] = base
            _save(sid, st)
        base = st['base']
        peak = max(getattr(pos, 'peak_price', None) or entry, ltp)
        stop = gamma_stop(entry, peak, base, st.get('stop'))
        if stop != st.get('stop'):
            st['stop'] = stop
            _save(sid, st)
            if m == 'live':
                audit.log('window_exit_gamma_stop', signal_id=sid, mark=MARK, stop=round(stop, 2), peak=round(peak, 2))
        reason = None
        if time_up(now):
            reason = 'window_exit_1300'
        elif stop > base and ltp < stop - 1e-9:
            reason = 'window_exit_gamma_lock'
        if m == 'shadow':
            if reason:
                _once(sid, st, reason, 'window_exit_would_exit', reason=reason, ltp=round(ltp, 2))
            elif stop > base:
                _once(sid, st, 'stop_%.0f' % stop, 'window_exit_would_move_stop', stop=round(stop, 2))
            return None
        return base, stop, stop > base, reason
    except Exception as exc:  # noqa: BLE001
        audit.log('window_exit_error', where='gamma', error=type(exc).__name__)
        return None
