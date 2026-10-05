"""Shadow comparison for the VWAP and days-to-expiry fixes. Observe only.

Runs beside the live scoring and changes nothing it reads: no score, tier,
signal, strike choice or order depends on this module. On every completed
5-minute index bar it writes one SHADOW_COMPARE audit event recording what
Bull Shield and the option Greeks would look like under the two fixes:

1. VWAP from the index's current-month FUTURE. Index ticks carry no volume,
   so today's "VWAP" is the bar's own close and above_vwap is always False.
   The future has real traded volume. Future price is compared with future
   VWAP (never spot with future VWAP: the futures premium would bias it).
   On the future's own expiry day the next month is used. If the future
   has no usable volume the VWAP factor sits out (no rescaling).
2. Exact time to expiry (minutes to the expiry-day close, floor 15 min)
   instead of whole days + 0.5. Logged as old vs new ATM IV and delta.

Disable without a deploy: KADIMA_SHADOW_COMPARE=0 in the service env.
The scoring weights mirror engine._score() on purpose (a copy, not a
refactor of the live path); if _score() changes, update this too.
"""
from __future__ import annotations

import math
import os
from dataclasses import replace
from datetime import datetime, timedelta

from . import audit, config, storage
from .config import IST

MIN_EXPIRY_MINUTES = 15.0          # floor for time to expiry on expiry day
MIN_FUTURE_BAR_VOLUME = 1          # a 5-min future bar with no trades -> sit out
VOL_AVG_BARS = 20                  # same window indicators.compute() uses


def enabled() -> bool:
    return os.environ.get("KADIMA_SHADOW_COMPARE", "1") == "1"


# ---------------------------------------------------------------- futures
def future_for(scrips, index, today):
    """Nearest index future that does not expire today. Returns a dict with
    token/symbol/expiry/exchange, or None if the scrip master has none."""
    best = None
    for r in scrips.rows:
        if (r.get("exch_seg") != index.opt_exchange
                or r.get("instrumenttype") != "FUTIDX"
                or r.get("name") != index.opt_symbol_root or not r.get("token")):
            continue
        try:
            expiry = datetime.strptime(r.get("expiry", ""), "%d%b%Y").date()
        except ValueError:
            continue
        if expiry <= today:            # roll on the future's own expiry day
            continue
        if best is None or expiry < best[0]:
            best = (expiry, r)
    if best is None:
        return None
    expiry, r = best
    return dict(token=str(r["token"]), symbol=r.get("symbol"),
                expiry=expiry.isoformat(), exchange=index.opt_exchange)


def future_vwap_view(builder, token):
    """(future_close, future_session_vwap, last_bar_volume, avg_bar_volume)
    from the future's own candle builder and its closed 5-min bars."""
    if builder is None:
        return None, None, None, None
    live = builder.live(5)
    close = live.close if live else None
    vwap = builder.session_vwap()
    bars = storage.recent_candles(token, 5, limit=VOL_AVG_BARS + 1)
    vols = [int(b["volume"] or 0) for b in bars]
    last = vols[-1] if vols else None
    prior = vols[-(VOL_AVG_BARS + 1):-1] if len(vols) > 1 else []
    avg = (sum(prior) / len(prior)) if prior else None
    return close, vwap, last, avg


# ------------------------------------------------------------ expiry time
def exact_days_to_expiry(expiry_str, now):
    """Days (fractional) to the expiry-day close, never below 15 minutes."""
    try:
        d = datetime.strptime(expiry_str, "%d%b%Y")
    except (TypeError, ValueError):
        return None
    close_h, close_m = config.MARKET_CLOSE
    expiry_at = d.replace(hour=close_h, minute=close_m, tzinfo=IST)
    minutes = (expiry_at - now).total_seconds() / 60.0
    if minutes < -1:                   # stale expiry from the scrip master
        return None
    return max(minutes, MIN_EXPIRY_MINUTES) / 1440.0


def atm_greeks(chain, spot, days):
    """ATM CE/PE IV and delta at the given days-to-expiry. Pure: never
    writes Greeks back onto the shared quote objects."""
    from .greeks import implied_vol_and_greeks
    out = {}
    if not chain or not chain.quotes or not spot or not days:
        return out
    atm = min({q.strike for q in chain.quotes.values()}, key=lambda k: abs(k - spot))
    for q in chain.quotes.values():
        if q.strike != atm or not q.ltp or q.ltp <= 0:
            continue
        iv, g = implied_vol_and_greeks(spot=spot, strike=q.strike, premium=q.ltp,
                                       days=days, opt_type=q.opt_type)
        out[q.opt_type] = dict(strike=q.strike, ltp=q.ltp,
                               iv=round(iv, 2) if iv else None,
                               delta=round(g["delta"], 3) if g else None)
    return out


# ---------------------------------------------------------------- scoring
def bull_bear(snap, chain, weights):
    """Copy of engine._score()'s weighting, as a pure function."""
    bull = bear = 0.0
    if snap.above_vwap is not None:
        if snap.above_vwap:
            bull += weights["vwap"]
        else:
            bear += weights["vwap"]
    if snap.ema_bull is not None:
        if snap.ema_bull:
            bull += weights["ema_cross"]
        else:
            bear += weights["ema_cross"]
    if snap.rsi is not None:
        if snap.rsi > 50:
            bull += weights["rsi_50"]
        else:
            bear += weights["rsi_50"]
    if snap.avg_vol and snap.vol > snap.avg_vol * 1.5:
        if snap.above_vwap:
            bull += weights["volume_spike"]
        else:
            bear += weights["volume_spike"]
    vel = chain.velocity.alerts() if chain else []
    if vel:
        ce_tokens = {q.token for q in chain.quotes.values() if q.opt_type == "CE"}
        ce_hits = sum(1 for v in vel if v["token"] in ce_tokens)
        pe_hits = len(vel) - ce_hits
        if ce_hits > pe_hits:
            bull += weights["oi_shift"]
        elif pe_hits > ce_hits:
            bear += weights["oi_shift"]
    if snap.failed_breakdown:
        bull += weights["failed_break"]
    elif snap.failed_breakout:
        bear += weights["failed_break"]
    direction = "CE" if bull >= bear else "PE"
    return direction, max(bull, bear), bull, bear


def record(engine, st, future, fut_builder, weights, shield_min):
    """Build and log one SHADOW_COMPARE event for index state `st`."""
    now = datetime.now(IST)
    snap = st.snap

    # --- VWAP fix
    f_close, f_vwap, f_vol, f_avg = future_vwap_view(
        fut_builder, future["token"] if future else None)
    vwap_usable = (f_close is not None and f_vwap is not None
                   and f_vol is not None and f_vol >= MIN_FUTURE_BAR_VOLUME)
    if vwap_usable:
        new_snap = replace(snap, close=f_close, vwap=f_vwap, vol=f_vol, avg_vol=f_avg)
        sit_out = None
    else:
        # VWAP and volume factors sit out; no rescaling of the other factors.
        new_snap = replace(snap, close=None, vwap=None, vol=0, avg_vol=None)
        sit_out = ("no_future" if future is None else
                   "no_future_ticks" if f_close is None or f_vwap is None else
                   "future_volume_too_thin")
    new_dir, new_score, new_bull, new_bear = bull_bear(new_snap, st.chain, weights)
    old_dir, old_score, _, _ = bull_bear(snap, st.chain, weights)

    # --- days-to-expiry fix
    expiry = engine.scrips.nearest_expiry(st.chain.index) if st.chain else None
    old_days = engine._dte(st)
    new_days = exact_days_to_expiry(expiry, now)
    greeks_old = atm_greeks(st.chain, st.spot, old_days)
    greeks_new = atm_greeks(st.chain, st.spot, new_days) if new_days else {}

    audit.log(
        "SHADOW_COMPARE", index=st.name, spot=st.spot,
        live=dict(direction=st.direction, score=st.score, tier=st.tier,
                  recomputed_score=old_score, recomputed_direction=old_dir,
                  above_vwap=snap.above_vwap, vwap=snap.vwap),
        vwap_fix=dict(direction=new_dir, score=new_score, bull=new_bull, bear=new_bear,
                      meets_score=new_score >= shield_min, sit_out=sit_out,
                      future=future and future["symbol"], future_close=f_close,
                      future_vwap=round(f_vwap, 2) if f_vwap else None,
                      future_above_vwap=new_snap.above_vwap,
                      future_bar_volume=f_vol,
                      future_avg_volume=round(f_avg, 1) if f_avg else None),
        dte_fix=dict(expiry=expiry, old_days=old_days,
                     new_days=round(new_days, 4) if new_days else None,
                     atm_old=greeks_old, atm_new=greeks_new),
        changed=dict(direction=new_dir != st.direction,
                     crosses_threshold=(new_score >= shield_min) != (st.score >= shield_min)))
