"""Offline tests for parallel entries, delivery-failure retry and unknown-order
timeout (2026-10-10). No broker, no network.

Run:  KADIMA_DATA_DIR=$(mktemp -d) python tools/test_parallel_orders.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("KADIMA_DATA_DIR", tempfile.mkdtemp(prefix="kadima-test-"))
os.environ.pop("KADIMA_CELESTIAL_ENABLED", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kadima import (angel_orders, auto_executor, config, execution_ledger,  # noqa: E402
                    portfolio_capacity, storage)
from kadima.config import IST  # noqa: E402

FAILED = []
ALERTS = []
execution_ledger.alerts.enqueue = lambda msg, **k: ALERTS.append(msg)


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def reset_db():
    c = execution_ledger.connection()
    with c:
        c.execute("DELETE FROM execution_orders")
        c.execute("DELETE FROM execution_fills")
        c.execute("DELETE FROM positions")
    ALERTS.clear()
    execution_ledger._LATE_ALERTED.clear()


def position(sid, token, index="NIFTY", status="open", qty=65, entry=100.0, stop=80.0):
    return auto_executor.Position(
        signal_id=sid, kind="gamma_blast", index=index, direction="CE",
        symbol=f"{index}{token}CE", token=token, exchange="NFO", entry_price=entry,
        entry_ts=datetime.now(IST), lots=1, lot_size=65, remaining_qty=qty,
        status=status, peak_price=entry or None, structural_stop=stop, effective_stop=stop)


def size(positions, pending, token="999", index="NIFTY", funds=100_000.0, **limits):
    return portfolio_capacity.affordable_lots(
        positions, pending, exchange="NFO", token=token, limit_price=100.0, stop=85.0,
        lot_size=65, requested_lots=1, broker_funds=funds, capital_buffer_pct=5.0,
        daily_limit=10_000, realized_pnl=0.0, cost_reserve=0.01, exit_buffer=0.025,
        underlying=index, **limits)


def pending_row(sid, price=100.0, qty=65, filled=0, side="BUY"):
    return dict(signal_id=sid, side=side, quantity=qty, filled=filled, price=price)


# ------------------------------------------------------------ capacity
def test_parallel_capacity():
    print("parallel entries: capacity and ceilings")
    open1 = position(1, "101")
    pend2 = position(2, "102", status="entry_pending", qty=0, entry=0.0)
    positions = {"1": open1, "2": pend2}
    pending = [pending_row(2)]
    cap = size(positions, pending)
    check("a pending BUY no longer blocks another contract", cap.lots == 1)
    check("pending BUY cash is reserved", abs(cap.cash_committed - 2 * 100 * 65 * 1.01) < 0.01)
    check("same contract as a pending BUY is refused",
          size(positions, pending, token="102").reason == "contract_already_open")
    check("max open positions enforced",
          size(positions, pending, max_open=2).reason == "portfolio_max_open_positions")
    check("max per underlying enforced",
          size(positions, pending, max_per_underlying=2).reason == "portfolio_max_per_underlying")
    check("other underlying unaffected by per-underlying ceiling",
          size(positions, pending, index="SENSEX", max_per_underlying=2).lots == 1)
    tight = size(positions, pending, funds=19_000)
    check("capital still the hard limit (pending reservation counted)",
          tight.lots == 0 and tight.reason == "portfolio_cash_below_one_lot")
    check("no headroom at all once reservations exceed funds",
          size(positions, pending, funds=13_000).reason == "portfolio_cash_headroom_exhausted")
    check("orphan order row fails closed",
          portfolio_capacity.entry_block({"1": open1}, [pending_row(7)]) == "portfolio_order_unresolved")
    check("unpriced pending order fails closed",
          size(positions, [pending_row(2, price=None)]).reason == "portfolio_pending_order_unpriced")
    check("pending status without an order row fails closed",
          portfolio_capacity.entry_block(positions, []) == "portfolio_position_state_unknown")


# ------------------------------------------------------------ retry
def _submit(sid, reason, ok=False, uncertain=False, price=100.0, token="201"):
    angel_orders.place_limit_order = lambda **k: angel_orders.OrderResult(
        ok, "OID1" if ok else None, reason, uncertain=uncertain)
    pos = position(sid, token, status="entry_pending", qty=0, entry=0.0)
    return pos, execution_ledger.submit(pos, "BUY", 65, price)


def _age_rows(sid, seconds):
    c = execution_ledger.connection()
    with c:
        c.execute("UPDATE execution_orders SET created_at=? WHERE signal_id=?",
                  ((datetime.now(IST) - timedelta(seconds=seconds)).isoformat(), sid))


def test_delivery_retry():
    print("delivery failures: bounded re-send")
    reset_db()
    pos, result = _submit(10, "rate_limited")
    check("rate limited is flagged retryable", result.get("retryable") is True)
    check("position released after delivery failure", pos.status == "closed")
    check("signal may be re-sent", execution_ledger.seen(10) is False)
    check("retry within 1% allowed", execution_ledger.retry_price_ok(10, 101.0))
    check("retry above 1% refused", not execution_ledger.retry_price_ok(10, 101.2))
    _submit(10, "session_unavailable")
    check("second delivery failure still re-sendable", execution_ledger.seen(10) is False)
    _submit(10, "rate_limited")
    check("stops after ORDER_RETRY_ATTEMPTS", execution_ledger.seen(10) is True)

    reset_db()
    _submit(11, "rate_limited")
    _age_rows(11, config.ORDER_RETRY_WINDOW_SEC + 5)
    check("stops after the retry window", execution_ledger.seen(11) is True)

    reset_db()
    _submit(12, "invalid_price")
    check("a non-delivery failure is never re-sent", execution_ledger.seen(12) is True)
    _submit(13, "ok", ok=True)
    check("an order Angel accepted is never re-sent", execution_ledger.seen(13) is True)


# ------------------------------------------------------------ unknown orders
def _book(*orders):
    angel_orders.fetch_order_book = lambda: list(orders)


def _broker(oid, tag, status="open", filled=0, avg=None):
    return angel_orders.OrderStatus(oid, status, filled, avg, dict(
        ordertag=tag, symboltoken="301", exchange="NFO", transactiontype="BUY",
        producttype="INTRADAY", price="100.0"))


def test_unknown_timeout():
    print("unknown orders: resolve by tag, time out, watch for late arrival")
    reset_db()
    angel_orders.cancel_order = lambda *a, **k: angel_orders.OrderResult(True, None, "ok")
    pos, result = _submit(20, "unparseable_broker_response", uncertain=True, token="301")
    positions = {"20": pos}
    intent = execution_ledger.pending(20)[0]["intent"]
    _book()
    check("young unknown order is awaited, not unhealthy", execution_ledger.refresh(positions) is True)
    check("still reserved while awaited", len(execution_ledger.pending()) == 1)

    _book(_broker("B1", intent, status="complete", filled=65, avg=99.5))
    execution_ledger.refresh(positions)
    check("found by order tag and fill applied", pos.status == "open" and pos.remaining_qty == 65)
    check("order id recorded", pos.order_id == "B1")

    reset_db()
    pos, _ = _submit(21, "submission_exception", uncertain=True, token="301")
    positions = {"21": pos}
    intent = execution_ledger.pending(21)[0]["intent"]
    _age_rows(21, config.UNKNOWN_ORDER_TIMEOUT_SEC + 1)
    _book()
    check("timeout does not mark the engine unhealthy", execution_ledger.refresh(positions) is True)
    check("timed-out order released", execution_ledger.pending() == [] and pos.status == "closed")
    check("timed-out order is never re-sent", execution_ledger.seen(21) is True)
    check("timeout alert raised", any("released it" in a for a in ALERTS))
    _book(_broker("LATE", intent))
    execution_ledger.refresh({})
    check("late arrival raises a loud alert", any("AFTER KADIMA timed it out" in a for a in ALERTS))
    n = len(ALERTS)
    execution_ledger.refresh({})
    check("late alert raised once", len(ALERTS) == n)


def test_previous_day_stuck_row():
    print("stuck row from an earlier session (the #237 case)")
    reset_db()
    pos, _ = _submit(237, "unparseable_broker_response", uncertain=True, token="301")
    c = execution_ledger.connection()
    with c:
        c.execute("UPDATE execution_orders SET created_at=? WHERE signal_id=237",
                  ((datetime.now(IST) - timedelta(days=2)).isoformat(),))
    _book()
    check("tracked previous-day row expires", execution_ledger.refresh({"237": pos}) is True)
    check("no longer blocks entries", execution_ledger.pending() == [])

    reset_db()
    _submit(238, "unparseable_broker_response", uncertain=True, token="301")
    with c:
        c.execute("UPDATE execution_orders SET created_at=? WHERE signal_id=238",
                  ((datetime.now(IST) - timedelta(days=1)).isoformat(),))
    check("untracked previous-day row expires too", execution_ledger.refresh({}) is True
          and execution_ledger.pending() == [])

    reset_db()
    _submit(239, "unparseable_broker_response", uncertain=True, token="301")
    check("untracked SAME-day row still fails closed", execution_ledger.refresh({}) is False)


def test_migration():
    print("schema migration")
    cols = {r["name"] for r in execution_ledger.connection().execute("PRAGMA table_info(execution_orders)")}
    check("price and note columns exist", {"price", "note"} <= cols)


if __name__ == "__main__":
    test_migration()
    test_parallel_capacity()
    test_delivery_retry()
    test_unknown_timeout()
    test_previous_day_stuck_row()
    print("-" * 40)
    print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1 if FAILED else 0)
