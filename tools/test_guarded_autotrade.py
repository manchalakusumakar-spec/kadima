import os
import sys
import tempfile
import time
from types import SimpleNamespace

os.environ.setdefault(
    "KADIMA_DATA_DIR",
    tempfile.mkdtemp(prefix="kadima-autotrade-test-"),
)
os.environ["KADIMA_SESSION_AUTOTRADE"] = "1"

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), ".."),
)

from kadima import (
    angel_orders,
    auto_trade_guard,
    config,
    execution_ledger,
    market_session_gate,
)
from kadima.session import SESSION


FAILED = []


def check(name, result):
    print(f"  [{'ok' if result else 'FAIL'}] {name}")
    if not result:
        FAILED.append(name)


class Executor:
    def __init__(self, count=100):
        self._reconcile_blocked = False
        self._last_reconcile = time.monotonic()
        self._positions_verified = True

        # Intentionally huge number of filled positions.
        # Guard must completely ignore position COUNT.
        self.state = SimpleNamespace(
            positions={
                str(i): SimpleNamespace(
                    status="open",
                    remaining_qty=65,
                )
                for i in range(count)
            }
        )


class Order:
    def __init__(self, oid, status="open", tag=None):
        self.order_id = oid
        self.status = status
        self.raw = {"ordertag": tag}


def paper():
    config.AUTO_TRADE_ENABLED = True
    config.PAPER_TRADE = True
    config.LIVE_TRADING_ENABLED = False
    config.MANUAL_TRADE_PROTECTION = True
    config.ONE_POSITION_GUARD = True


def main():
    old_gate = market_session_gate.entry_block
    old_pending = execution_ledger.pending
    old_book = angel_orders.fetch_order_book
    old_api = SESSION.api

    try:
        paper()

        market_session_gate.entry_block = lambda exchange: None
        execution_ledger.pending = lambda: []
        angel_orders.fetch_order_book = lambda: []
        SESSION.api = object()

        ex = Executor(100)

        check(
            "paper/shadow automatic mode accepted",
            auto_trade_guard.mode_block() is None,
        )

        check(
            "100 existing filled positions do not block a new eligible trade",
            auto_trade_guard.entry_block(ex, "NFO") is None,
        )

        execution_ledger.pending = lambda: [
            {
                "intent": "K-UNKNOWN",
                "state": "unknown",
                "order_id": None,
            }
        ]

        check(
            "ledger timeout ambiguity blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "ledger_unresolved_order",
        )

        execution_ledger.pending = lambda: []

        angel_orders.fetch_order_book = lambda: [
            Order("MANUAL-1", "open", None)
        ]

        check(
            "manual pending broker order blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "manual_trade_pending",
        )

        angel_orders.fetch_order_book = lambda: None

        check(
            "unhealthy broker order book blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "broker_order_book_unhealthy",
        )

        angel_orders.fetch_order_book = lambda: []

        ex._reconcile_blocked = True

        check(
            "reconciliation unhealthy blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "reconciliation_unhealthy",
        )

        ex._reconcile_blocked = False

        SESSION.api = None

        check(
            "broker disconnected blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "broker_disconnected",
        )

        SESSION.api = object()

        config.LIVE_TRADING_ENABLED = True

        check(
            "paper/live conflict blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "paper_live_flag_conflict",
        )

        config.LIVE_TRADING_ENABLED = False
        config.MANUAL_TRADE_PROTECTION = False

        check(
            "manual protection cannot be disabled",
            auto_trade_guard.entry_block(ex, "NFO")
            == "manual_trade_protection_required",
        )

        config.MANUAL_TRADE_PROTECTION = True
        config.ONE_POSITION_GUARD = False

        check(
            "submission integrity protection cannot be disabled",
            auto_trade_guard.entry_block(ex, "NFO")
            == "submission_integrity_guard_required",
        )

        config.ONE_POSITION_GUARD = True
        os.environ["KADIMA_SESSION_AUTOTRADE"] = "0"

        check(
            "session audit must be armed",
            auto_trade_guard.entry_block(ex, "NFO")
            == "session_audit_not_armed",
        )

        os.environ["KADIMA_SESSION_AUTOTRADE"] = "1"

        market_session_gate.entry_block = (
            lambda exchange: "session_permit_expired"
        )

        check(
            "expired session permit blocks",
            auto_trade_guard.entry_block(ex, "NFO")
            == "session_audit_session_permit_expired",
        )

        market_session_gate.entry_block = lambda exchange: None
        paper()

        check(
            "paper mode cannot write real broker orders",
            auto_trade_guard.broker_writes_enabled() is False,
        )

    finally:
        market_session_gate.entry_block = old_gate
        execution_ledger.pending = old_pending
        angel_orders.fetch_order_book = old_book
        SESSION.api = old_api

    print("-" * 60)

    if FAILED:
        print("FAILED:")
        for name in FAILED:
            print(" -", name)
        return 1

    print("ALL GUARDED AUTO-TRADE TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
