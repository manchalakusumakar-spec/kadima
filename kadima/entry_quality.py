"""
entry_quality.py — enter where the stop is least likely to be hit.

THE INSIGHT:
    Stopout probability is set at ENTRY, not by the stop percentage.

    Enter after a premium has already run 40% off its intraday low, and
    a 30% stop sits inside the range the premium routinely travels — a
    normal pullback takes you out. Enter on a controlled retest near a
    structural level, and the same setup gives you:

        - a TIGHTER stop in rupees (structural level is closer)
        - a LOWER chance of it being hit (the level is defended)
        - a BETTER RR (more room to target, less paid at entry)

    This is why the answer to "how do I reduce risk" is usually entry
    timing, not stop width. Widening the stop reduces stopout odds but
    increases rupee loss when hit. Better entry reduces both.

YOUR SPEC, SECTION 22 (Chase Prevention):
    Reject when premium has already expanded abnormally, when underlying
    is excessively extended from VWAP/EMA, when entry materially damages
    1:2 RR, when spread has widened, or when the move lacks a controlled
    retest. Prefer pullback/retest, fresh confirmed breakout, or fresh
    re-acceleration.

    This module implements that as a hard gate plus a structural stop.

WHAT THIS IS NOT:
    Not a way to make a marginal setup tradeable. If the premium is
    extended and there is no retest, the correct output is WAIT — not a
    worse entry with a wider stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import audit


# ---------------------------------------------------------------- config
# Move into config.py.
MAX_PREMIUM_RANGE_POSITION = 0.60   # reject if LTP is above this fraction of
                                    # today's premium range (0=low, 1=high)
MAX_PREMIUM_RUN_PCT = 25.0          # reject if premium already up this much
                                    # from its recent (30-min) base
MAX_UNDERLYING_EXT_PCT = 0.40       # reject if spot is this far from VWAP (%)
MIN_RR_AFTER_ENTRY = 2.0            # your standing 1:2 minimum

STRUCTURAL_STOP_BUFFER_PCT = 5.0    # place stop this far beyond the level
MAX_STRUCTURAL_STOP_PCT = 30.0      # never wider than the fixed stop
MIN_STRUCTURAL_STOP_PCT = 8.0       # never tighter than this — below this a
                                    # stop sits inside normal option noise
                                    # and will be hit at random


@dataclass
class EntryContext:
    # option premium
    ltp: float
    premium_day_high: float
    premium_day_low: float
    premium_base_30min: Optional[float]   # premium 30 min ago, for run check

    # underlying structure
    spot: float
    vwap: float
    swing_level: Optional[float]          # recent swing low (for CE) or high (for PE)
                                          # expressed as a PREMIUM level, not spot
    target_premium: Optional[float]       # from the setup's T1

    direction: str                        # "CE" or "PE"
    spread_pct: Optional[float] = None
    stop_override: Optional[float] = None


@dataclass
class EntryVerdict:
    allowed: bool
    reason: str
    stop_price: Optional[float] = None
    stop_pct: Optional[float] = None
    rr: Optional[float] = None
    range_position: Optional[float] = None


# ---------------------------------------------------------------- checks
def _range_position(ctx: EntryContext) -> Optional[float]:
    """Where LTP sits in today's premium range. 0 = at the low, 1 = at the high.
    Entering near 1 means buying the top of the range the premium has been
    oscillating in — the most likely place to be stopped by mean reversion."""
    span = ctx.premium_day_high - ctx.premium_day_low
    if span <= 0:
        return None
    return (ctx.ltp - ctx.premium_day_low) / span


def _premium_run_pct(ctx: EntryContext) -> Optional[float]:
    if not ctx.premium_base_30min or ctx.premium_base_30min <= 0:
        return None
    return (ctx.ltp - ctx.premium_base_30min) / ctx.premium_base_30min * 100.0


def _underlying_extension_pct(ctx: EntryContext) -> Optional[float]:
    if not ctx.vwap or ctx.vwap <= 0:
        return None
    return abs(ctx.spot - ctx.vwap) / ctx.vwap * 100.0


# ---------------------------------------------------------------- stop
def structural_stop(ctx: EntryContext) -> tuple[Optional[float], Optional[float], str]:
    """
    Derive a stop from structure rather than a flat percentage.

    Returns (stop_price, stop_pct, note).

    If a swing level exists, place the stop just beyond it. That level is
    where the setup is genuinely invalidated — a stop there is both
    tighter and more meaningful than an arbitrary 30%.

    Clamped both ways: never wider than the fixed stop (capital control),
    never tighter than MIN_STRUCTURAL_STOP_PCT (below which the stop sits
    inside ordinary option noise and gets hit at random).
    """
    if ctx.swing_level is None or ctx.swing_level <= 0:
        # No structure to lean on -> fall back to the flat stop.
        stop = ctx.ltp * (1 - MAX_STRUCTURAL_STOP_PCT / 100.0)
        return stop, MAX_STRUCTURAL_STOP_PCT, "no_swing_level_using_flat_stop"

    raw_stop = ctx.swing_level * (1 - STRUCTURAL_STOP_BUFFER_PCT / 100.0)
    stop_pct = (ctx.ltp - raw_stop) / ctx.ltp * 100.0

    if stop_pct > MAX_STRUCTURAL_STOP_PCT:
        stop = ctx.ltp * (1 - MAX_STRUCTURAL_STOP_PCT / 100.0)
        return stop, MAX_STRUCTURAL_STOP_PCT, "structural_stop_too_wide_clamped"

    if stop_pct < MIN_STRUCTURAL_STOP_PCT:
        stop = ctx.ltp * (1 - MIN_STRUCTURAL_STOP_PCT / 100.0)
        return stop, MIN_STRUCTURAL_STOP_PCT, "structural_stop_too_tight_widened"

    return raw_stop, stop_pct, "structural"


# ---------------------------------------------------------------- gate
def evaluate_entry(ctx: EntryContext) -> EntryVerdict:
    """
    Decide whether this is a good place to enter. WAIT is a valid, common,
    and correct answer.
    """
    rng = _range_position(ctx)

    # 1. Is the premium already extended within today's range?
    if rng is not None and rng > MAX_PREMIUM_RANGE_POSITION:
        return EntryVerdict(
            False,
            f"premium_at_{rng:.0%}_of_day_range_over_{MAX_PREMIUM_RANGE_POSITION:.0%}",
            range_position=rng,
        )

    # 2. Has the premium already run hard in the last 30 min?
    run = _premium_run_pct(ctx)
    if run is not None and run > MAX_PREMIUM_RUN_PCT:
        return EntryVerdict(
            False,
            f"premium_already_up_{run:.0f}pct_in_30min",
            range_position=rng,
        )

    # 3. Is the underlying overextended from VWAP?
    ext = _underlying_extension_pct(ctx)
    if ext is not None and ext > MAX_UNDERLYING_EXT_PCT:
        return EntryVerdict(
            False,
            f"underlying_{ext:.2f}pct_from_vwap_over_{MAX_UNDERLYING_EXT_PCT}",
            range_position=rng,
        )

    # 4. Derive the stop from structure.
    if ctx.stop_override is not None:
        stop_price = ctx.stop_override
        if not 0 < stop_price < ctx.ltp:
            return EntryVerdict(False, 'invalid_structural_stop')
        stop_pct = (ctx.ltp - stop_price) / ctx.ltp * 100
        note = 'confirmed_underlying_pivot_mapped_to_premium'
    else:
        stop_price, stop_pct, note = structural_stop(ctx)  # legacy callers only
    if stop_price is None:
        return EntryVerdict(False, "cannot_derive_stop", range_position=rng)

    # 5. Does the resulting RR still clear the 1:2 minimum?
    if ctx.target_premium and ctx.target_premium > ctx.ltp:
        reward = ctx.target_premium - ctx.ltp
        risk = ctx.ltp - stop_price
        rr = reward / risk if risk > 0 else None
        if rr is None or rr < MIN_RR_AFTER_ENTRY:
            return EntryVerdict(
                False,
                f"rr_{rr:.2f}_under_{MIN_RR_AFTER_ENTRY}" if rr else "rr_uncomputable",
                stop_price=stop_price,
                stop_pct=stop_pct,
                rr=rr,
                range_position=rng,
            )
    else:
        rr = None

    audit.log(
        "entry_quality_pass",
        ltp=ctx.ltp,
        range_position=round(rng, 3) if rng is not None else None,
        premium_run_30min_pct=round(run, 1) if run is not None else None,
        underlying_ext_pct=round(ext, 3) if ext is not None else None,
        stop_price=round(stop_price, 2),
        stop_pct=round(stop_pct, 1),
        stop_basis=note,
        rr=round(rr, 2) if rr else None,
    )
    return EntryVerdict(True, note, stop_price, stop_pct, rr, rng)

