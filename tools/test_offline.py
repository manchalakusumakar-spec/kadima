#!/usr/bin/env python3
"""Offline sanity tests - no network, no broker. Run after any edit."""
import os, sys, tempfile
os.environ.setdefault("KADIMA_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, ".")

from datetime import datetime, timedelta
from kadima import indicators, greeks
from kadima.candles import CandleBuilder
from kadima.config import IST
from kadima.option_chain import OIVelocityTracker, OptionQuote

fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond: fails.append(name)

print("indicators")
closes = [100+i for i in range(60)]
check("RSI all-up ~100", indicators.rsi(closes) > 99)
check("RSI all-down ~0", indicators.rsi(list(reversed(closes))) < 1)
check("RSI short series None", indicators.rsi([1,2,3]) is None)
e = indicators.ema([10]*50, 20)
check("EMA flat series stable", abs(e[-1]-10) < 1e-9)

print("candles (cumulative volume handling)")
b = CandleBuilder("T")
base = datetime(2026,8,5,9,15,tzinfo=IST)
cum = 1000
for i in range(6):
    b.add_tick(base+timedelta(seconds=i*30), 100+i, cum)
    cum += 500
c = b.live(5)
check("candle open", c.open == 100)
check("candle high", c.high == 105)
check("volume is delta not cumulative", c.volume == 2500)
check("session vwap sane", 100 <= b.session_vwap() <= 106)

print("oi velocity")
t = OIVelocityTracker("NIFTY")
q = [OptionQuote(token="1", symbol="X", strike=24000, opt_type="CE", oi=1_000_000)]
t.add(q)
check("single sample silent", t.alerts() == [])
t.degraded_until = None
for _ in range(25):
    t.add([OptionQuote(token="1", symbol="X", strike=24000, opt_type="CE", oi=1_000_000)])
q2 = [OptionQuote(token="1", symbol="X", strike=24000, opt_type="CE", oi=700_000)]
t.add(q2)
a = t.alerts()
check("30% drop detected", len(a) == 1 and a[0]["drop_pct"] > 15)
t.mark_degraded()
check("degraded window suppresses", t.alerts() == [])

print("greeks")
iv, g = greeks.implied_vol_and_greeks(spot=24000, strike=24000, premium=150, days=3, opt_type="CE")
check("ATM CE delta ~0.5", g and 0.45 < g["delta"] < 0.60)
check("IV plausible", iv and 3 < iv < 80)
iv2, g2 = greeks.implied_vol_and_greeks(spot=24000, strike=24000, premium=150, days=3, opt_type="PE")
check("ATM PE delta ~-0.5", g2 and -0.60 < g2["delta"] < -0.40)
check("theta negative", g["theta"] < 0)

print("-"*40)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
