"""MANUAL FORECAST STOP v1 (trader 2026-10-26 23:58 IST).

"Manual trades: a warning when your trade goes against the day trend ... forecast the loss in
advance and exit if the trade is going to hit 35% loss if the forecast is supported by all
other parameters." / "i need a dynamic stoploss that can forecast and decide."

Incident: manual NIFTY 22650 PE, 6 lots, bought 81.63 at 13:25 on an UP day (NIFTY +153);
KADIMA's manual hard stop sold it at 56.00 (-31%, -Rs 9,997) at 14:09.

For KADIMA-protected manual positions (kind MANUAL_PROTECTED):

WARNING  once per position when its side is against the index day trend (CE on a DOWN day,
         PE on an UP day): Telegram alert + audit line. Never exits by itself.

FORECAST EXIT  exits BEFORE the hard stop when every parameter below agrees:
  P1 loss now >= MIN_LOSS_PCT (no exits on noise)
  P2 projection: the premium's own slope over the last SLOPE_MIN minutes, carried forward
     HORIZON_MIN minutes, reaches the hard-stop level (entry x (1 - 35%))
  P3 premium below its 1-min EMA9 for 2 completed candles (still falling, not basing)
  P4 day trend is against the position
  P5 index spot on the losing side of VWAP          (if a fresh VWAP is published)
  P6 hourly forecast band middle on the losing side  (if a live forecast point exists)
  P1-P4 must be available and agree; P5/P6 must agree whenever they are available.
The existing 35% hard stop and profit trail stay as they are (backstops).

Config DATA_DIR/manual_forecast.json {"mode": "off|shadow|live", ...}, re-read every 30 s.
shadow logs what it would do. Any error -> no forecast exit (hard stop still protects).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

from . import audit
from .config import DATA_DIR
from .utils import now_ist

MARK = 'MANUAL FORECAST STOP v1'
DEFAULTS = {
    'mode': 'off',
    'warn': 'live',
    'min_loss_pct': 12.0,
    'hard_pct': 35.0,
    'slope_min': 10.0,
    'horizon_min': 30.0,
    'min_samples': 8,
}
_cfg = {'at': 0.0, 'value': None}
_warned: set = set()
_logged: dict = {}


def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'manual_forecast.json')) as f:
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
        for k in ('mode', 'warn'):
            if value[k] not in ('off', 'shadow', 'live'):
                value[k] = 'off'
        _cfg.update(at=now, value=value)
    return _cfg['value']


def _log(event, key=None, every=60.0, **fields):
    if key is not None:
        t = time.monotonic()
        if t - _logged.get(key, -1e9) < every:
            return
        _logged[key] = t
    try:
        audit.log(event, mode=config()['mode'], **fields)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ inputs
def _trend_label(index):
    from . import trend_rules
    st = trend_rules._trend.get(index)
    if st and st.get('day') == now_ist().date().isoformat():
        return st.get('label')
    return None


def _structure(index, now):
    try:
        from . import celestial
        h = celestial.health('structure:' + index)
        if h and celestial.fresh(h.get('ts'), now) and celestial.fresh(h.get('spot_at'), now):
            return h.get('spot'), h.get('vwap')
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _forecast_mid(index, now):
    try:
        from . import trend_rules
        b = trend_rules.forecast_point(index, now)
        if b:
            return (b['upper'] + b['lower']) / 2
    except Exception:  # noqa: BLE001
        pass
    return None


def slope_per_min(samples, now, minutes):
    """Least-squares premium slope (Rs/min) over the last `minutes`."""
    pts = [((ts - now).total_seconds() / 60.0, px) for ts, px in samples
           if ts is not None and px and now - timedelta(minutes=minutes) <= ts <= now]
    if len(pts) < 2:
        return None, len(pts)
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den <= 0:
        return None, n
    return sum((x - mx) * (y - my) for x, y in pts) / den, n


def below_ema(samples, now, need=2):
    """True when the last `need` completed 1-min closes are below a 1-min EMA9."""
    closes = {}
    cur = now.replace(second=0, microsecond=0)
    for ts, px in samples:
        if ts is None or not px:
            continue
        m = ts.replace(second=0, microsecond=0)
        if m < cur:
            closes[m] = px
    seq = [closes[k] for k in sorted(closes)]
    if len(seq) < need + 3:
        return None
    ema, flags = None, []
    for c in seq:
        ema = c if ema is None else 0.2 * c + 0.8 * ema
        flags.append(c < ema)
    return all(flags[-need:])


# ------------------------------------------------------------------ decisions
def assess(pos, ltp, samples, now=None):
    """Returns dict(params..., exit=bool, against=bool|None). Never raises."""
    now = now or now_ist()
    cfg = config()
    out = dict(exit=False, against=None)
    try:
        entry = float(pos.entry_price)
        side = pos.direction if getattr(pos, 'direction', None) in ('CE', 'PE') else (
            'PE' if str(pos.symbol).endswith('PE') else 'CE')
        label = _trend_label(pos.index)
        out['trend'] = label
        out['against'] = (label == 'UP' and side == 'PE') or (label == 'DOWN' and side == 'CE')
        loss = (entry - ltp) / entry * 100
        out['loss_pct'] = round(loss, 2)
        p1 = loss >= cfg['min_loss_pct'] and loss < cfg['hard_pct']
        slope, n = slope_per_min(samples, now, cfg['slope_min'])
        hard_level = entry * (1 - cfg['hard_pct'] / 100)
        projected = ltp + slope * cfg['horizon_min'] if slope is not None else None
        out['projected'] = round(projected, 2) if projected is not None else None
        p2 = n >= cfg['min_samples'] and projected is not None and projected <= hard_level
        p3 = below_ema(samples, now)
        p4 = bool(out['against'])
        spot, vwap = _structure(pos.index, now)
        p5 = None
        if spot and vwap:
            p5 = (side == 'CE' and spot < vwap) or (side == 'PE' and spot > vwap)
        mid = _forecast_mid(pos.index, now)
        p6 = None
        if spot and mid:
            p6 = (side == 'CE' and mid < spot) or (side == 'PE' and mid > spot)
        out.update(p1=p1, p2=p2, p3=p3, p4=p4, p5=p5, p6=p6)
        out['exit'] = bool(p1 and p2 and p3 and p4 and p5 is not False and p6 is not False)
    except Exception as exc:  # noqa: BLE001
        _log('manual_forecast_error', error=str(exc)[:200])
        out['exit'] = False
    return out


def check(pos, ltp, samples, now=None):
    """Hook for check_exits (manual positions, after the existing manual decision found no
    reason). Returns 'manual_forecast_stop' to exit now, else None."""
    try:
        cfg = config()
        if cfg['mode'] == 'off' and cfg['warn'] == 'off':
            return None
        if not getattr(pos, 'entry_price', None) or not ltp or getattr(pos, 'status', None) != 'open':
            return None
        a = assess(pos, ltp, samples, now)
        sid = pos.signal_id
        if a.get('against') and sid not in _warned and cfg['warn'] != 'off':
            _warned.add(sid)
            _log('manual_counter_trend_warning', signal_id=sid, symbol=pos.symbol,
                 trend=a.get('trend'))
            if cfg['warn'] == 'live':
                try:
                    from . import alerts
                    alerts.enqueue(f"⚠️ Manual {pos.symbol} is AGAINST the {pos.index} day "
                                   f"trend ({a.get('trend')}). KADIMA will exit early if the loss "
                                   f"forecast confirms.", key=f'manual-ct:{sid}')
                except Exception:  # noqa: BLE001
                    pass
        if cfg['mode'] == 'off':
            return None
        if a.get('p1'):
            _log('manual_forecast_assess', key=('a', sid), signal_id=sid, ltp=ltp, **a)
        if not a.get('exit'):
            return None
        _log('manual_forecast_exit', signal_id=sid, symbol=pos.symbol, ltp=ltp, **a)
        return 'manual_forecast_stop' if cfg['mode'] == 'live' else None
    except Exception as exc:  # noqa: BLE001
        _log('manual_forecast_error', where='check', error=str(exc)[:200])
        return None
