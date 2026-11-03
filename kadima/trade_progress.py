"""TRADE PROGRESS v1 (trader 2026-11-03 09:16 IST). Display only.

"In KADIMA Celestial, Gamma Blast Top 20, while a trade is live, provide a realtime status of the
percentage of the trade achieved and remaining; this may be dynamic in nature."

For every open position, every dashboard refresh (4 s):

  target premium (dynamic) = where this option's premium is projected to peak:
    1. favourable index move still expected = the PULSE live-forecast line's furthest point in the
       trade's direction (CE: highest, PE: lowest) from now to the end of the line;
    2. if no forecast line exists (stocks, or forecast down), the 1-sigma move the option's own
       implied volatility gives over the next IV_HORIZON_MIN minutes (capped at the session end);
    3. move -> premium with the option's own delta and gamma (IV solved from the live premium):
         target = ltp + delta x move + 0.5 x gamma x move^2   (premium change, CE and PE alike)
    4. never below the peak so far or the live price: past the target, the target moves up.
  achieved %  = (ltp - entry) / (target - entry)        remaining % = 100 - achieved %
  below entry -> stop_used % = (entry - ltp) / (entry - stop)
  status: BUILDING (<40%), MIDWAY (40-80%), NEAR TARGET (>=80%), BEYOND FORECAST (peak above the
          forecast target), PEAK FORMING (scalp runner sees the top), UNDER WATER, NEAR STOP
          (within 20% of the entry-to-stop distance from the stop).
Never places, sizes, moves or exits anything. Any error -> fields absent; the row still renders.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from .config import IST

MARK = 'TRADE PROGRESS v1'
_SYM = re.compile(r'^([A-Z&\-]+?)(\d{2})([A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$')
_MON = {m: i for i, m in enumerate(
    ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'), 1)}
SESSION_END = (15, 30)
IV_HORIZON_MIN = 60.0
TRADING_MIN_PER_YEAR = 375 * 252


def parse_symbol(symbol):
    """'NIFTY03NOV2622450CE' -> ('NIFTY', expiry datetime 15:30 IST, 22450.0, 'CE') or None."""
    m = _SYM.match(str(symbol or ''))
    if not m:
        return None
    und, dd, mon, yy, strike, side = m.groups()
    if mon not in _MON:
        return None
    try:
        exp = datetime(2000 + int(yy), _MON[mon], int(dd), *SESSION_END, tzinfo=IST)
    except ValueError:
        return None
    return und, exp, float(strike), side


def _finite(*v):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v)


def forecast_move(index, side, spot, now):
    """Favourable index points the PULSE live forecast still expects, or None."""
    try:
        from . import live_forecast
        snap = live_forecast.LIVE.get(index) or {}
        line = ((snap.get('pulse') or {}).get('line')) or []
        pts = []
        for p in line:
            ts = datetime.fromisoformat(p['ts'])
            if ts >= now:
                pts.append(float(p['v']))
        if not pts or not _finite(spot):
            return None
        best = max(pts) - spot if side == 'CE' else spot - min(pts)
        return max(0.0, best)
    except Exception:  # noqa: BLE001
        return None


def iv_move(spot, iv_pct, now):
    """1-sigma index move over the next IV_HORIZON_MIN minutes (or to the close) from the option's IV."""
    end = now.replace(hour=SESSION_END[0], minute=SESSION_END[1], second=0, microsecond=0)
    mins = min(IV_HORIZON_MIN, max(0.0, (end - now).total_seconds() / 60.0))
    if not _finite(spot, iv_pct) or mins <= 0 or iv_pct <= 0:
        return 0.0
    return spot * (iv_pct / 100.0) * math.sqrt(mins / TRADING_MIN_PER_YEAR)


def project(*, entry, ltp, peak, stop, spot, strike, side, days, index, now):
    """Pure-ish core (reads only the live forecast line). Returns a dict for the dashboard."""
    from . import greeks
    out = dict(entry=entry, ltp=ltp, peak=max(peak or entry, ltp), stop=stop)
    iv, g = (None, None)
    if _finite(spot, strike, ltp, days) and spot > 0 and ltp > 0:
        iv, g = greeks.implied_vol_and_greeks(spot, strike, ltp, max(days, 0.01), side)
    move, basis = forecast_move(index, side, spot, now), 'forecast'
    if move is None:
        move, basis = (iv_move(spot, iv, now) if iv else 0.0), 'iv'
    target = None
    if g and _finite(g.get('delta'), g.get('gamma')):
        d = abs(g['delta'])
        target = ltp + d * move + 0.5 * g['gamma'] * move * move
    if target is None:
        basis = 'peak_only'
        target = out['peak']
    beyond = out['peak'] > target + 1e-9
    target = max(target, out['peak'], ltp)
    out.update(target=round(target, 2), basis=basis, index_move_pts=round(move, 1),
               iv_pct=round(iv, 1) if iv else None,
               delta=round(abs(g['delta']), 3) if g else None)
    span = target - entry
    if ltp >= entry:
        achieved = 100.0 if span <= 0 else max(0.0, min(100.0, (ltp - entry) / span * 100.0))
        out.update(achieved_pct=round(achieved, 1), remaining_pct=round(100.0 - achieved, 1),
                   stop_used_pct=0.0)
    else:
        risk = entry - stop if _finite(stop) and stop < entry else None
        used = None if not risk else max(0.0, min(100.0, (entry - ltp) / risk * 100.0))
        out.update(achieved_pct=0.0, remaining_pct=100.0, stop_used_pct=round(used, 1) if used is not None else None)
    out['gain_pct'] = round((ltp - entry) / entry * 100.0, 2) if entry else None
    out['status'] = _status(out, beyond)
    return out


def _status(o, beyond):
    entry, ltp, stop = o['entry'], o['ltp'], o['stop']
    if _finite(stop) and stop < entry and ltp - stop <= 0.2 * (entry - stop):
        return 'NEAR STOP'
    if ltp < entry:
        return 'UNDER WATER'
    if beyond:
        return 'BEYOND FORECAST'
    a = o['achieved_pct']
    return 'NEAR TARGET' if a >= 80 else 'MIDWAY' if a >= 40 else 'BUILDING'


def for_position(p, ltp, now=None):
    """Dashboard fields for one open position (an auto_executor Position). Never raises."""
    try:
        from .utils import now_ist
        now = now or now_ist()
        if not _finite(ltp, p.entry_price) or p.entry_price <= 0 or ltp <= 0:
            return None
        parsed = parse_symbol(p.symbol)
        if not parsed:
            return None
        und, exp, strike, side = parsed
        index = getattr(p, 'index', None) or und
        spot = None
        try:
            from . import celestial
            h = celestial.health('structure:' + index)
            if h and celestial.fresh(h.get('spot_at'), now, 60):
                spot = float(h['spot'])
        except Exception:  # noqa: BLE001
            spot = None
        if spot is None:
            try:
                from . import live_forecast
                s = (live_forecast.LIVE.get(index) or {}).get('spot')
                spot = float(s) if _finite(s) else None
            except Exception:  # noqa: BLE001
                spot = None
        days = max(0.0, (exp - now).total_seconds() / 86400.0)
        out = project(entry=float(p.entry_price), ltp=float(ltp), peak=p.peak_price,
                      stop=p.effective_stop, spot=spot, strike=strike, side=side, days=days,
                      index=index, now=now)
        try:
            from . import scalp_runner
            st = scalp_runner._state.get(int(p.signal_id)) or {}
            if st.get('active') and st.get('hits', 0) >= 1 and ltp >= p.entry_price:
                out['status'] = 'PEAK FORMING'
        except Exception:  # noqa: BLE001
            pass
        ets = getattr(p, 'entry_ts', None)
        out['entry_ts'] = ets.isoformat() if hasattr(ets, 'isoformat') else ets
        out['held_sec'] = int((now - ets).total_seconds()) if hasattr(ets, 'tzinfo') and ets else None
        qty = getattr(p, 'remaining_qty', 0) or 0
        lot = getattr(p, 'lot_size', None) or None
        out['pnl_inr'] = round((ltp - p.entry_price) * qty, 2)
        out['pnl_per_lot_inr'] = round((ltp - p.entry_price) * lot, 2) if lot else None
        out['target_pnl_inr'] = round((out['target'] - p.entry_price) * qty, 2)
        return out
    except Exception:  # noqa: BLE001
        return None
