"""Offline checks for shadow_compare.py. No network, no broker.

    KADIMA_DATA_DIR=/tmp/kadima-test python tools/test_shadow_compare.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("KADIMA_DATA_DIR", tempfile.mkdtemp(prefix="kadima-test-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kadima import audit, indicators, shadow_compare as sc          # noqa: E402
from kadima.candles import CandleBuilder                             # noqa: E402
from kadima.config import IST, INDICES                               # noqa: E402
from kadima.option_chain import OIVelocityTracker, OptionQuote       # noqa: E402

FAILED = []


def check(name, cond):
    print(("  [ok] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILED.append(name)


WEIGHTS = {"vwap": 25.0, "ema_cross": 20.0, "rsi_50": 20.0,
           "volume_spike": 15.0, "oi_shift": 15.0, "failed_break": 5.0}

# ------------------------------------------------------------ future lookup
print("future selection")
nifty = INDICES["NIFTY"]
rows = [
    dict(exch_seg="NFO", instrumenttype="FUTIDX", name="NIFTY", token="1", symbol="NIFTY27OCT26FUT", expiry="27OCT2026"),
    dict(exch_seg="NFO", instrumenttype="FUTIDX", name="NIFTY", token="2", symbol="NIFTY24NOV26FUT", expiry="24NOV2026"),
    dict(exch_seg="NFO", instrumenttype="FUTIDX", name="BANKNIFTY", token="3", symbol="BANKNIFTY27OCT26FUT", expiry="27OCT2026"),
    dict(exch_seg="NFO", instrumenttype="OPTIDX", name="NIFTY", token="4", symbol="x", expiry="06OCT2026"),
]
scrips = SimpleNamespace(rows=rows)
f = sc.future_for(scrips, nifty, datetime(2026, 10, 7).date())
check("nearest NIFTY future picked", f and f["token"] == "1" and f["exchange"] == "NFO")
f = sc.future_for(scrips, nifty, datetime(2026, 10, 27).date())
check("rolls to next month on the future's own expiry day", f and f["token"] == "2")
check("no future -> None", sc.future_for(scrips, INDICES["SENSEX"], datetime(2026, 10, 7).date()) is None)

# ------------------------------------------------------------ expiry time
print("exact time to expiry")
morning = datetime(2026, 10, 6, 9, 20, tzinfo=IST)
d = sc.exact_days_to_expiry("06OCT2026", morning)
check(f"expiry-day morning is hours, not half a day ({d:.4f} d)", 0.25 < d < 0.27)
late = datetime(2026, 10, 6, 15, 35, tzinfo=IST)
check("floored at 15 minutes near the close",
      abs(sc.exact_days_to_expiry("06OCT2026", late) - 15 / 1440) < 1e-9)
check("day before expiry is about 1.26 d",
      1.2 < sc.exact_days_to_expiry("06OCT2026", morning - timedelta(days=1)) < 1.3)
check("bad or past expiry -> None",
      sc.exact_days_to_expiry("bad", morning) is None
      and sc.exact_days_to_expiry("01OCT2026", morning) is None)

# ------------------------------------------------------------ scoring copy
print("score copy and VWAP fix")
snap = indicators.Snapshot(close=100, vwap=100, ema_fast=105, ema_slow=100, rsi=60, vol=0, avg_vol=0)
direction, score, bull, bear = sc.bull_bear(snap, None, WEIGHTS)
check("live-style snapshot (close==vwap) scores PE-side VWAP", bear == 25 and bull == 40)
fixed = indicators.Snapshot(close=101, vwap=100, ema_fast=105, ema_slow=100, rsi=60, vol=900, avg_vol=400)
direction, score, bull, bear = sc.bull_bear(fixed, None, WEIGHTS)
check(f"real VWAP + volume spike lets an uptrend reach CE 80 ({direction} {score})",
      direction == "CE" and score == 80)

# ------------------------------------------------------------ end to end
print("record() writes one SHADOW_COMPARE event")
fut = dict(token="F1", symbol="NIFTY27OCT26FUT", expiry="2026-10-27", exchange="NFO")
fb = CandleBuilder("F1")
t = datetime.now(IST).replace(hour=9, minute=15, second=2, microsecond=0)
for i in range(240):                    # 2h of rising future prices with volume
    fb.add_tick(t + timedelta(seconds=30 * i), 25000 + i, 1000 * (i + 1))
quotes = {}
for k, (strike, side, ltp) in enumerate([(25100, "CE", 120.0), (25100, "PE", 95.0),
                                          (25050, "CE", 150.0), (25150, "PE", 130.0)]):
    quotes[str(k)] = OptionQuote(str(k), f"N{k}", strike, side, ltp=ltp)
chain = SimpleNamespace(index=nifty, quotes=quotes, velocity=OIVelocityTracker("NIFTY"))
st = SimpleNamespace(name="NIFTY", spot=25110.0, chain=chain, direction="PE", score=45.0,
                     tier="NONE", snap=indicators.Snapshot(close=25110, vwap=25110, ema_fast=25080,
                                                           ema_slow=25000, rsi=62, vol=0, avg_vol=0))
eng = SimpleNamespace(scrips=SimpleNamespace(nearest_expiry=lambda idx: "06OCT2026"),
                      _dte=lambda s: 1.5)
sc.record(eng, st, fut, fb, WEIGHTS, 65.0)
audit.close()
day = datetime.now(IST).strftime("%Y-%m-%d")
from kadima.config import AUDIT_PATH                                 # noqa: E402
events = [json.loads(l) for l in open(os.path.join(AUDIT_PATH, f"{day}.jsonl"))
          if '"SHADOW_COMPARE"' in l]
check("one event written", len(events) == 1)
e = events[-1]
check("future above its own VWAP", e["vwap_fix"]["future_above_vwap"] is True)
check(f"fix turns it CE ({e['vwap_fix']['direction']} {e['vwap_fix']['score']})",
      e["vwap_fix"]["direction"] == "CE" and e["changed"]["direction"])
check("live view recorded as-is", e["live"]["direction"] == "PE" and e["live"]["above_vwap"] is False)
check("ATM greeks under both expiry clocks",
      e["dte_fix"]["atm_old"].get("CE", {}).get("iv") and e["dte_fix"]["atm_new"].get("CE", {}).get("iv"))
check("live quote objects untouched (no Greeks written back)",
      all(q.delta is None and q.iv is None for q in quotes.values()))

sc.record(eng, st, None, None, WEIGHTS, 65.0)
audit.close()
e = [json.loads(l) for l in open(os.path.join(AUDIT_PATH, f"{day}.jsonl"))
     if '"SHADOW_COMPARE"' in l][-1]
check("no future -> VWAP sits out, no rescale",
      e["vwap_fix"]["sit_out"] == "no_future" and e["vwap_fix"]["future_above_vwap"] is None
      and e["vwap_fix"]["score"] <= 40)

os.environ["KADIMA_SHADOW_COMPARE"] = "0"
check("env switch disables it", sc.enabled() is False)

print("-" * 40)
print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
