"""
KADIMA exit manager — trailing stop-loss + daily loss limit.

Scope: this module ONLY decides when an already-open position should be
closed. It does not open positions (that's auto_executor.py's entry
logic) and it does not compute strikes (that's strike_selector.py).

Spec locked with the trader on 2026-08-17:
  - Trail method:        fixed points, not percentage or ATR
  - Trail activation:    once position is ACTIVATION_POINTS in profit
  - Trail distance:      TRAIL_POINTS behind the best premium seen
  - Initial hard SL:     INITIAL_SL_PCT of entry premium (until trail activates)
  - Daily loss limit:    DAILY_LOSS_PCT of configured capital, hard stop
                         for the whole engine, not just one position
  - First live week:     MAX_LOTS_FIRST_WEEK cap, enforced by auto_executor.py
                         reading FIRST_WEEK_MODE from here — this module
                         does not place trades, only flags when to exit.

Nothing here sends an order. It returns a decision; auto_executor.py
(or you, manually, during dry-run) is the only thing that acts on it.
This separation is deliberate: exit *logic* and exit *execution* should
never be the same function, so a bug in one can't silently skip the
other's safety check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from kadima import storage, audit
from kadima.utils import now_ist


# ---------------------------------------------------------------- config
# All of these are starting points, not fixed forever. Change them here,
# not inline in the logic below, so the forecast-vs-actual tracker can
# tell you if a number needs recalibrating.

INITIAL_SL_PCT = 0.30          # hard SL = 30% of entry premium, until trail activates
ACTIVATION_POINTS = 20         # trail starts once position is +20 pts in premium
TRAIL_POINTS = 10              # once active, SL trails 10 pts behind best premium seen

DAILY_LOSS_PCT = 0.05          # 5% of capital — hard stop for the day, whole engine
CAPITAL_RUPEES = None          # set this before going live; None = daily limit disabled

FIRST_WEEK_MODE = True         # True = cap position size regardless of Shield tier
MAX_LOTS_FIRST_WEEK = 1


ExitReason = Literal[
    "initial_sl_hit",
    "trailing_sl_hit",
    "daily_loss_limit",
    "market_close_forced",
    "manual",
]


@dataclass
class Position:
    """Mirrors one open position. auto_executor.py constructs this at
    entry time and passes it in on every candle close for evaluation."""
    signal_id: int
    index: str
    direction: str          # "CE" or "PE"
    strike_symbol: str
    entry_premium: float
    entry_ts: datetime
    lots: int
    best_premium_seen: float = None  # set to entry_premium on first eval
    trail_active: bool = False
    current_sl: float = field(default=None)  # computed on first eval if None

    def __post_init__(self):
        if self.best_premium_seen is None:
            self.best_premium_seen = self.entry_premium
        if self.current_sl is None:
            self.current_sl = self.entry_premium * (1 - INITIAL_SL_PCT)


@dataclass
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    exit_premium: float | None = None
    new_sl: float | None = None  # if not exiting, the (possibly updated) SL to persist


def evaluate_position(pos: Position, current_premium: float) -> ExitDecision:
    """Call this on every 5-min candle close for each open position.
    Pure function: no DB writes, no order calls. Caller persists the
    returned new_sl back onto the Position and acts on should_exit."""

    # Update best-seen premium first — trail logic depends on the high
    # water mark, not just the current tick.
    best = max(pos.best_premium_seen, current_premium)

    # Has the trail activated yet?
    trail_active = pos.trail_active or (best - pos.entry_premium) >= ACTIVATION_POINTS

    if trail_active:
        candidate_sl = best - TRAIL_POINTS
        # SL only ever moves up (in the direction of profit), never back down.
        new_sl = max(pos.current_sl, candidate_sl)
    else:
        new_sl = pos.current_sl  # unchanged, still the initial hard SL

    if current_premium <= new_sl:
        reason: ExitReason = "trailing_sl_hit" if trail_active else "initial_sl_hit"
        return ExitDecision(
            should_exit=True,
            reason=reason,
            exit_premium=current_premium,
        )

    return ExitDecision(should_exit=False, new_sl=new_sl)


def check_daily_loss_limit(realized_pnl_today: float) -> bool:
    """Returns True if the daily loss limit has been breached and the
    engine should refuse new entries and flatten open positions.
    Called by auto_executor.py before every new entry attempt, and by
    the position-eval loop for existing positions."""
    if CAPITAL_RUPEES is None:
        audit.log("daily_loss_limit_check_skipped", reason="CAPITAL_RUPEES not set")
        return False

    limit = CAPITAL_RUPEES * DAILY_LOSS_PCT
    breached = realized_pnl_today <= -limit

    if breached:
        audit.log(
            "daily_loss_limit_breached",
            realized_pnl_today=round(realized_pnl_today, 2),
            limit=round(limit, 2),
            capital=CAPITAL_RUPEES,
        )
    return breached


def realized_pnl_today() -> float:
    """Sums closed-position P&L for today from storage. Assumes a
    `positions` table with columns (signal_id, exit_reason, entry_premium,
    exit_premium, lots, closed_ts) gets created by auto_executor.py when
    it's built — this function is written against that assumption and
    will need its query adjusted to match the real schema once that
    table exists."""
    c = storage.conn()
    today = now_ist().strftime("%Y-%m-%d")
    rows = c.execute(
        "SELECT entry_premium, exit_premium, lots FROM positions "
        "WHERE closed_ts LIKE ? AND exit_reason IS NOT NULL",
        (f"{today}%",),
    ).fetchall()

    total = 0.0
    for r in rows:
        # premium is per-unit; lot size multiplier belongs in config,
        # left as 1x here since lot sizes differ by index (NIFTY vs
        # BANKNIFTY vs SENSEX) and that table isn't visible to this module.
        total += (r["exit_premium"] - r["entry_premium"]) * r["lots"]
    return total


def max_lots_allowed(requested_lots: int) -> int:
    """auto_executor.py calls this before sizing any new entry."""
    if FIRST_WEEK_MODE:
        capped = min(requested_lots, MAX_LOTS_FIRST_WEEK)
        if capped < requested_lots:
            audit.log(
                "first_week_lot_cap_applied",
                requested=requested_lots,
                capped_to=capped,
            )
        return capped
    return requested_lots
