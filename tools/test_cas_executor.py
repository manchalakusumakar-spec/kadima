#!/usr/bin/env python3
"""Offline tests for CAS auto-execution -- no network, no broker. Run after any edit."""
import os, sys, tempfile
os.environ["KADIMA_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, ".")

from datetime import datetime, timedelta
from types import SimpleNamespace

from kadima import (angel_orders, auto_executor, cas_blast_scanner, cas_executor,
                    eod_watchdog, execution_ledger, storage)
from kadima.config import IST
from kadima.option_chain import OptionQuote

fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond: fails.append(name)

T0 = datetime(2026, 9, 29, 15, 10, tzinfo=IST)
clock = {"now": T0}
now = lambda: clock["now"]
for mod in (cas_blast_scanner, cas_executor, auto_executor, execution_ledger):
    mod.now_ist = now

print("product type")
check("CAS -> CARRYFORWARD", angel_orders.product_for(SimpleNamespace(kind="cas_blast")) == "CARRYFORWARD")
check("others -> INTRADAY", angel_orders.product_for(SimpleNamespace(kind="gamma_blast")) == "INTRADAY")

print("sizing and price ladder")
check("10k at cap 6.5, lot 75 -> 20 lots", cas_executor.size_order("NIFTY", 10000, 6.5, 75) == (20, 1500))
check("freeze limit clamps quantity", cas_executor.size_order("NIFTY", 10000, 1.0, 75)[1] <= 1800)
check("budget below one lot -> 0", cas_executor.size_order("NIFTY", 300, 6.5, 75) == (0, 0))
lad = cas_executor.price_ladder(dict(ltp=5.2, bid=5.0, ask=5.4), cap=6.5)
check("ladder starts at bid", lad[0] == 5.0)
check("ladder increasing and capped", all(a < b for a, b in zip(lad, lad[1:])) and max(lad) <= 6.5)
check("ladder never above cap", max(cas_executor.price_ladder(dict(ltp=6.4, bid=6.3, ask=7.0), 6.5)) <= 6.5)

print("scanner trigger")
class Chain:
    def __init__(self):
        self.index = SimpleNamespace(strike_step=50, opt_exchange="NFO")
        self.quotes = {}
    def set(self, ce, pe, cum_ce, cum_pe):
        self.quotes = {
            "CE1": OptionQuote("CE1", "NIFTY29SEP2622700CE", 22700, "CE", ltp=ce, volume=cum_ce, bid=ce-.1, ask=ce+.1),
            "PE1": OptionQuote("PE1", "NIFTY29SEP2622600PE", 22600, "PE", ltp=pe, volume=cum_pe, bid=pe-.1, ask=pe+.1)}
fired = []
sc = cas_blast_scanner.CasBlastScanner(executor_hook=lambda c, ch: fired.append(c))
ch = Chain(); cum = 0; spot = 22660.0
for i in range(10):                         # pre-window: builds history, never triggers
    cum += 1000; ch.set(5.0, 6.0, cum, cum)
    clock["now"] = T0 + timedelta(seconds=15 * i)
    sc.on_chain("NIFTY", ch, spot)
check("no trigger before 15:15", not fired)
clock["now"] = datetime(2026, 9, 29, 15, 22, tzinfo=IST)
for ce, spot_i in ((5.0, 22660), (5.5, 22670)):
    cum += 1000; ch.set(ce, 6.0, cum, cum); sc.on_chain("NIFTY", ch, spot_i)
cum += 9000; ch.set(7.5, 4.0, cum, cum); sc.on_chain("NIFTY", ch, 22690)
check("CE blast triggers once", len(fired) == 1 and fired[0].option_type == "CE")
check("PE ignored while spot rises", all(c.option_type == "CE" for c in fired))
cum += 9000; ch.set(9.0, 3.0, cum, cum); sc.on_chain("NIFTY", ch, 22700)
check("same strike debounced", len(fired) == 1)
check("window closes at 15:35", not cas_blast_scanner.in_cas_window(datetime(2026, 9, 29, 15, 35, tzinfo=IST)))

print("exit rule")
Pos = auto_executor.Position
p = Pos(signal_id=1, kind="cas_blast", index="NIFTY", direction="CE", symbol="X", token="T",
        exchange="NFO", entry_price=5.0, entry_ts=now(), lots=1, lot_size=75, remaining_qty=75)
check("no stop-loss at entry", cas_executor.exit_reason(p, 1.0, False, False) is None)
p.peak_price = 6.4
check("not armed below +30%", cas_executor.exit_reason(p, 5.0, False, False) is None)
p.peak_price = 20.0
check("armed after +30%: holds above giveback", cas_executor.exit_reason(p, 16.0, False, False) is None and p.trail_active)
check("exits on 25% fall from peak", cas_executor.exit_reason(p, 15.0, False, False) == "cas_peak_trail")
q = Pos(signal_id=2, kind="cas_blast", index="NIFTY", direction="CE", symbol="Y", token="U",
        exchange="NFO", entry_price=10.0, entry_ts=now(), lots=1, lot_size=75, remaining_qty=75, peak_price=13.0)
check("armed trail never below entry", cas_executor.exit_reason(q, 10.5, False, False) is None and q.effective_stop == 10.0)
with storage.conn() as c:
    c.execute("DELETE FROM positions WHERE signal_id=2")
check("EOD overrides", cas_executor.exit_reason(p, 19.0, False, True) == "eod_force_close")
check("daily loss overrides", cas_executor.exit_reason(p, 19.0, True, False) == "daily_loss_limit_breached")
with storage.conn() as c:            # the unit-test position above is not a real trade
    c.execute("DELETE FROM positions WHERE signal_id=1")

print("end-to-end entry (dry run) and exit order product")
sent = []
real_place = angel_orders.place_limit_order
def fake_place(**kw):
    sent.append(kw)
    return angel_orders.OrderResult(True, f"DRY{len(sent)}", "dry_run_not_sent", dry_run=True)
angel_orders.place_limit_order = fake_place
ex = auto_executor.EXECUTOR
ex._reconcile_blocked = False; ex._positions_verified = True
cas_executor.capital_guard.broker_available_funds = lambda: 100908.94
cas_executor.fetch_quote = lambda e, t: dict(ltp=7.5, bid=7.4, ask=7.6)
cas_executor.get_lot_size = lambda s, t: 75
cas_executor.market_session_gate.entry_block = lambda e, **k: None
cand = fired[0]
clock["now"] = cand.detected_at + timedelta(seconds=3)
r = cas_executor.enter(cand)
pos = next(p for p in ex.state.positions.values() if p.kind == "cas_blast")
check("entry accepted", r.get("ok"))
check("BUY sent as CARRYFORWARD", sent and sent[0]["producttype"] == "CARRYFORWARD" and sent[0]["transactiontype"] == "BUY")
check("first price is best bid", sent[0]["price"] == 7.4)
check("size within 10k at cap", pos.remaining_qty * 9.7 <= 10000 and pos.remaining_qty % 75 == 0 and pos.remaining_qty > 0)
check("second CAS entry blocked", cas_executor.enter(cand)["reason"] in
      ("cas_daily_trade_limit", "account_position_already_open", "portfolio_order_pending_wait"))
auto_executor._poll_ltp = lambda exch, toks: {t: 30.0 for t in toks}
ex.check_exits(kinds={"cas_blast"})
auto_executor._poll_ltp = lambda exch, toks: {t: 22.0 for t in toks}
ex.check_exits(kinds={"cas_blast"})
check("trail exit SELL sent as CARRYFORWARD",
      len(sent) == 2 and sent[1]["transactiontype"] == "SELL" and sent[1]["producttype"] == "CARRYFORWARD")
check("position closed with trail reason", storage.conn().execute(
      "SELECT exit_reason FROM positions WHERE signal_id=?", (pos.signal_id,)).fetchone()[0] == "cas_peak_trail")
angel_orders.place_limit_order = real_place

print("spread gate")
cas_executor.fetch_quote = lambda e, t: dict(ltp=7.5, bid=6.0, ask=7.6)
ex.state.positions.clear()
with storage.conn() as c:
    c.execute("DELETE FROM positions")
cand.detected_at = now() - timedelta(seconds=2)
check("wide spread rejected", cas_executor.enter(cand)["reason"] == "spread_too_wide")

print("entry price stepping (live path, faked broker)")
calls = []
cas_executor.CAS_ENTRY_STEP_SEC = 0
cas_executor.angel_orders = SimpleNamespace(
    get_order_status=lambda oid: angel_orders.OrderStatus(oid, "open", 0, None, {}),
    modify_order=lambda oid, price, quantity, contract, side=None: (calls.append(("modify", price, contract["producttype"]))
                 or angel_orders.OrderResult(True, oid, "ok")),
    cancel_order=lambda oid: calls.append(("cancel",)) or angel_orders.OrderResult(True, oid, "ok"))
quotes = iter([dict(ltp=7.5, bid=7.4, ask=7.6), dict(ltp=8.4, bid=8.3, ask=8.6),
               dict(ltp=9.5, bid=9.4, ask=9.8)])
cas_executor.fetch_quote = lambda e, t: next(quotes)
cas_executor._step_entry(SimpleNamespace(symbol="X", token="T", exchange="NFO", signal_id=9),
                         "OID", 1500, 7.4, 9.7)
prices = [c[1] for c in calls if c[0] == "modify"]
check("steps follow the live market up", prices == [7.5, 8.6, 9.7])
check("never above the 1.3x cap", max(prices) <= 9.7)
check("modifies keep CARRYFORWARD", all(c[2] == "CARRYFORWARD" for c in calls if c[0] == "modify"))
check("unfilled remainder cancelled", calls[-1] == ("cancel",))
calls.clear()
cas_executor.angel_orders.get_order_status = lambda oid: angel_orders.OrderStatus(oid, "complete", 1500, 7.4, {})
cas_executor._step_entry(SimpleNamespace(symbol="X", token="T", exchange="NFO", signal_id=9),
                         "OID", 1500, 7.4, 9.7)
check("filled at bid: no modify, no cancel", calls == [])
cas_executor.angel_orders = angel_orders

if hasattr(eod_watchdog, "_fetch_positions"):      # GitHub-style watchdog places its own orders
    print("EOD watchdog")
    closed = []
    eod_watchdog._close_position = lambda p: closed.append(p["tradingsymbol"])
    eod_watchdog._fetch_positions = lambda: [
        dict(tradingsymbol="MANUALCF", exchange="MCX", symboltoken="M1", producttype="CARRYFORWARD", netqty="1"),
        dict(tradingsymbol="CASCF", exchange="NFO", symboltoken="C1", producttype="CARRYFORWARD", netqty="75"),
        dict(tradingsymbol="MIS", exchange="NFO", symboltoken="I1", producttype="INTRADAY", netqty="75")]
    from kadima import cas_eod_guard
    cas_eod_guard.owned_cas_contracts = lambda: {("NFO", "C1")}
    eod_watchdog.now_ist = lambda: datetime(2026, 9, 29, 15, 25, tzinfo=IST)
    eod_watchdog.main()
    check("15:25: closes MIS only", closed == ["MIS"])
    closed.clear(); eod_watchdog.now_ist = lambda: datetime(2026, 9, 29, 15, 39, tzinfo=IST)
    eod_watchdog.main()
    check("15:39: closes KADIMA CAS too, never manual CF", closed == ["CASCF", "MIS"])
else:
    print("EOD watchdog: engine-owned design (exits via executor._close_position) - covered above")


print("0.05 tick grid (CARRYFORWARD only)")
check("BUY rounds up to tick", angel_orders.to_tick(7.43, "BUY") == 7.45)
check("SELL rounds down to tick", angel_orders.to_tick(7.43, "SELL") == 7.40)
check("exact tick kept", angel_orders.to_tick(0.05, "SELL") == 0.05 and angel_orders.to_tick(7.45, "BUY") == 7.45)
check("never below one tick", angel_orders.to_tick(0.02, "SELL") == 0.05)
check("CF price keeps Rs 0.05", angel_orders._price_str(0.05, "CARRYFORWARD", "SELL") == "0.05")
check("CF price on grid, 2dp", angel_orders._price_str(7.43, "CARRYFORWARD", "BUY") == "7.45")
check("INTRADAY rounding unchanged", angel_orders._price_str(7.43, "INTRADAY", "BUY") == "7.4")
check("CAS entry ladder on 0.05 grid", cas_executor.price_ladder(dict(ltp=5.23, bid=5.21, ask=5.27), cap=6.8)[0] == 5.25)

print("collapsed-premium exit price")
submitted = []
real_submit = auto_executor.execution_ledger.submit
auto_executor.execution_ledger.submit = lambda pos, side, qty, price: submitted.append(price) or {"ok": True}
real_pending = auto_executor.execution_ledger.pending
auto_executor.execution_ledger.pending = lambda *a, **k: []
for kind in ("cas_blast", "gamma_blast"):
    z = Pos(signal_id=77, kind=kind, index="NIFTY", direction="CE", symbol="Z", token="Z1", exchange="NFO",
            entry_price=4.0, entry_ts=now(), lots=1, lot_size=65, remaining_qty=65, status="open")
    ex._close_position(z, 0.05, "test")
check("Rs 0.05 premium exits at Rs 0.05, not 0.0 (CAS and intraday)", submitted == [0.05, 0.05])
auto_executor.execution_ledger.submit = real_submit
auto_executor.execution_ledger.pending = real_pending

print("session gate: 15:15 INTRADAY window vs CARRYFORWARD")
import importlib
from kadima import market_session_gate as msg
importlib.reload(msg)   # earlier sections replaced entry_block with a fake
saved = (msg.calendar_reason, msg.read_json, msg.permit_reason, msg.BOOT, os.environ.get("KADIMA_PRIORITY_TRADING"))
os.environ["KADIMA_PRIORITY_TRADING"] = "1"
msg.calendar_reason = lambda *a, **k: None
msg.read_json = lambda *a, **k: {"lanes": {"equity": True}}
msg.permit_reason = lambda *a, **k: None
msg.BOOT = SimpleNamespace(read_text=lambda: "boot")
t1520 = datetime(2026, 9, 30, 15, 20, tzinfo=IST)
check("INTRADAY entry blocked after 15:15", msg.entry_block("NFO", t1520) == "outside_broker_intraday_entry_window")
check("CARRYFORWARD (CAS) entry allowed after 15:15", msg.entry_block("NFO", t1520, product="CARRYFORWARD") is None)
msg.read_json = lambda *a, **k: {"lanes": {"equity": False}}
check("CAS still needs the lane permit", msg.entry_block("NFO", t1520, product="CARRYFORWARD") == "lane_permit_not_granted")
msg.calendar_reason, msg.read_json, msg.permit_reason, msg.BOOT = saved[:4]
if saved[4] is None: os.environ.pop("KADIMA_PRIORITY_TRADING", None)
else: os.environ["KADIMA_PRIORITY_TRADING"] = saved[4]

print("owned watchdog leaves CAS to the 15:38 engine close")
from kadima import owned_watchdog
w = Pos(signal_id=88, kind="cas_blast", index="NIFTY", direction="CE", symbol="W", token="W1", exchange="NFO",
        entry_price=4.0, entry_ts=now(), lots=1, lot_size=65, remaining_qty=65, status="open")
led = [dict(signal_id=88, side="BUY", filled=65, dry_run=0, order_id="O1", state="terminal")]
brk = [dict(exchange="NFO", symboltoken="W1", producttype="CARRYFORWARD", netqty="65", tradingsymbol="W")]
due = lambda hh, mm: owned_watchdog.owned_positions({"88": w}, led, brk, [], datetime(2026, 9, 30, hh, mm, tzinfo=IST))
check("15:25: CAS position not due", due(15, 25) == [])
check("15:38: CAS position due", due(15, 38) == [w])

print("product consistency across lanes")
import pathlib
src = {p.name: p.read_text() for p in pathlib.Path("kadima").glob("*.py")}
for lane in ("cas_orders.py", "cas_engine.py"):
    if lane in src:
        check(f"{lane} never sets its own product (ledger decides)",
              "CARRYFORWARD" not in src[lane] and "producttype" not in src[lane])
check("'cas' lane is INTRADAY both ways", angel_orders.product_for(SimpleNamespace(kind="cas")) == "INTRADAY")
allowed = {"angel_orders.py", "cas_config.py", "cas_executor.py", "cas_eod_guard.py", "celestial_adoption.py",
           "market_session_gate.py", "owned_watchdog.py", "test_order_pathway.py", "auto_executor.py"}
stray = sorted(n for n, t in src.items() if "CARRYFORWARD" in t and n not in allowed)
print(f"  [info] other modules mentioning CARRYFORWARD (review, not a failure): {stray or 'none'}")

print("-" * 40)
print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
