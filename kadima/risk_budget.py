"""Quality-gated structural-stop sizing against allocated trading capital."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from . import audit, trade_policy
from .instrument_master import get_lot_size


# Quality gates — a strike must pass ALL of these regardless of price.
MIN_DELTA = 0.25                   # below this, needs an implausible move
MAX_SPREAD_PCT = 1.5               # bid-ask as % of mid
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
    stop_price: Optional[float] = None


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


def _quality_fail_reason(c: StrikeCandidate, spot: float, gates: Optional[dict] = None) -> Optional[str]:
    """Returns None if the strike passes all gates, else the failure reason.
    Missing data is a FAILURE, not a pass — we do not size on unknowns.
    gates: STOCK BLAST v1 (2026-10-27) per-call overrides for stock contracts;
    None keeps the index gates above unchanged."""
    g = gates or {}
    max_spread = g.get('max_spread_pct', MAX_SPREAD_PCT)
    min_delta = g.get('min_delta', MIN_DELTA)
    min_volume = g.get('min_volume', MIN_OPTION_VOLUME)
    min_oi = g.get('min_oi', MIN_OPEN_INTEREST)
    max_distance = g.get('max_distance_pct', MAX_DISTANCE_FROM_SPOT_PCT)
    if not trade_policy.positive(c.ltp, spot, c.strike):
        return "no_valid_ltp"

    sp = _spread_pct(c)
    if sp is None:
        return "spread_unavailable"
    if not 0 <= sp <= max_spread:
        # Stock rule: a tight rupee spread on a cheap contract is tradeable up to a hard cap.
        if not (gates and c.ask - c.bid <= g.get('abs_spread', 0) + 1e-9
                and sp <= g.get('hard_spread_pct', max_spread)):
            return f"spread_{sp:.1f}pct_over_{max_spread}"

    if c.delta is None:
        return "delta_unavailable"
    if not trade_policy.positive(abs(c.delta)) or abs(c.delta) < min_delta:
        return f"delta_{abs(c.delta):.2f}_under_{min_delta}"

    if not trade_policy.positive(c.volume) or c.volume < min_volume:
        return f"volume_{c.volume}_under_{min_volume}"

    if not trade_policy.positive(c.open_interest) or c.open_interest < min_oi:
        return f"oi_{c.open_interest}_under_{min_oi}"

    dist_pct = abs(c.strike - spot) / spot * 100.0
    if dist_pct > max_distance:
        return f"distance_{dist_pct:.1f}pct_over_{max_distance}"

    return None


# ---------------------------------------------------------------- select
def select_within_budget(
    candidates: Sequence[StrikeCandidate],
    spot: float,
    allocated_capital: float = 0.0,
    max_lots: int = 1,
    gates: Optional[dict] = None,
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
    max_risk_inr = allocated_capital * trade_policy.RISK_FRACTION

    for c in candidates:
        fail = _quality_fail_reason(c, spot, gates)
        if fail:
            rejected.append((c.strike, c.option_type, fail))
            continue

        lot_size = get_lot_size(c.symbol, c.token)
        if not trade_policy.positive(lot_size):
            rejected.append((c.strike, c.option_type, "lot_size_unresolvable"))
            continue

        entry_limit = round(c.ltp * (1 + trade_policy.ENTRY_BUFFER), 1)
        unit_risk = trade_policy.risk_per_unit(entry_limit, c.stop_price)
        if unit_risk is None or trade_policy.lots_within_allocation(
                allocated_capital, entry_limit, c.stop_price, lot_size, max_lots) < 1:
            rejected.append((c.strike, c.option_type, 'structure_or_allocation_insufficient'))
            continue
        cost_1lot = entry_limit * (1 + trade_policy.COST_RESERVE) * lot_size
        risk_1lot = unit_risk * lot_size

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
    lots = min(max_lots, int(max_risk_inr // risk_1lot), int(allocated_capital // cost_1lot))
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

