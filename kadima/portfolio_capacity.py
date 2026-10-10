"""Capital/risk-based concurrency for independent long-option contracts.

No broker call, order submission, control write or imports of live
configuration (limits are passed in by the caller). The shared executor's
execution lock owns serialization.

2026-10-10: several entries may be open or pending at once. A pending order no
longer blocks unrelated signals; it blocks only a second order on the same
contract. Every pending BUY reserves its unfilled cash and loss-to-stop at its
own limit price, so simultaneous signals cannot all size against the same
balance. Inconsistent local state (orphan order rows, unknown statuses,
duplicate contracts, unpriced pending orders) still fails closed. Exits are
unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

ACTIVE = ('open', 'entry_pending', 'exit_pending')


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


def _pending_by_signal(pending):
    out = {}
    for row in pending:
        try:
            out.setdefault(str(row['signal_id']), []).append(row)
        except (KeyError, TypeError):
            return None
    return out


def entry_block(positions, pending, *, max_open=None, max_per_underlying=None, underlying=None):
    """Fail closed on inconsistent local state; otherwise apply count ceilings."""
    if not isinstance(positions, dict) or not isinstance(pending, list):
        return 'portfolio_state_unknown'
    rows = _pending_by_signal(pending)
    if rows is None:
        return 'portfolio_state_unknown'
    for sid, signal_rows in rows.items():
        position = positions.get(sid)
        # An order the executor does not track cannot be valued or sized around.
        if position is None or getattr(position, 'status', None) not in ('entry_pending', 'exit_pending'):
            return 'portfolio_order_unresolved'
        if len(signal_rows) != 1:
            return 'portfolio_order_unresolved'
    identities, active, same_underlying = set(), 0, 0
    for key, position in positions.items():
        status = getattr(position, 'status', None)
        qty = getattr(position, 'remaining_qty', None)
        if type(qty) is not int or qty < 0:
            return 'portfolio_quantity_unknown'
        if status == 'closed':
            if qty:
                return 'portfolio_closed_quantity_unresolved'
            continue
        if status not in ACTIVE:
            return 'portfolio_position_state_unknown'
        if status in ('open', 'exit_pending') and qty == 0:
            return 'portfolio_position_state_unknown'
        if status in ('entry_pending', 'exit_pending') and str(key) not in rows:
            # Pending locally but no order row: state is not trustworthy.
            return 'portfolio_position_state_unknown'
        try:
            identity = _identity(position)
        except ValueError as exc:
            return str(exc)
        if identity in identities:
            return 'portfolio_duplicate_contract_unresolved'
        identities.add(identity)
        active += 1
        if underlying is not None and getattr(position, 'index', None) == underlying:
            same_underlying += 1
    if max_open is not None and active >= max_open:
        return 'portfolio_max_open_positions'
    if max_per_underlying is not None and underlying is not None and same_underlying >= max_per_underlying:
        return 'portfolio_max_per_underlying'
    return None


def affordable_lots(positions, pending, *, exchange, token, limit_price, stop,
                    lot_size, requested_lots, broker_funds, capital_buffer_pct,
                    daily_limit, realized_pnl, cost_reserve, exit_buffer,
                    max_open=None, max_per_underlying=None, underlying=None):
    """Keep existing per-trade sizing, then apply shared cash and loss headroom.

    Broker available funds can already reflect current margin. Deducting open
    entry costs again retains the existing capital guard's conservative rule;
    this is not a total-equity estimator. No expected sale proceeds, unrealized
    gains, pending cancellations or unconfirmed fills are credited. Pending BUYs
    are charged at their full unfilled quantity and limit price.

    Aggregate modeled loss-to-stop plus estimated costs is capped by remaining
    daily loss allowance. Realized losses reduce it; profits do not expand it.
    This is a sizing constraint, not a guarantee of loss or stop-fill prices.
    """
    reason = entry_block(positions, pending, max_open=max_open,
                         max_per_underlying=max_per_underlying, underlying=underlying)
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
    rows = _pending_by_signal(pending)
    cash, risk, count = 0.0, 0.0, 0
    for key, position in positions.items():
        if position.status == 'closed':
            continue
        if _identity(position) == (exchange, token):
            return Capacity(0, 'contract_already_open')
        structural = getattr(position, 'structural_stop', None)
        if not _number(structural, positive=True):
            return Capacity(0, 'portfolio_existing_risk_unverifiable')
        qty = position.remaining_qty
        if qty:
            entry = getattr(position, 'entry_price', None)
            if not _number(entry, positive=True):
                return Capacity(0, 'portfolio_existing_risk_unverifiable')
            cash += entry * qty * (1 + cost_reserve)
            # Do not increase capacity by claiming a profitable stop is guaranteed.
            protective_reference = min(entry, structural)
            risk += (entry - protective_reference * (1 - exit_buffer) + entry * cost_reserve) * qty
        for row in rows.get(str(key), []):
            if row.get('side') != 'BUY':
                continue
            unfilled = row.get('quantity', 0) - row.get('filled', 0)
            price = row.get('price')
            if type(unfilled) is not int or unfilled < 0 or (unfilled and not _number(price, positive=True)):
                return Capacity(0, 'portfolio_pending_order_unpriced')
            if unfilled:
                cash += price * unfilled * (1 + cost_reserve)
                reference = min(price, structural)
                risk += (price - reference * (1 - exit_buffer) + price * cost_reserve) * unfilled
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
