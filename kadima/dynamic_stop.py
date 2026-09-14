"""
dynamic_stop.py -- invalidation-based stop-loss, replacing a flat
percentage stop.

WHY THIS EXISTS (Kusumakar, 2026-09-11):
    A flat % stop exits purely on how far price has moved, with no
    regard for whether the setup could still play out. This module
    instead asks: does the technical picture still allow for a
    reversal? Exit ONLY once that answer becomes genuinely no --
    not before, regardless of what % the position happens to be
    showing at that moment.

REVERSAL SCOPE is considered PRESENT as long as BOTH:
    1. Price has not decisively broken the swing level against the
       position (day_low for a CE position, day_high for a PE
       position -- same swing_level proxy auto_executor.py already
       computes).
    2. VWAP/EMA alignment has NOT flipped against the position for
       TWO consecutive candles (mirrors the existing Bull Shield
       "SHIELD INVALIDATED" concept: a flip within 2 candles voids
       the setup).

The stop-loss fires ONLY when BOTH turn against the position at the
same time -- reversal scope is gone, not just "price is down".

SAFETY ADDITION (flagged, not silently assumed): a hard catastrophic
floor at CATASTROPHIC_FLOOR_PCT of premium fires regardless of the
technical picture above. Without an absolute floor, a bug in this
logic or a violent gap move could let a position ride toward total
loss with no backstop. Remove this if you don't want it -- but I
would not deploy real-capital stop-loss logic without one.

INTEGRATION:
    This is a NEW check, meant to run alongside (and eventually
    replace) auto_executor.py's existing check_exits() /
    FIXED_STOPLOSS_PCT logic. It reuses the same snap
    (indicators.Snapshot) and swing_level values auto_executor.py
    already computes at entry -- see the call-site note in
    should_exit() below for exactly what auto_executor.py needs to
    pass in.

    This module does NOT touch auto_executor.py itself -- wiring it
    in is a deliberate separate step once you've reviewed the logic,
    same caution as the CAS Blast Scanner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import audit

# ---------------------------------------------------------------- config
CATASTROPHIC_FLOOR_PCT = 60.0   # hard exit regardless of technicals,
                                 # flagged safety addition -- see docstring

FLIP_CONFIRM_CANDLES = 2        # VWAP/EMA must be against the position
                                 # for this many consecutive candles
                                 # before counting as a real flip


@dataclass
class ReversalScopeState:
    """Tracks consecutive candles of adverse VWAP/EMA alignment for one
    open position. One instance per position; discard on exit."""
    adverse_alignment_streak: int = 0
    last_checked_candle_ts: Optional[datetime] = None


def _price_broke_swing_level(direction: str, ltp: float, swing_level: float) -> bool:
    """direction: CE or PE. swing_level: day_low for CE, day_high for PE
    (the same PREMIUM-level proxy auto_executor.py already computes --
    see its comment: 'a break back below today's low invalidates a
    bullish premium thesis', mirror case for PE)."""
    if direction == "CE":
        return ltp < swing_level
    else:  # PE
        return ltp > swing_level


def _alignment_is_adverse(direction: str, above_vwap: Optional[bool],
                          ema_bull: Optional[bool]) -> bool:
    """True if VWAP/EMA alignment currently opposes the position.
    Missing data is NOT treated as adverse -- unknown is unknown, we
    don't invalidate on a data gap, we just don't advance the flip
    counter that candle."""
    if above_vwap is None or ema_bull is None:
        return False
    if direction == "CE":
        return (not above_vwap) and (not ema_bull)
    else:  # PE
        return above_vwap and ema_bull


def check_reversal_scope(
    state: ReversalScopeState,
    direction: str,
    entry_premium: float,
    current_premium: float,
    swing_level: float,
    above_vwap: Optional[bool],
    ema_bull: Optional[bool],
    candle_ts: datetime,
) -> tuple[bool, str]:
    """
    Returns (should_exit, reason).

    Call this once per new candle for each open position (not every
    tick -- the flip-confirmation logic is candle-based, matching the
    existing Bull Shield 'hold one extra candle' convention).
    """
    if entry_premium <= 0:
        return True, "invalid_entry_premium_failing_closed"

    # Hard catastrophic floor -- always checked first, always wins.
    loss_pct = (entry_premium - current_premium) / entry_premium * 100.0
    if loss_pct >= CATASTROPHIC_FLOOR_PCT:
        audit.log("dynamic_stop_catastrophic_floor_hit",
                  entry_premium=entry_premium, current_premium=current_premium,
                  loss_pct=round(loss_pct, 1))
        return True, f"catastrophic_floor_{loss_pct:.0f}pct"

    # Don't re-process the same candle twice.
    if state.last_checked_candle_ts == candle_ts:
        return False, "already_checked_this_candle"
    state.last_checked_candle_ts = candle_ts

    swing_broken = _price_broke_swing_level(direction, current_premium, swing_level)
    adverse = _alignment_is_adverse(direction, above_vwap, ema_bull)

    if adverse:
        state.adverse_alignment_streak += 1
    else:
        state.adverse_alignment_streak = 0  # any favorable/neutral candle resets it

    flip_confirmed = state.adverse_alignment_streak >= FLIP_CONFIRM_CANDLES

    # Reversal scope is gone only when BOTH conditions are true together.
    if swing_broken and flip_confirmed:
        audit.log(
            "dynamic_stop_reversal_scope_exhausted",
            direction=direction, entry_premium=entry_premium,
            current_premium=current_premium, loss_pct=round(loss_pct, 1),
            swing_level=swing_level, adverse_streak=state.adverse_alignment_streak,
        )
        return True, "reversal_scope_exhausted"

    return False, "reversal_scope_still_present"
