"""Gamma Blast priority (trader rule, 2026-10-20). Exits only - never opens anything.

"No other trade may stand in the way of a Gamma Blast; open trades - manual ones
too - can be exited to make room."

When a Gamma Blast that passed every other entry check is refused only because
of capital/risk headroom, or because a manual position in the same index blocks
it, KADIMA exits other open positions to make room, then the Gamma is retried on
its next evaluation (the existing entry path, with every check, runs again).

Order, lowest priority first (trader choice): Scalp -> Intraday/other auto ->
CAS -> Manual. Within a group the weakest P&L goes first. One exit per Gamma
attempt, so only what is needed is freed (the next attempt sees the new room;
while that SELL is pending the entry waits on portfolio_order_pending_wait).

Never: exit another Gamma Blast position; exit while the kill switch is on; exit
a position whose loss would breach the daily loss limit; exit without a fresh
price; exit a position with an order already pending.

Mode: DATA_DIR/gamma_priority.json {"mode": "off|shadow|live"} (re-read every
30 s; env KADIMA_GAMMA_PRIORITY overrides). shadow logs gamma_priority_would_exit
and never sells.
"""
from __future__ import annotations

import json
import math
import os
import time

from . import audit
from .config import DATA_DIR

DEFAULTS = {'mode': 'shadow'}
MODES = ('off', 'shadow', 'live')
CAPITAL_REASONS = ('portfolio_cash_headroom_exhausted', 'portfolio_daily_risk_headroom_exhausted',
                   'portfolio_cash_below_one_lot', 'portfolio_risk_below_one_lot',
                   'risk_budget_no_qualifying_strike')   # allocation follows live capital
CAPITAL_PREFIXES = ('capital_guard_',)
MANUAL_REASONS = ('manual_carryforward_same_underlying', 'contract_held_outside_kadima')
MANUAL_KINDS = ('MANUAL_PROTECTED', 'MANUAL_ADOPTED')
GROUP = {'fast_momentum': 0, 'stock_fast': 0, 'cas': 2, 'cas_blast': 2}   # other auto kinds = 1
ATTEMPT_GAP_SEC = 5.0
EXIT_REASON = 'gamma_priority_exit'

_cfg = {'at': 0.0, 'value': None}
_last: dict = {}


def mode():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'gamma_priority.json')) as f:
                value.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError, AttributeError):
            pass
        value['mode'] = os.environ.get('KADIMA_GAMMA_PRIORITY', value['mode'])
        if value['mode'] not in MODES:
            value['mode'] = DEFAULTS['mode']
        _cfg.update(at=now, value=value)
    return _cfg['value']['mode']


def blocking(reason):
    """'capital' | 'manual' | None for an entry rejection reason."""
    if not isinstance(reason, str):
        return None
    if reason in CAPITAL_REASONS or reason.startswith(CAPITAL_PREFIXES):
        return 'capital'
    if reason in MANUAL_REASONS:
        return 'manual'
    return None


def group(pos):
    kind = getattr(pos, 'kind', None)
    if kind in MANUAL_KINDS:
        return 3
    return GROUP.get(kind, 1)


def _finite(*v):
    try:
        return all(x is not None and math.isfinite(float(x)) and float(x) > 0 for x in v)
    except (TypeError, ValueError):
        return False


def candidates(positions, index_name, why, price_of, realized_today, daily_limit, pending=lambda p: False,
               token=None):
    """Pure ordering of exitable positions: [(pos, ltp, pnl_pct, unrealized)], first = exit first."""
    out = []
    for pos in positions:
        if getattr(pos, 'status', None) != 'open' or (getattr(pos, 'remaining_qty', 0) or 0) <= 0:
            continue
        if getattr(pos, 'kind', None) == 'gamma_blast':
            continue                       # never exit one Gamma for another
        if why == 'manual':
            same_index = getattr(pos, 'index', None) == index_name
            same_contract = token is not None and str(getattr(pos, 'token', '')) == str(token)
            if getattr(pos, 'kind', None) not in MANUAL_KINDS or not (same_index or same_contract):
                continue
        if pending(pos):
            continue
        ltp = price_of(pos)
        if not _finite(ltp, getattr(pos, 'entry_price', None)):
            continue
        unrealized = (ltp - pos.entry_price) * pos.remaining_qty
        if realized_today + min(0.0, unrealized) <= -daily_limit:
            continue                       # booking this loss would breach the daily limit
        out.append((pos, ltp, (ltp / pos.entry_price - 1) * 100, unrealized))
    out.sort(key=lambda x: (group(x[0]), x[2]))
    return out


def make_room(executor, index_name, direction, reason, signal_id=None, token=None, now=None):
    """Called after a Gamma Blast entry was refused. Exits at most one position."""
    try:
        m = mode()
        why = blocking(reason)
        if m == 'off' or why is None:
            return None
        clock = time.monotonic()
        key = (index_name, direction)
        if clock - _last.get(key, 0) < ATTEMPT_GAP_SEC:
            return None
        _last[key] = clock
        from . import execution_ledger, exit_manager, killswitch
        from .config import DAILY_LOSS_LIMIT_INR
        if killswitch.engaged():
            return None
        tracker = executor.state.premium_tracker
        ranked = candidates(
            list(executor.state.positions.values()), index_name, why,
            lambda p: tracker.latest(p.token, 60.0), exit_manager.realized_pnl_today(), DAILY_LOSS_LIMIT_INR,
            pending=lambda p: bool(execution_ledger.pending(p.signal_id)), token=token)
        if not ranked:
            audit.log('gamma_priority_nothing_to_exit', index=index_name, direction=direction,
                      gamma_signal=signal_id, blocked_by=reason, mode=m)
            return None
        pos, ltp, pnl_pct, unrealized = ranked[0]
        info = dict(index=index_name, direction=direction, gamma_signal=signal_id, blocked_by=reason,
                    exit_signal=pos.signal_id, exit_kind=pos.kind, exit_symbol=getattr(pos, 'symbol', None),
                    exit_ltp=round(ltp, 2), exit_pnl_pct=round(pnl_pct, 2), exit_pnl_inr=round(unrealized, 2),
                    queue=len(ranked), mode=m)
        if m == 'shadow':
            audit.log('gamma_priority_would_exit', **info)
            return dict(info, ok=None)
        result = executor._close_position(pos, ltp, EXIT_REASON)
        audit.log('gamma_priority_exit', ok=bool((result or {}).get('ok')), **info)
        return dict(info, result=result)
    except Exception as exc:  # noqa: BLE001 - never disturb the entry path
        audit.log('gamma_priority_error', error=type(exc).__name__)
        return None
