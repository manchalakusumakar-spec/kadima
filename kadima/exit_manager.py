"""Realised-P&L helpers and legacy pure exit evaluator.

The live executor uses trade_policy's structural stop and persisted ratchet.
INITIAL_SL_PCT remains only for legacy positions/evaluator callers; new entries
must supply confirmed structural protection. See docs/trade_policy.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from kadima import storage, audit
from .trade_policy import ACTIVATION, RETAIN_PROFIT
from kadima.utils import now_ist


# ---------------------------------------------------------------- config
# All of these are starting points, not fixed forever. Change them here,
# not inline in the logic below, so the forecast-vs-actual tracker can
# tell you if a number needs recalibrating.

INITIAL_SL_PCT = 0.30          # legacy positions only
GAIN_ACTIVATION_PCT = ACTIVATION
GIVEBACK_PCT = 1 - RETAIN_PROFIT            # once active, exit if price gives back 50% of
                               # the PEAK GAIN reached (not 50% of peak price).
                               # Widened from 30% on 2026-08-24, trader's
                               # explicit choice, after discussing the real
                               # tradeoff: a wider giveback means less risk
                               # of being shaken out by a temporary pullback
                               # within a still-strong move, at the direct
                               # cost of protecting less of the peak gain if
                               # the move genuinely reverses. Deliberate,
                               # not a default -- there's no objectively
                               # "right" number here, this is a real
                               # risk/reward calibration choice.

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
    entry time and passes it in on every evaluation."""
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
    """Call this on every evaluation tick for each open position.
    Pure function: no DB writes, no order calls. Caller persists the
    returned new_sl back onto the Position and acts on should_exit."""

    # Update best-seen premium first — trail logic depends on the high
    # water mark, not just the current tick.
    best = max(pos.best_premium_seen, current_premium)

    gained = best - pos.entry_premium
    activation_threshold = pos.entry_premium * GAIN_ACTIVATION_PCT

    # Has the trail activated yet? Percentage-of-entry-premium threshold,
    # not a fixed point count — see module docstring for why.
    trail_active = pos.trail_active or (gained >= activation_threshold)

    if trail_active:
        # Protect (1 - GIVEBACK_PCT) of the peak gain. This is proportional
        # to the SIZE of the gain, not a fixed distance behind the peak —
        # a large run gets meaningfully more room to breathe than a small
        # one, which a fixed-points trail cannot express.
        giveback_allowed = gained * GIVEBACK_PCT
        candidate_sl = best - giveback_allowed
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
    """Sums closed-position realized P&L, in actual rupees, for today.

    Fixed 2026-08-23: this function predated the real `positions` table
    and was written against assumed column names that turned out wrong
    (entry_premium/exit_premium/closed_ts vs the real entry_price/
    exit_price/exit_ts), AND was missing the lot_size multiplier entirely
    (old comment: "left as 1x here") -- meaning it was computing points
    times lots, not rupees. Comparing that against a rupee-denominated
    daily loss limit would have been comparing the wrong units and could
    have silently under- or over-stated real risk by whatever a single
    lot's contract multiplier happens to be (65 for NIFTY, etc.) -- never
    caught until wiring this in for real and reading it against the
    actual schema."""
    from . import execution_ledger
    c = execution_ledger.connection()
    today = now_ist().strftime("%Y-%m-%d")
    rows = c.execute(
        "SELECT entry_price, exit_price, lots, lot_size FROM positions "
        "WHERE exit_ts LIKE ? AND status = 'closed' AND NOT EXISTS "
        "(SELECT 1 FROM execution_orders e WHERE e.signal_id=positions.signal_id) "
        "AND COALESCE(exit_reason,'') != 'external_fill_reconciled'",
        (f"{today}%",),
    ).fetchall()

    total = 0.0
    for r in rows:
        if r["entry_price"] is None or r["exit_price"] is None:
            continue
        total += (r["exit_price"] - r["entry_price"]) * r["lots"] * r["lot_size"]
    total += c.execute("SELECT COALESCE(SUM(pnl),0) FROM execution_fills "
                       "WHERE ts LIKE ? AND dry_run=0", (f"{today}%",)).fetchone()[0]
    from .external_reconciliation import realized_today
    total += realized_today()
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



