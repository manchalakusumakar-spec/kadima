"""Capital/risk-based concurrency for independent long-option contracts.

No position-count cap, broker call, order submission, control write or imports
of live configuration. The shared executor's execution lock owns serialization.
One unresolved broker order blocks NEW submissions until reconciled; this does
not limit the number of already filled open positions. Exits are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Capacity:
    lots: int
    reason: str
    open_positions: int = 0
    cash_committed: float = 0.0
    stop_risk_committed: float = 0.0


def _number(value, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _identity(position):
    exchange, token = getattr(position, 'exchange', None), getattr(position, 'token', None)
    if (not isinstance(exchange, str) or not exchange or
            not isinstance(token, str) or not token or token.strip() != token):
        raise ValueError('portfolio_contract_identity_unknown')
    return exchange, token


def entry_block(positions, pending):
    """Fail closed on unfinished orders or an inconsistent local position book."""
    if not isinstance(positions, dict) or not isinstance(pending, list):
        return 'portfolio_state_unknown'
    if pending:
        return 'portfolio_order_pending_wait'
    identities = set()
    for position in positions.values():
        status = getattr(position, 'status', None)
        qty = getattr(position, 'remaining_qty', None)
        if type(qty) is not int or qty < 0:
            return 'portfolio_quantity_unknown'
        if status == 'closed':
            if qty:
                return 'portfolio_closed_quantity_unresolved'
            continue
        if status in ('entry_pending', 'exit_pending'):
            return 'portfolio_order_pending_wait'
        if status != 'open' or qty == 0:
            return 'portfolio_position_state_unknown'
        if getattr(position, 'kind', None) == 'cas_blast':
            return 'cas_owns_account_slot'
        try:
            key = _identity(position)
        except ValueError as exc:
            return str(exc)
        if key in identities:
            return 'portfolio_duplicate_contract_unresolved'
        identities.add(key)
    return None


def affordable_lots(positions, pending, *, exchange, token, limit_price, stop,
                    lot_size, requested_lots, broker_funds, capital_buffer_pct,
                    daily_limit, realized_pnl, cost_reserve, exit_buffer):
    """Keep existing per-trade sizing, then apply shared cash and loss headroom.

    Broker available funds can already reflect current margin. Deducting open
    entry costs again retains the existing capital guard's conservative rule;
    this is not a total-equity estimator. No expected sale proceeds, unrealized
    gains, pending cancellations or unconfirmed fills are credited.

    Aggregate modeled loss-to-stop plus estimated costs is capped by remaining
    daily loss allowance. Realized losses reduce it; profits do not expand it.
    This is a sizing constraint, not a guarantee of loss or stop-fill prices.
    """
    reason = entry_block(positions, pending)
    if reason:
        return Capacity(0, reason)
    if (not isinstance(exchange, str) or not exchange or not isinstance(token, str) or not token or
            type(lot_size) is not int or lot_size <= 0 or
            type(requested_lots) is not int or requested_lots <= 0 or
            not _number(limit_price, positive=True) or not _number(stop, positive=True) or stop >= limit_price or
            not _number(broker_funds) or not _number(daily_limit, positive=True) or
            type(realized_pnl) not in (int, float) or not math.isfinite(realized_pnl) or
            not _number(capital_buffer_pct) or capital_buffer_pct >= 100 or
            not _number(cost_reserve) or cost_reserve >= 1 or
            not _number(exit_buffer) or exit_buffer >= 1):
        return Capacity(0, 'portfolio_sizing_inputs_unavailable')
    cash, risk, count = 0.0, 0.0, 0
    for position in positions.values():
        if position.status == 'closed':
            continue
        if _identity(position) == (exchange, token):
            return Capacity(0, 'contract_already_open')
        entry = getattr(position, 'entry_price', None)
        structural = getattr(position, 'structural_stop', None)
        if not _number(entry, positive=True) or not _number(structural, positive=True):
            return Capacity(0, 'portfolio_existing_risk_unverifiable')
        qty = position.remaining_qty
        cash += entry * qty * (1 + cost_reserve)
        # Do not increase capacity by claiming a profitable stop is guaranteed.
        protective_reference = min(entry, structural)
        risk += (entry - protective_reference * (1 - exit_buffer) + entry * cost_reserve) * qty
        count += 1
    if not math.isfinite(cash) or not math.isfinite(risk):
        return Capacity(0, 'portfolio_totals_invalid')
    cash_room = broker_funds * (1 - capital_buffer_pct / 100) - cash
    risk_room = max(0.0, daily_limit + min(realized_pnl, 0.0)) - risk
    if cash_room <= 0:
        return Capacity(0, 'portfolio_cash_headroom_exhausted', count, cash, risk)
    if risk_room <= 0:
        return Capacity(0, 'portfolio_daily_risk_headroom_exhausted', count, cash, risk)
    cash_per_lot = limit_price * lot_size * (1 + cost_reserve)
    risk_per_lot = (limit_price - stop * (1 - exit_buffer) + limit_price * cost_reserve) * lot_size
    if not all(math.isfinite(x) and x > 0 for x in (cash_per_lot, risk_per_lot)):
        return Capacity(0, 'portfolio_lot_cost_invalid', count, cash, risk)
    allowed = min(requested_lots, int(cash_room // cash_per_lot), int(risk_room // risk_per_lot))
    if allowed < 1:
        reason = ('portfolio_cash_below_one_lot' if cash_room < cash_per_lot
                  else 'portfolio_risk_below_one_lot')
        return Capacity(0, reason, count, cash, risk)
    return Capacity(allowed, 'portfolio_sized_down' if allowed < requested_lots else 'ok', count, cash, risk)
