"""Fail-closed protection for NEW KADIMA automatic entries.

IMPORTANT:
- There is NO numerical limit on filled/open positions.
- Multiple simultaneous positions are permitted whenever funds, portfolio
  capacity and existing risk controls permit.
- ONE_POSITION_GUARD is a legacy configuration name. Here it means:
  do not overlap unresolved order submissions.
- Gamma/CAS/Shield strategy scoring and selection are untouched.
"""
from __future__ import annotations

import os
import time

from . import (
    angel_orders,
    config,
    execution_ledger,
    market_session_gate,
)
from .session import SESSION


MAX_RECONCILE_AGE_SEC = 45.0


def mode_block():
    if not config.AUTO_TRADE_ENABLED:
        return "auto_trade_disabled"

    # Paper and live can never be armed simultaneously.
    if config.PAPER_TRADE and config.LIVE_TRADING_ENABLED:
        return "paper_live_flag_conflict"

    # Automatic trading needs exactly one execution destination.
    if not config.PAPER_TRADE and not config.LIVE_TRADING_ENABLED:
        return "execution_mode_not_selected"

    if not config.MANUAL_TRADE_PROTECTION:
        return "manual_trade_protection_required"

    if not config.ONE_POSITION_GUARD:
        return "submission_integrity_guard_required"

    return None


def _ledger_pending():
    """Fail closed on unreadable/ambiguous execution ledger."""
    try:
        rows = execution_ledger.pending()
    except Exception:
        return None

    if not isinstance(rows, list):
        return None

    return rows


def _broker_pending_block(ledger_rows):
    """Check broker order book before another automatic submission.

    Because any KADIMA non-terminal ledger row already blocks before this
    function is called, any remaining non-terminal broker order is external
    or manual and therefore blocks a NEW automatic entry.

    Filled/manual POSITIONS are deliberately not checked here and do not
    impose a global position-count restriction.
    """

    try:
        book = angel_orders.fetch_order_book()
    except Exception:
        return "broker_order_book_unhealthy"

    if book is None:
        return "broker_order_book_unhealthy"

    own_ids = {
        str(r.get("order_id"))
        for r in ledger_rows
        if r.get("order_id")
    }

    own_tags = {
        str(r.get("intent"))
        for r in ledger_rows
        if r.get("intent")
    }

    terminal = execution_ledger.TERMINAL

    for order in book:
        try:
            if order.status in terminal:
                continue

            if order.order_id and str(order.order_id) in own_ids:
                continue

            raw = order.raw or {}

            if raw.get("ordertag") and str(raw.get("ordertag")) in own_tags:
                continue

            return "manual_trade_pending"

        except Exception:
            return "broker_order_book_unhealthy"

    return None


def entry_block(executor, exchange):
    """Return None only when another NEW automatic submission is safe.

    Open-position count is intentionally irrelevant.
    """

    reason = mode_block()
    if reason:
        return reason

    # Existing session-calendar + expiring lease must be explicitly armed.
    if os.environ.get("KADIMA_SESSION_AUTOTRADE") != "1":
        return "session_audit_not_armed"

    try:
        reason = market_session_gate.entry_block(exchange)
    except Exception:
        return "session_audit_unhealthy"

    if reason:
        return "session_audit_" + str(reason)

    # Existing reconciliation remains authoritative.
    if getattr(executor, "_reconcile_blocked", True):
        return "reconciliation_unhealthy"

    last = getattr(executor, "_last_reconcile", 0.0)

    if (
        not isinstance(last, (int, float))
        or isinstance(last, bool)
        or last <= 0
        or not 0 <= time.monotonic() - last <= MAX_RECONCILE_AGE_SEC
    ):
        return "reconciliation_stale"

    # If this executor version exposes broker-position verification,
    # require it. Older versions without that field remain protected by
    # _reconcile_blocked and the direct broker order-book verification below.
    if hasattr(executor, "_positions_verified"):
        if not executor._positions_verified:
            return "broker_positions_unverified"

    # User requirement: disconnected broker blocks even PAPER decisions.
    if SESSION.api is None:
        return "broker_disconnected"

    pending = _ledger_pending()

    if pending is None:
        return "ledger_unhealthy"

    # Includes pending/submitting/unknown ledger states and therefore
    # submission-timeout ambiguity.
    if pending:
        return "ledger_unresolved_order"

    reason = _broker_pending_block(pending)

    if reason:
        return reason

    return None


def broker_writes_enabled():
    """True only for deliberately selected real LIVE broker mode."""

    return (
        config.AUTO_TRADE_ENABLED
        and config.LIVE_TRADING_ENABLED
        and not config.PAPER_TRADE
        and config.MANUAL_TRADE_PROTECTION
        and config.ONE_POSITION_GUARD
    )
