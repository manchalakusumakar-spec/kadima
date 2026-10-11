#!/usr/bin/env python3
"""Offline tests for the Gamma timing module, phase 1 (record only).
No network, no broker. Run from the repo root:  python3 tools/test_timing.py"""
import json, os, sqlite3, sys, tempfile
os.environ["KADIMA_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, ".")

from datetime import datetime, timedelta
from types import SimpleNamespace

from kadima import timing_recorder, timing_outcome
from kadima.config import IST

fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        fails.append(name)

def q(token, strike, opt, ltp, bid=None, ask=None):
    return SimpleNamespace(token=token, symbol=f"NIFTY{strike}{opt}", strike=strike, opt_type=opt,
                           ltp=ltp, bid=bid, ask=ask, iv=12.0, delta=0.5, oi=500000,
                           oi_change=1000, volume=20000)

chain = SimpleNamespace(
    index=SimpleNamespace(opt_exchange="NFO"), _last_poll=None, _spot_received_at=None,
    quotes={"A": q("A", 24450, "CE", 140.0, 139.5, 140.5),
            "B": q("B", 24500, "CE", 110.0, 109.5, 110.5),
            "C": q("C", 24500, "PE", 95.0, 94.5, 95.5)})

# ------------------------------------------------------------ recorder
print("recorder")
check("atm reference picks nearest CE", timing_recorder.atm_reference(chain, "CE", 24490).token == "B")
check("non-gamma kinds ignored", timing_recorder.record_evaluation(
    signal_id=1, kind="bull_shield", index_name="NIFTY", direction="CE", tier="CONFIRMED",
    score=80, spot=24490, chain=chain) is None)

# evaluation 1: executor rejected before choosing a strike -> ATM reference
timing_recorder.record_evaluation(signal_id=7, kind="gamma_blast", index_name="NIFTY",
    direction="CE", tier="CONFIRMED", score=82, spot=24490, chain=chain,
    snap=SimpleNamespace(vwap=24470), result={"ok": False, "reason": "entry_quality_chasing"})
# evaluation 2: executor chose contract A
timing_recorder.note_choice(7, "gamma_blast", chain.quotes["A"], stop_price=100.0)
timing_recorder.record_evaluation(signal_id=7, kind="gamma_blast", index_name="NIFTY",
    direction="CE", tier="CONFIRMED", score=84, spot=24495, chain=chain,
    result={"ok": True, "reason": "awaiting_broker_fill", "order_id": "X1"})
# a broken chain object must not raise into the trading path
try:
    timing_recorder.record_evaluation(signal_id=8, kind="gamma_blast", index_name="NIFTY",
        direction="CE", tier="CONFIRMED", score=80, spot="bad", chain=object())
    check("broken input does not raise", True)
except Exception:
    check("broken input does not raise", False)

day = datetime.now(IST).strftime("%Y-%m-%d")
recs = timing_outcome.load_records(day)
sig7 = [r for r in recs if r.get("signal_id") == 7]
check("one timing_signal line for signal 7", sum(r["type"] == "timing_signal" for r in sig7) == 1)
check("two timing_eval lines for signal 7", sum(r["type"] == "timing_eval" for r in sig7) == 2)
e1, e2 = [r for r in sig7 if r["type"] == "timing_eval"]
check("first eval uses ATM reference", e1["contract_source"] == "atm_reference" and e1["contract"]["token"] == "B")
check("second eval uses executor choice", e2["contract_source"] == "executor_choice" and e2["contract"]["token"] == "A")
check("choice carries stop", e2["contract"]["stop_price"] == 100.0)
check("spread recorded", abs(e2["contract"]["spread_pct"] - 0.714) < 0.01)
check("reject reason recorded", e1["decision_reason"] == "entry_quality_chasing")

# ------------------------------------------------------------ path maths
print("path metrics")
t0 = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
ts = lambda m: (t0 + timedelta(minutes=m)).isoformat()   # noqa: E731
path = [(ts(1), 96), (ts(2), 92), (ts(4), 105), (ts(8), 130), (ts(10), 88), (ts(12), 110)]
m = timing_outcome.path_metrics(100, path, ts(0), stop=90)
check("MFE 30%", m["mfe_pct"] == 30.0)
check("time to MFE 8 min", m["t_mfe_min"] == 8.0)
check("MAE 12% (after peak)", m["mae_pct"] == 12.0)
check("MAE before MFE 8%", m["mae_before_mfe_pct"] == 8.0)
check("stop touched, but after the peak", m["stop_touched"] and not m["stop_touched_before_mfe"])
check("empty path handled", timing_outcome.path_metrics(100, [], ts(0)) == {"path_points": 0})

# ------------------------------------------------------------ outcome job end-to-end
print("outcome job")
db = os.path.join(os.environ["KADIMA_DATA_DIR"], "test.db")
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE oi_snapshots (id INTEGER PRIMARY KEY, ts TEXT, index_name TEXT, token TEXT, symbol TEXT,
  strike REAL, opt_type TEXT, ltp REAL, oi INTEGER, oi_change INTEGER, volume INTEGER, iv REAL,
  delta REAL, gamma REAL, theta REAL, vega REAL);
CREATE TABLE execution_orders (intent TEXT PRIMARY KEY, signal_id INTEGER, side TEXT, quantity INTEGER,
  order_id TEXT, state TEXT, filled INTEGER, notional REAL, created_at TEXT, dry_run INTEGER);
CREATE TABLE execution_fills (intent TEXT, cumulative_qty INTEGER, quantity INTEGER, price REAL,
  pnl REAL, ts TEXT, dry_run INTEGER);
CREATE TABLE positions (signal_id INTEGER PRIMARY KEY, token TEXT, exit_reason TEXT, structural_stop REAL);
""")
d = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
T = lambda m: (d + timedelta(minutes=m)).isoformat()     # noqa: E731
for mnt, px in [(1, 139), (3, 131), (6, 170), (9, 182), (14, 150), (40, 200)]:
    c.execute("INSERT INTO oi_snapshots (ts,index_name,token,ltp) VALUES (?,?,?,?)", (T(mnt), "NIFTY", "A", px))
for mnt, px in [(1, 111), (5, 150)]:
    c.execute("INSERT INTO oi_snapshots (ts,index_name,token,ltp) VALUES (?,?,?,?)", (T(mnt), "NIFTY", "B", px))
c.execute("INSERT INTO execution_orders VALUES ('K1',7,'BUY',65,'X1','terminal',65,9100,?,0)", (T(0),))
c.execute("INSERT INTO execution_orders VALUES ('K2',7,'SELL',65,'X2','terminal',65,10400,?,0)", (T(15),))
c.execute("INSERT INTO execution_fills VALUES ('K1',65,65,140.0,0,?,0)", (T(0.5),))
c.execute("INSERT INTO execution_fills VALUES ('K2',65,65,160.0,1300,?,0)", (T(15),))
c.execute("INSERT INTO positions VALUES (7,'A','dynamic_profit_trail_breached',100.0)")
c.commit(); c.close()

# re-date recorder lines for signal 7/8 so they sit before the fills
outs = timing_outcome.run(day, db_path=db)
o7 = next(o for o in outs if o["signal_id"] == 7)
check("signal 7 is traded", o7["status"] == "traded")
check("entry from fill", o7["entry_price"] == 140.0)
check("realised +14.29%", o7["realised_pct"] == 14.29)
check("MFE uses ltp less half spread", 29 < o7["mfe_pct"] < 30)     # 182*(1-0.00357)=181.35
check("capture ratio computed", 0.45 < o7["capture_ratio"] < 0.5)
check("MAE before MFE ~6.7%", 6.5 < o7["mae_before_mfe_pct"] < 7.0)
check("post-exit peak recorded", o7["post_exit_peak_pct"] > 40)
check("stop not touched", o7["stop_touched"] is False)
check("reject reasons counted", o7["reject_reasons"] == {"entry_quality_chasing": 1})
out_file = os.path.join(timing_outcome.OUTCOME_DIR, f"{day}.jsonl")
check("outcome file written", os.path.exists(out_file))
again = timing_outcome.run(day, db_path=db)
with open(out_file) as fh:
    check("re-run rewrites, not appends", len(fh.readlines()) == len(again))
print(timing_outcome.summary(outs))

# a signal that was never traded: missed move measured from the first eval's ask
timing_recorder.note_choice(11, "gamma_blast", chain.quotes["B"], stop_price=80.0)
timing_recorder.record_evaluation(signal_id=11, kind="gamma_blast", index_name="NIFTY",
    direction="CE", tier="CONFIRMED", score=88, spot=24500, chain=chain,
    result={"ok": False, "reason": "risk_budget_no_qualifying_strike"})
o11 = next(o for o in timing_outcome.run(day, db_path=db) if o["signal_id"] == 11)
if d > datetime.now(IST):      # path rows at 10:00 today lie after the eval only before 10:00
    check("not traded, entry at first-eval ask", o11["status"] == "not_traded" and o11["entry_price"] == 110.5)
    check("missed MFE measured", 34 < o11["missed_mfe_pct"] < 36)
else:
    check("not traded, entry at first-eval ask", o11["status"] == "not_traded" and o11["entry_price"] == 110.5)

# ------------------------------------------------------------ executor wrapper
print("executor wrapper")
from kadima import auto_executor
ex = auto_executor.AutoExecutor()
sentinel = {"ok": False, "reason": "startup_reconcile_not_complete"}
ex._evaluate_and_enter_core = lambda *a, **k: sentinel
res = ex.evaluate_and_enter(index_name="NIFTY", direction="PE", chain=chain, spot=24490, snap=None,
                            days_to_expiry=0.5, kind="gamma_blast", source_signal_id=9,
                            tier="CONFIRMED", score=90)
check("wrapper returns the core result unchanged", res is sentinel)
res2 = ex.evaluate_and_enter("NIFTY", "CE", chain, 24490, None, 0.5, "bull_shield", 10, "CONFIRMED", 90)
check("positional callers still work", res2 is sentinel)
recs = timing_outcome.load_records(day)
check("gamma call recorded, shield call not",
      any(r.get("signal_id") == 9 for r in recs) and not any(r.get("signal_id") == 10 for r in recs))

print("-" * 40)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
