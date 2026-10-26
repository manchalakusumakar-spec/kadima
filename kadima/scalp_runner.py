"""PEAK RUNNER v1 for Scalping v2 (trader 2026-10-27 00:15 IST).

"amend [the fixed +20% target] as trailing stoploss of 20.00 profit" / "it should continuously
forecast and should be able to reach the peak; once the peak is reached as per forecast of
latest 15 seconds, it should exit."

Incident: 26-Oct NIFTY 22450 CE scalp. The fixed +20% target would have sold at ~198 while the
premium went on to 366.80.

Below +20%  : unchanged (v2 ladder: breakeven at +1R, +6% lock at +12%, ATR trail).
At +20%     : the fixed target exit is OFF. The position becomes a runner:
  floor     : stop never below entry + 20% (the old target is now the minimum result)
  backstop  : stop never below entry + BACKSTOP_KEEP x (peak - entry) (a wide trail in case
              quotes stop arriving)
  peak exit : every monitor cycle (every FAST_SEC while a runner is open) the premium's
              slope over the latest WINDOW_SEC (15 s) is fitted. Exit when, on CONFIRM
              consecutive checks:
                - the 15-s forecast (ltp + slope x 15 s) is below the peak minus noise
                  (no new high expected), and
                - the slope is negative, and
                - the premium is at least `noise` below the peak.
              noise = max(NOISE_MIN_PCT of the peak, 2 x median 2-s move over the last minute).
Config DATA_DIR/scalp_runner.json {"mode": "off|shadow|live"}; off/shadow = old +20% target.
Any error -> old +20% target behaviour.
"""
from __future__ import annotations

import json
import os
import threading
import time
from statistics import median

from . import audit
from .config import DATA_DIR

MARK = 'PEAK RUNNER v1'
DEFAULTS = {
    'mode': 'off',
    'activate_pct': 20.0,
    'floor_pct': 20.0,
    'backstop_keep': 0.5,
    'window_sec': 15.0,
    'confirm': 2,
    'noise_min_pct': 1.0,
    'fast_sec': 2.0,
}
_cfg = {'at': 0.0, 'value': None}
_lock = threading.Lock()
_state: dict = {}     # signal_id -> {'buf': [(t, px)], 'hits': int, 'active': bool, 'logged': bool}


def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'scalp_runner.json')) as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k not in value:
                    continue
                d = DEFAULTS[k]
                if isinstance(d, str) and isinstance(v, str):
                    value[k] = v
                elif not isinstance(d, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    value[k] = float(v) if isinstance(d, float) else int(v)
        except (OSError, ValueError):
            pass
        if value['mode'] not in ('off', 'shadow', 'live'):
            value['mode'] = 'off'
        _cfg.update(at=now, value=value)
    return _cfg['value']


def _log(event, **fields):
    try:
        audit.log(event, mode=config()['mode'], **fields)
    except Exception:  # noqa: BLE001
        pass


def slope(points):
    """Least-squares slope (Rs per second) of [(t, px)]; None with < 3 points."""
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(p for _, p in points) / n
    den = sum((t - mx) ** 2 for t, _ in points)
    if den <= 0:
        return None
    return sum((t - mx) * (p - my) for t, p in points) / den


def step(sid, entry, peak, ltp, stop, now=None):
    """Called from scalp_v2.managed_stop on every monitor cycle.
    Returns (stop, reason, handled). handled=False -> caller keeps the old +20% target."""
    cfg = config()
    if cfg['mode'] == 'off' or not entry or not ltp:
        return stop, None, False
    now = time.monotonic() if now is None else now
    with _lock:
        st = _state.setdefault(sid, dict(buf=[], hits=0, active=False, logged=False))
        st['buf'].append((now, ltp))
        st['buf'] = [(t, p) for t, p in st['buf'] if now - t <= 120]
        if not st['active'] and peak >= entry * (1 + cfg['activate_pct'] / 100) - 1e-9:
            st['active'] = True
            _log('scalp_runner_active', signal_id=sid, entry=entry, peak=peak, ltp=ltp)
        if not st['active']:
            return stop, None, cfg['mode'] == 'live'
        floor = entry * (1 + cfg['floor_pct'] / 100)
        backstop = entry + cfg['backstop_keep'] * (peak - entry)
        new_stop = max(stop or 0, floor, backstop)
        w = cfg['window_sec']
        recent = [(t, p) for t, p in st['buf'] if now - t <= w]
        s1 = slope(recent)
        last_min = [p for t, p in st['buf'] if now - t <= 60]
        moves = [abs(b - a) for a, b in zip(last_min, last_min[1:])]
        noise = max(peak * cfg['noise_min_pct'] / 100, 2 * median(moves) if moves else 0.0)
        reason = None
        if ltp < new_stop - 1e-9:
            reason = 'scalp_runner_floor' if new_stop == floor else 'scalp_runner_backstop'
        elif s1 is not None:
            forecast = ltp + s1 * w
            turned = forecast < peak - noise and s1 < 0 and peak - ltp >= noise
            st['hits'] = st['hits'] + 1 if turned else 0
            if st['hits'] >= cfg['confirm']:
                reason = 'scalp_runner_peak_forecast'
                _log('scalp_runner_peak', signal_id=sid, entry=entry, peak=peak, ltp=ltp,
                     forecast=round(forecast, 2), slope_per_sec=round(s1, 3), noise=round(noise, 2))
        if cfg['mode'] == 'shadow':
            if reason and not st['logged']:
                st['logged'] = True
                _log('scalp_runner_would_exit', signal_id=sid, reason=reason, ltp=ltp, peak=peak)
            return stop, None, False
        return new_stop, reason, True


def active(sid):
    with _lock:
        st = _state.get(sid)
        return bool(st and st.get('active')) and config()['mode'] == 'live'


def interval(executor):
    """Monitor-loop interval: FAST_SEC while a live runner is open, else a large number."""
    try:
        cfg = config()
        if cfg['mode'] != 'live':
            return 1e9
        with _lock:
            active = {k for k, v in _state.items() if v.get('active')}
        for p in list(executor.state.positions.values()):
            if (getattr(p, 'kind', None) == 'fast_momentum' and p.status == 'open'
                    and p.remaining_qty > 0 and p.signal_id in active):
                return cfg['fast_sec']
    except Exception:  # noqa: BLE001
        pass
    return 1e9
