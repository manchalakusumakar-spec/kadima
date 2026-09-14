"""Thirty-minute forecast condition for long-option profit trailing.

A forecast is an external model output, not a target or a probability.
This module neither creates forecasts nor places orders.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import math


@dataclass(frozen=True)
class Projection:
    token: str
    exchange: str
    signal_id: int
    generated_at: datetime
    market_data_at: datetime
    target_at: datetime
    expected_premium: float
    model_id: str


@dataclass(frozen=True)
class Decision:
    stop: float
    breached: bool
    should_exit: bool
    reason: str
    risk_amount: float
    projected_profit: float | None = None


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def evaluate(*, entry, peak, previous_stop, ltp, quantity, giveback_fraction,
             token, exchange, signal_id, now, projection=None):
    """Compare remaining 30-minute upside with allowed peak-profit giveback.

    Risk = (peak - ratcheted stop) * remaining quantity, frozen in price
    terms by the current peak. It is NOT current-minus-stop, which becomes
    zero/negative when the stop is breached. Equality does not trigger exit.
    Only call for an already activated trailing stop.
    """
    if not all(_finite(v) for v in (entry, peak, previous_stop, ltp, giveback_fraction)):
        raise ValueError("Non-finite price or trailing fraction")
    if min(entry, peak, ltp) <= 0 or peak < entry or previous_stop < 0:
        raise ValueError("Invalid long-option prices")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("Quantity must be a positive integer")
    if not 0 < giveback_fraction < 1:
        raise ValueError("Giveback fraction must be between zero and one")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware time required")
    stop = max(previous_stop, peak - (peak - entry) * giveback_fraction)
    risk = max(0.0, peak - stop) * quantity
    breached = ltp <= stop
    reason = None
    p = projection
    if p is None:
        reason = "forecast_unavailable"
    elif p.token != str(token) or p.exchange != exchange or p.signal_id != signal_id:
        reason = "forecast_position_mismatch"
    elif not p.model_id or not _finite(p.expected_premium) or p.expected_premium < 0:
        reason = "forecast_invalid"
    elif any(t.tzinfo is None or t.utcoffset() is None for t in
             (p.generated_at, p.market_data_at, p.target_at)):
        reason = "forecast_timestamp_invalid"
    elif not timedelta(0) <= now - p.generated_at <= timedelta(seconds=60):
        reason = "forecast_stale_or_future"
    elif not timedelta(0) <= now - p.market_data_at <= timedelta(seconds=60):
        reason = "forecast_inputs_stale_or_future"
    elif p.market_data_at > p.generated_at:
        reason = "forecast_timestamp_invalid"
    elif abs((p.target_at - p.generated_at).total_seconds() - 1800) > 1:
        reason = "forecast_horizon_not_30_minutes"
    if reason:
        return Decision(stop, breached, breached, 'ordinary_trail_fallback:' + reason, risk)
    profit = max(0.0, p.expected_premium - ltp) * quantity
    exit_now = breached and profit < risk
    reason = ("forecast_trailing_exit" if exit_now else
              "stop_not_breached" if not breached else "forecast_upside_at_least_stop_risk")
    return Decision(stop, breached, exit_now, reason, risk, profit)

