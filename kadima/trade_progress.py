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
_SYM_WEEKLY = re.compile(r'^([A-Z&\-]+?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$')   # SENSEX26N0576400CE
_WMON = {**{str(i): i for i in range(1, 10)}, 'O': 10, 'N': 11, 'D': 12}
_MON = {m: i for i, m in enumerate(
    ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'), 1)}
SESSION_END = (15, 30)
IV_HORIZON_MIN = 60.0
TRADING_MIN_PER_YEAR = 375 * 252


def parse_symbol(symbol):
    """'NIFTY03NOV2622450CE' -> ('NIFTY', expiry datetime 15:30 IST, 22450.0, 'CE') or None."""
    m = _SYM.match(str(symbol or ''))
    try:
        if m:
            und, dd, mon, yy, strike, side = m.groups()
            if mon not in _MON:
                return None
            exp = datetime(2000 + int(yy), _MON[mon], int(dd), *SESSION_END, tzinfo=IST)
        else:
            w = _SYM_WEEKLY.match(str(symbol or ''))
            if not w:
                return None
            und, yy, mon, dd, strike, side = w.groups()
            exp = datetime(2000 + int(yy), _WMON[mon], int(dd), *SESSION_END, tzinfo=IST)
    except (ValueError, KeyError):
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


# ------------------------------------------------------------------ v1.3: paper progress
def paper(now=None, limit=40):
    """TRADE PROGRESS v1.3 (trader 2026-11-03 10:28: "i want to know the live status of these also").
    Live progress for today's CONFIRMED suggestions that were NOT traded, from the suggested entry,
    plan stop, last observed quote and session high. Display only. Never raises."""
    try:
        from . import trade_audit
        from .utils import now_ist
        now = now or now_ist()
        rep = trade_audit.report(now.date().isoformat(), 'CONFIRMED', 'ALL', None, 200, 0)
        out = []
        for r in rep.get('rows') or []:
            if r.get('execution_status') != 'SUGGESTED' or len(out) >= limit:
                continue
            plan = r.get('plan') or {}
            entry = plan.get('entry') or plan.get('entry_reference')
            ltp, stop = r.get('ltp'), plan.get('stop')
            parsed = parse_symbol(r.get('symbol'))
            if not parsed or not _finite(entry, ltp) or entry <= 0 or ltp <= 0:
                continue
            und, exp, strike, side = parsed
            index = r.get('underlying') or und
            spot = _spot(index, now)
            days = max(0.0, (exp - now).total_seconds() / 86400.0)
            peak = r.get('high_ltp') if _finite(r.get('high_ltp')) else ltp
            g = project(entry=float(entry), ltp=float(ltp), peak=float(peak),
                        stop=float(stop) if _finite(stop) else None, spot=spot, strike=strike,
                        side=side, days=days, index=index, now=now)
            res = r.get('observed_result')
            if res == 'SL_OBSERVED':
                g['status'] = 'STOP HIT'
            q = None
            try:
                q = datetime.fromisoformat(r['quote_at']) if r.get('quote_at') else None
            except (TypeError, ValueError):
                q = None
            g['quote_age_sec'] = int((now - q).total_seconds()) if q else None
            sug = r.get('suggested_at')
            try:
                g['held_sec'] = int((now - datetime.fromisoformat(sug)).total_seconds()) if sug else None
            except (TypeError, ValueError):
                g['held_sec'] = None
            g.update(t1=plan.get('t1'), t2=plan.get('t2'), observed=res,
                     points=round(float(ltp) - float(entry), 2))
            out.append(dict(audit_id=r.get('audit_id'), symbol=r.get('symbol'), suggested_at=sug,
                            strategy_label=r.get('strategy_label') or r.get('strategy'),
                            paper=True, ltp=ltp, progress=g))
        return out
    except Exception:  # noqa: BLE001
        return []


def _spot(index, now):
    try:
        from . import celestial
        h = celestial.health('structure:' + index)
        if h and celestial.fresh(h.get('spot_at'), now, 60):
            return float(h['spot'])
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import live_forecast
        s = (live_forecast.LIVE.get(index) or {}).get('spot')
        return float(s) if _finite(s) else None
    except Exception:  # noqa: BLE001
        return None
