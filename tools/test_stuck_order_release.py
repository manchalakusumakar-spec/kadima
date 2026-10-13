"""Offline tests for the 2026-10-11 stuck-order release in execution_ledger.

No broker, no network. Every case drives the real ledger on a scratch database.

Run:  python tools/test_stuck_order_release.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

os.environ["KADIMA_DATA_DIR"] = tempfile.mkdtemp(prefix="kadima-stuck-")
os.environ["HOME"] = os.environ["KADIMA_DATA_DIR"]
os.environ.pop("KADIMA_CELESTIAL_ENABLED", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import socket  # noqa: E402


def _no_network(*a, **k):
    raise RuntimeError("network blocked in offline test")


socket.socket.connect = _no_network

from kadima import angel_orders, auto_executor, config, execution_ledger as L, storage  # noqa: E402
from kadima.config import IST  # noqa: E402

FAILED = []
BASE = datetime(2026, 10, 13, 10, 0, 0, tzinfo=IST)     # a Tuesday, mid-session
CLOCK = {"now": BASE}
L.now_ist = lambda: CLOCK["now"]
BOOK = {"rows": []}
CALLS = {"fetch": 0, "place": 0, "cancel": []}
ALERTS = []


def _fetch():
    CALLS["fetch"] += 1
    return None if BOOK["rows"] is None else list(BOOK["rows"])


angel_orders.fetch_order_book = _fetch
angel_orders.cancel_order = lambda oid, variety="NORMAL": (CALLS["cancel"].append(oid)
                                                           or angel_orders.OrderResult(True, oid, "ok"))
L.alerts.enqueue = lambda text, **kw: ALERTS.append((text, kw))
PLACE_RESULT = {"r": None}


def _place(**kw):
    CALLS["place"] += 1
    return PLACE_RESULT["r"]


angel_orders.place_limit_order = _place


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def reset():
    c = L.connection()
    with c:
        c.execute("DELETE FROM execution_orders")
        c.execute("DELETE FROM execution_fills")
    L._LATE_HANDLED.clear()
    L._REPRICE.clear()
    BOOK["rows"] = []
    CALLS.update(fetch=0, place=0, cancel=[])
    ALERTS.clear()
    CLOCK["now"] = BASE
    if hasattr(config, "UNKNOWN_ORDER_TIMEOUT_SEC"):
        delattr(config, "UNKNOWN_ORDER_TIMEOUT_SEC")


def position(sid, qty=0, status="open"):
    return auto_executor.Position(
        signal_id=sid, kind="gamma_blast", index="NIFTY", direction="CE",
        symbol=f"NIFTY{sid}CE", token=str(1000 + sid), exchange="NFO", entry_price=100.0,
        entry_ts=BASE, lots=1, lot_size=65, remaining_qty=qty, status=status,
        peak_price=100.0, structural_stop=80.0, effective_stop=80.0)


def lost_reply(pos, side="BUY", qty=65):
    """Submit an order whose Angel One reply is lost (uncertain, no order id)."""
    PLACE_RESULT["r"] = angel_orders.OrderResult(False, None, "timeout", uncertain=True)
    return L.submit(pos, side, qty, 100.0)


def accepted(pos, oid, side="BUY", qty=65):
    PLACE_RESULT["r"] = angel_orders.OrderResult(True, oid, "ok")
    return L.submit(pos, side, qty, 100.0)


def row_of(sid):
    return dict(L.connection().execute(
        "SELECT * FROM execution_orders WHERE signal_id=? ORDER BY created_at DESC", (sid,)).fetchone())


def book_row(oid, tag, status, filled=0, avg=None, side="BUY", token="1001"):
    return angel_orders.OrderStatus(oid, status, filled, avg,
                                    dict(ordertag=tag, symboltoken=token, exchange="NFO",
                                         transactiontype=side, producttype="INTRADAY", price="100"))


def after(seconds):
    CLOCK["now"] = BASE + timedelta(seconds=seconds)


# ------------------------------------------------------------------ cases
def test_before_timeout_unchanged():
    print("lost reply, not in book, before 60 s: unchanged (still blocks)")
    reset()
    pos = position(1)
    res = lost_reply(pos)
    positions = {"1": pos}
    after(30)
    check("submit reports submission_unknown", res.get("reason") == "submission_unknown")
    check("refresh unhealthy at 30 s", L.refresh(positions) is False)
    check("row still pending at 30 s", len(L.pending()) == 1)
    after(59)
    check("still unhealthy at 59 s", L.refresh(positions) is False)
    check("no alert before the timeout", not ALERTS)


def test_release_after_timeout():
    print("lost reply, not in book, at 60 s: released, never re-sent")
    reset()
    pos = position(2)
    lost_reply(pos)
    positions = {"2": pos}
    after(60)
    healthy = L.refresh(positions)
    row = row_of(2)
    check("refresh healthy after release", healthy is True)
    check("no pending rows left", L.pending() == [])
    check("row terminal with note unknown_timeout",
          row["state"] == "terminal" and row["note"] == L.UNKNOWN_TIMEOUT)
    check("BUY with nothing filled -> position closed", pos.status == "closed")
    check("order was sent exactly once (never re-sent)", CALLS["place"] == 1)
    check("one release alert", len(ALERTS) == 1 and "released it" in ALERTS[0][0])
    check("signal stays 'seen' so it cannot be submitted again", L.seen(2))
    after(120)
    check("next refresh healthy, no repeat alert", L.refresh(positions) is True and len(ALERTS) == 1)


def test_late_arrival_open_cancelled():
    print("released order appears later, still open: cancelled once, loud alert")
    reset()
    pos = position(3)
    lost_reply(pos)
    positions = {"3": pos}
    after(61)
    L.refresh(positions)
    intent = row_of(3)["intent"]
    ALERTS.clear()
    BOOK["rows"] = [book_row("LATE3", intent, "open")]
    after(90)
    check("refresh stays healthy", L.refresh(positions) is True)
    check("cancel sent for the late order", CALLS["cancel"] == ["LATE3"])
    check("one loud alert", len(ALERTS) == 1 and ALERTS[0][1].get("bypass") is True
          and "sent a cancel" in ALERTS[0][0])
    after(120)
    L.refresh(positions)
    check("handled only once (no repeat cancel/alert)", CALLS["cancel"] == ["LATE3"] and len(ALERTS) == 1)
    check("position not re-opened by the late order", pos.status == "closed" and pos.remaining_qty == 0)


def test_late_arrival_filled():
    print("released order appears later, already filled: loud alert, no cancel")
    reset()
    pos = position(4)
    lost_reply(pos)
    positions = {"4": pos}
    after(61)
    L.refresh(positions)
    intent = row_of(4)["intent"]
    ALERTS.clear()
    BOOK["rows"] = [book_row("LATE4", intent, "complete", 65, 101.0)]
    after(90)
    L.refresh(positions)
    check("no cancel for a completed order", CALLS["cancel"] == [])
    check("alert says FILLED and unmanaged", len(ALERTS) == 1 and "FILLED" in ALERTS[0][0])


def test_late_arrival_partly_filled():
    print("released order appears later, open with a partial fill: cancel + partial warning")
    reset()
    pos = position(5)
    lost_reply(pos)
    positions = {"5": pos}
    after(61)
    L.refresh(positions)
    intent = row_of(5)["intent"]
    ALERTS.clear()
    BOOK["rows"] = [book_row("LATE5", intent, "open", 30, 100.0)]
    after(90)
    L.refresh(positions)
    check("cancel sent", CALLS["cancel"] == ["LATE5"])
    check("alert names the 30 already filled", len(ALERTS) == 1 and "30 already filled" in ALERTS[0][0])


def test_late_arrival_rejected_quiet():
    print("released order appears later as rejected: logged, no alarm")
    reset()
    pos = position(6)
    lost_reply(pos)
    positions = {"6": pos}
    after(61)
    L.refresh(positions)
    intent = row_of(6)["intent"]
    ALERTS.clear()
    BOOK["rows"] = [book_row("LATE6", intent, "rejected")]
    after(90)
    L.refresh(positions)
    check("no cancel, no alert", CALLS["cancel"] == [] and ALERTS == [])


def test_accepted_missing_still_blocks():
    print("accepted order (has id) missing from book: unchanged (still blocks)")
    reset()
    pos = position(7)
    accepted(pos, "ACC7")
    positions = {"7": pos}
    after(300)
    check("unhealthy after 5 minutes", L.refresh(positions) is False)
    check("row still pending", len(L.pending()) == 1)
    check("nothing released, no alert", row_of(7)["note"] is None and not ALERTS)


def test_found_by_tag_unchanged():
    print("lost reply but order found in book by tag: filled normally (unchanged path)")
    reset()
    pos = position(8)
    lost_reply(pos)
    positions = {"8": pos}
    intent = row_of(8)["intent"]
    BOOK["rows"] = [book_row("TAG8", intent, "complete", 65, 100.0, token=pos.token)]
    after(10)
    check("healthy", L.refresh(positions) is True)
    check("fill applied: 65 owned, position open", pos.remaining_qty == 65 and pos.status == "open")
    check("not marked released", row_of(8)["note"] is None)


def test_previous_day_expired():
    print("unresolved row from a previous day: expired at first refresh")
    reset()
    CLOCK["now"] = BASE - timedelta(days=1)
    pos = position(9)
    accepted(pos, "OLD9")
    CLOCK["now"] = BASE - timedelta(days=1)
    pos_none = position(10)
    lost_reply(pos_none)
    after(5)
    positions = {"9": pos}          # signal 10 is no longer tracked by the executor
    check("healthy after expiry", L.refresh(positions) is True)
    check("both rows expired_previous_day",
          row_of(9)["note"] == L.EXPIRED_PREVIOUS_DAY and row_of(10)["note"] == L.EXPIRED_PREVIOUS_DAY)
    check("no pending rows left", L.pending() == [])
    check("no unknown-timeout alert for previous-day rows", not ALERTS)


def test_untracked_today_unchanged():
    print("today's row whose position is not tracked: unchanged (still blocks)")
    reset()
    pos = position(11)
    lost_reply(pos)
    after(120)
    check("unhealthy", L.refresh({}) is False)
    check("row kept", len(L.pending()) == 1)


def test_sell_release_keeps_exit():
    print("lost SELL released: position stays open with its exit reason latched")
    reset()
    pos = position(12, qty=65)
    pos.exit_reason = "structural_stop_breached"
    lost_reply(pos, side="SELL")
    positions = {"12": pos}
    check("position marked exit_pending while waiting", pos.status == "exit_pending")
    after(61)
    check("healthy after release", L.refresh(positions) is True)
    check("position open again with 65 owned", pos.status == "open" and pos.remaining_qty == 65)
    check("exit reason still latched for the monitor", pos.exit_reason == "structural_stop_breached")
    check("SELL not re-sent by the ledger itself", CALLS["place"] == 1)


def test_book_unreadable():
    print("order book unreadable")
    reset()
    pos = position(13)
    lost_reply(pos)
    positions = {"13": pos}
    BOOK["rows"] = None
    after(120)
    check("with a pending row: unhealthy (unchanged), nothing released",
          L.refresh(positions) is False and len(L.pending()) == 1)
    BOOK["rows"] = []
    L.refresh(positions)
    BOOK["rows"] = None
    check("only a released row today: healthy", L.refresh(positions) is True)


def test_no_rows_no_fetch():
    print("no orders at all: unchanged (healthy, book not fetched)")
    reset()
    check("healthy", L.refresh({}) is True)
    check("order book not fetched", CALLS["fetch"] == 0)


def test_timeout_floor():
    print("configured timeout below 30 s is ignored (60 s used)")
    reset()
    config.UNKNOWN_ORDER_TIMEOUT_SEC = 1
    check("1 s ignored -> 60 s", L._timeout_sec() == 60)
    config.UNKNOWN_ORDER_TIMEOUT_SEC = 90
    check("90 s honoured", L._timeout_sec() == 90)
    pos = position(14)
    lost_reply(pos)
    after(61)
    check("not released at 61 s when 90 s configured", L.refresh({"14": pos}) is False)
    after(91)
    check("released at 91 s", L.refresh({"14": pos}) is True)
    delattr(config, "UNKNOWN_ORDER_TIMEOUT_SEC")


def test_migration_adds_note_column():
    print("existing live database without the note column")
    path = os.path.join(tempfile.mkdtemp(prefix="kadima-mig-"), "old.db")
    c = sqlite3.connect(path)
    c.executescript('''CREATE TABLE execution_orders (
        intent TEXT PRIMARY KEY, signal_id INTEGER NOT NULL, side TEXT NOT NULL,
        quantity INTEGER NOT NULL, order_id TEXT, state TEXT NOT NULL,
        filled INTEGER NOT NULL DEFAULT 0, notional REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0);
        INSERT INTO execution_orders VALUES ('Kold',1,'BUY',65,'X1','terminal',65,6500,'2026-10-09T10:00:00+05:30',0);''')
    c.commit()
    c.close()
    real = storage.conn

    def old_conn():
        k = sqlite3.connect(path)
        k.row_factory = sqlite3.Row
        return k
    storage.conn = old_conn
    try:
        k = L.connection()
        cols = {r["name"] for r in k.execute("PRAGMA table_info(execution_orders)")}
        check("note column added", "note" in cols)
        check("existing row kept", k.execute("SELECT COUNT(*) FROM execution_orders").fetchone()[0] == 1)
        L.connection()
        check("second open is a no-op", True)
    finally:
        storage.conn = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("-" * 40)
    print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1 if FAILED else 0)
