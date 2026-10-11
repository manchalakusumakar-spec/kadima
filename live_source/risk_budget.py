"""
risk_budget.py — cap absolute rupee risk per trade by strike selection.

THE IDEA:
    Rather than widening/narrowing the stop %, constrain which strikes are
    tradeable so that (premium x lot_size x stop%) stays under a fixed
    rupee ceiling. This caps downside per trade in absolute terms, which
    is what actually matters on a small account.

THE TRAP THIS AVOIDS:
    "Just buy the cheapest option" degenerates into buying far-OTM
    lottery tickets. Cheap premium is cheap because:
      - delta is low        -> needs a much larger underlying move to pay
      - theta is savage     -> can lose 30-50% in a day with spot flat
      - spreads are wide    -> a Rs 1 spread on a Rs 20 option is 5% each
                               way, 10% round-trip, before you are right
      - depth is thin       -> the stop may not fill near where you set it

    Your spec already says this (Section 3: rank strikes by TRADEABILITY,
    not theoretical profit; Section 23: execution quality has VETO
    authority). This module implements that with a budget added, not a
    budget instead.

THE RULE:
    Among strikes that pass ALL quality gates, pick the one with the
    lowest rupee risk that still fits the budget. If none passes, return
    None -> "NO A-GRADE SETUP - WAIT". Never relax a gate to find a fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from . import audit
from .instrument_master import get_lot_size


# ---------------------------------------------------------------- config
# Move into config.py. Suggested: MAX_RISK_PER_TRADE_INR ~= 2-5% of capital.
MAX_RISK_PER_TRADE_INR = 750.0     # absolute rupee risk ceiling per trade
STOPLOSS_PCT = 30.0                # must match stops.STOPLOSS_PCT

# Quality gates — a strike must pass ALL of these regardless of price.
MIN_DELTA = 0.25                   # below this, needs an implausible move
MAX_SPREAD_PCT = 3.0               # bid-ask as % of mid
MIN_OPTION_VOLUME = 1000           # today's traded contracts
MIN_OPEN_INTEREST = 5000
MAX_DISTANCE_FROM_SPOT_PCT = 2.0   # how far OTM we will go, hard cap


@dataclass
class StrikeCandidate:
    symbol: str
    token: str
    strike: float
    option_type: str        # CE | PE
    ltp: float
    bid: Optional[float]
    ask: Optional[float]
    delta: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]


@dataclass
class StrikeVerdict:
    candidate: Optional[StrikeCandidate]
    lots: int
    risk_inr: float
    cost_inr: float
    reason: str
    rejected: list          # [(strike, why), ...] for audit


# ---------------------------------------------------------------- gates
def _spread_pct(c: StrikeCandidate) -> Optional[float]:
    if c.bid is None or c.ask is None or c.bid <= 0 or c.ask <= 0:
        return None
    mid = (c.bid + c.ask) / 2
    if mid <= 0:
        return None
    return (c.ask - c.bid) / mid * 100.0


def _quality_fail_reason(c: StrikeCandidate, spot: float) -> Optional[str]:
    """Returns None if the strike passes all gates, else the failure reason.
    Missing data is a FAILURE, not a pass — we do not size on unknowns."""
    if c.ltp is None or c.ltp <= 0:
        return "no_valid_ltp"

    sp = _spread_pct(c)
    if sp is None:
        return "spread_unavailable"
    if sp > MAX_SPREAD_PCT:
        return f"spread_{sp:.1f}pct_over_{MAX_SPREAD_PCT}"

    if c.delta is None:
        return "delta_unavailable"
    if abs(c.delta) < MIN_DELTA:
        return f"delta_{abs(c.delta):.2f}_under_{MIN_DELTA}"

    if c.volume is None or c.volume < MIN_OPTION_VOLUME:
        return f"volume_{c.volume}_under_{MIN_OPTION_VOLUME}"

    if c.open_interest is None or c.open_interest < MIN_OPEN_INTEREST:
        return f"oi_{c.open_interest}_under_{MIN_OPEN_INTEREST}"

    dist_pct = abs(c.strike - spot) / spot * 100.0
    if dist_pct > MAX_DISTANCE_FROM_SPOT_PCT:
        return f"distance_{dist_pct:.1f}pct_over_{MAX_DISTANCE_FROM_SPOT_PCT}"

    return None


# ---------------------------------------------------------------- select
def select_within_budget(
    candidates: Sequence[StrikeCandidate],
    spot: float,
    max_risk_inr: float = MAX_RISK_PER_TRADE_INR,
    max_lots: int = 1,
) -> StrikeVerdict:
    """
    Pick the lowest-risk strike that passes every quality gate AND fits
    the rupee risk budget.

    Returns a verdict with candidate=None if nothing qualifies. That is a
    legitimate outcome — "NO A-GRADE SETUP - WAIT" — not a failure to
    handle by relaxing gates.
    """
    rejected = []
    viable = []

    for c in candidates:
        fail = _quality_fail_reason(c, spot)
        if fail:
            rejected.append((c.strike, c.option_type, fail))
            continue

        lot_size = get_lot_size(c.symbol, c.token)
        if lot_size is None:
            rejected.append((c.strike, c.option_type, "lot_size_unresolvable"))
            continue

        cost_1lot = c.ltp * lot_size
        risk_1lot = cost_1lot * (STOPLOSS_PCT / 100.0)

        if risk_1lot > max_risk_inr:
            rejected.append(
                (c.strike, c.option_type, f"risk_{risk_1lot:.0f}_over_budget_{max_risk_inr:.0f}")
            )
            continue

        viable.append((risk_1lot, cost_1lot, lot_size, c))

    if not viable:
        audit.log(
            "strike_selection_no_qualifying_strike",
            spot=spot,
            max_risk_inr=max_risk_inr,
            candidates_examined=len(candidates),
            rejections=rejected,
        )
        return StrikeVerdict(None, 0, 0.0, 0.0, "no_qualifying_strike", rejected)

    # Lowest rupee risk first; tie-break on tighter spread (better execution).
    viable.sort(key=lambda v: (v[0], _spread_pct(v[3]) or 99))
    risk_1lot, cost_1lot, lot_size, chosen = viable[0]

    # How many lots fit the risk budget?
    lots = min(max_lots, int(max_risk_inr // risk_1lot))
    if lots < 1:
        return StrikeVerdict(None, 0, 0.0, 0.0, "budget_below_one_lot", rejected)

    verdict = StrikeVerdict(
        candidate=chosen,
        lots=lots,
        risk_inr=risk_1lot * lots,
        cost_inr=cost_1lot * lots,
        reason="ok",
        rejected=rejected,
    )
    audit.log(
        "strike_selected_within_budget",
        symbol=chosen.symbol,
        strike=chosen.strike,
        option_type=chosen.option_type,
        ltp=chosen.ltp,
        lots=lots,
        risk_inr=round(verdict.risk_inr, 0),
        cost_inr=round(verdict.cost_inr, 0),
        delta=chosen.delta,
        spread_pct=round(_spread_pct(chosen) or -1, 2),
        rejected_count=len(rejected),
    )
    return verdict
