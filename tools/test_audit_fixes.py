"""Offline tests for the 2026-09-29 audit fixes. No broker, no network.

Run:  KADIMA_DATA_DIR=/tmp/kadima-test python tools/test_audit_fixes.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("KADIMA_DATA_DIR", tempfile.mkdtemp(prefix="kadima-test-"))
os.environ.pop("KADIMA_CELESTIAL_ENABLED", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kadima import (angel_orders, auto_executor, capital_guard, entry_quality,  # noqa: E402
                    execution_ledger, storage, trade_policy)
from kadima.config import IST  # noqa: E402

FAILED = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def position(sid=1, token="111", qty=65, status="open", entry=100.0, stop=80.0):
    return auto_executor.Position(
        signal_id=sid, kind="gamma_blast", index="NIFTY", direction="CE",
        symbol=f"NIFTY{token}CE", token=token, exchange="NFO", entry_price=entry,
        entry_ts=datetime.now(IST), lots=1, lot_size=65, remaining_qty=qty,
        status=status, peak_price=entry, structural_stop=stop, effective_stop=stop)


# ------------------------------------------------------------------ fix 1
def test_bear_shield_band():
    print("fix 1: PE Shield capital band")
    capital_guard.broker_available_funds = lambda: 100_000.0
    check("bull_shield gets 30k", capital_guard.get_capital_for_signal("bull_shield", "CONFIRMED") == 30_000.0)
    check("bear_shield gets 30k", capital_guard.get_capital_for_signal("bear_shield", "CONFIRMED") == 30_000.0)
    check("unknown kind still 0", capital_guard.get_capital_for_signal("other", "CONFIRMED") == 0.0)


# ------------------------------------------------------------------ fix 3
def _reconcile(ex, broker_rows, book):
    auto_executor.fetch_broker_positions = lambda: broker_rows
    angel_orders.fetch_order_book = lambda: book
    ex.reconcile_on_startup()


def _order(oid, token, status="open", tag=None):
    return angel_orders.OrderStatus(oid, status, 0, None,
        dict(exchange="NFO", symboltoken=token, ordertag=tag))


def test_reconcile_scoped():
    print("fix 3: reconcile only KADIMA-owned contracts")
    execution_ledger.refresh = lambda positions: True
    execution_ledger.pending = lambda signal_id=None: []
    storage.load_open_positions = lambda: []
    kad = dict(exchange="NFO", symboltoken="111", producttype="INTRADAY", netqty="65")
    manual = dict(exchange="NFO", symboltoken="999", producttype="INTRADAY", netqty="75")

    ex = auto_executor.AutoExecutor()
    ex.state.positions["1"] = position()
    _reconcile(ex, [kad, manual], [_order("M1", "888")])
    check("manual position + order elsewhere: exits monitored", "1" in ex._exit_verified and ex._positions_verified)
    check("manual position elsewhere: entries allowed", ex._reconcile_blocked is False)
    check("manual contracts recorded", ("NFO", "999") in ex._external_contracts and ("NFO", "888") in ex._external_contracts)

    ex = auto_executor.AutoExecutor()
    ex.state.positions["1"] = position()
    _reconcile(ex, [dict(kad, netqty="130")], [])
    check("broker holds MORE of our contract: exits run", "1" in ex._exit_verified)
    check("broker holds MORE of our contract: entries blocked", ex._reconcile_blocked is True)

    ex = auto_executor.AutoExecutor()
    ex.state.positions["1"] = position()
    auto_executor.alerts.enqueue = lambda *a, **k: None
    _reconcile(ex, [dict(kad, netqty="0")], [])
    check("broker holds LESS: exits paused for that position (no oversell)", "1" not in ex._exit_verified)
    check("broker holds LESS: entries blocked", ex._reconcile_blocked is True)

    ex = auto_executor.AutoExecutor()
    ex.state.positions["1"] = position()
    _reconcile(ex, [kad], [_order("M2", "111")])
    check("manual order on OUR contract blocks entries", ex._reconcile_blocked is True)
    check("manual order on our contract: exits still run", "1" in ex._exit_verified)

    ex = auto_executor.AutoExecutor()
    ex.state.positions["1"] = position()
    auto_executor.fetch_broker_positions = lambda: None
    ex.reconcile_on_startup()
    check("broker unreadable: fails closed", ex._positions_verified is False and ex._reconcile_blocked is True)


if __name__ == "__main__":
    for t in [v for k, v in list(globals().items()) if k.startswith("test_")]:
        t()
    print("-" * 40)
    print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1 if FAILED else 0)
