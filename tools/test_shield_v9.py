"""Offline checks for SHIELD v9 (Master Checklist v9.0) and its engine wiring.

No broker, no network. Run:  python tools/test_shield_v9.py
"""
import os
import socket
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

os.environ["KADIMA_DATA_DIR"] = tempfile.mkdtemp(prefix="kadima-v9-")
os.environ["HOME"] = os.environ["KADIMA_DATA_DIR"]
os.environ.pop("KADIMA_SHIELD_V9_LIVE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _no_network(*a, **k):
    raise RuntimeError("network blocked in offline test")


socket.socket.connect = _no_network

from kadima import shield_v9 as v9  # noqa: E402
from kadima.config import IST  # noqa: E402

FAILED = []
T0 = datetime(2026, 10, 13, 10, 0, tzinfo=IST)


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


BULL = NS(ema_bull=True, rsi=60.0, failed_breakdown=False, failed_breakout=False)
BEAR = NS(ema_bull=False, rsi=40.0, failed_breakdown=False, failed_breakout=False)


def test_pick_future():
    print("near-month index future, rolled on expiry day")
    rows = [dict(exch_seg="NFO", instrumenttype="FUTIDX", name="NIFTY", expiry="27OCT2026", token="1", symbol="NIFTY27OCT26FUT"),
            dict(exch_seg="NFO", instrumenttype="FUTIDX", name="NIFTY", expiry="24NOV2026", token="2", symbol="NIFTY24NOV26FUT"),
            dict(exch_seg="NFO", instrumenttype="OPTIDX", name="NIFTY", expiry="14OCT2026", token="9"),
            dict(exch_seg="BFO", instrumenttype="FUTIDX", name="SENSEX", expiry="29OCT2026", token="5", symbol="SENSEXFUT"),
            dict(exch_seg="NFO", instrumenttype="FUTIDX", name="BANKNIFTY", expiry="27OCT2026", token="7", permitted_to_trade=False)]
    check("NIFTY near month", v9.pick_future(rows, "NIFTY", "NSE", date(2026, 10, 13))["token"] == "1")
    check("rolls on expiry day", v9.pick_future(rows, "NIFTY", "NSE", date(2026, 10, 27))["token"] == "2")
    check("options ignored", v9.pick_future([rows[2]], "NIFTY", "NSE", date(2026, 10, 13)) is None)
    check("SENSEX on BFO", v9.pick_future(rows, "SENSEX", "BSE", date(2026, 10, 13))["exchange"] == "BFO")
    check("not permitted -> none", v9.pick_future(rows, "BANKNIFTY", "NSE", date(2026, 10, 13)) is None)


def test_futures_bars():
    print("futures bars: same-period replace, previous 5 volumes")
    b = v9.FuturesBars(dict(symbol="NIFTYFUT"))
    for i in range(6):
        b.add(T0 + timedelta(minutes=5 * i), 25000 + i, 100 + i, 24990.0)
    b.add(T0 + timedelta(minutes=25), 25010, 900, 24995.0)          # closed version replaces open reading
    got = b.at(T0 + timedelta(minutes=25))
    check("replaced, not duplicated", len(b.bars) == 6 and got["vol"] == 900)
    check("previous 5 volumes", got["prev_vols"] == [100, 101, 102, 103, 104])
    check("missing period -> None", b.at(T0 + timedelta(minutes=60)) is None)


def test_score():
    print("score legs (v9.0)")
    check("EMA + RSI only = 40", v9.score(BULL)[:2] == ("CE", 40.0))
    fut = dict(vwap=25000.0, close=25040.0, vol=900, prev_vols=[300, 400, 500, 350, 450])
    check("+ futures VWAP reclaim + volume = 80", v9.score(BULL, futures=fut)[:2] == ("CE", 80.0))
    check("+ OI = 95", v9.score(BULL, "CE", fut)[:2] == ("CE", 95.0))
    check("+ failed breakdown = 100", v9.score(NS(**{**vars(BULL), "failed_breakdown": True}), "CE", fut)[:2] == ("CE", 100.0))
    check("volume not above all 5 -> no volume leg", "volume" not in v9.score(BULL, futures=dict(fut, vol=480))[2])
    check("volume needs a VWAP side", "volume" not in v9.score(BULL, futures=dict(fut, close=25000.0))[2])
    check("RSI 50 scores neither", "rsi" not in v9.score(NS(**{**vars(BULL), "rsi": 50.0}))[2])
    check("bear mirror", v9.score(BEAR, "PE", dict(fut, close=24950.0))[:2] == ("PE", 95.0))
    check("no futures: EMA + RSI + OI = 55 (VWAP and volume sit out)", v9.score(BULL, "CE")[1] == 55.0)


def q(token, typ, strike, oi):
    return NS(token=token, opt_type=typ, strike=strike, oi=oi)


def test_oi():
    print("OI: writing / covering / unwinding, 10-minute window, ambiguity")
    h = v9.OIHistory()
    h.add(T0, [q("P", "PE", 25000.0, 1000), q("C", "CE", 25050.0, 1000)])
    check("warmup before 10 minutes", h.side("NIFTY", 25020, T0)[1] == "oi_warmup")
    h.add(T0 + timedelta(minutes=5), [q("P", "PE", 25000.0, 1100), q("C", "CE", 25050.0, 1000)])
    h.add(T0 + timedelta(minutes=10), [q("P", "PE", 25000.0, 1200), q("C", "CE", 25050.0, 1000)])
    check("put writing +20% -> CE", h.side("NIFTY", 25020, T0 + timedelta(minutes=10))[0] == "CE")
    h2 = v9.OIHistory()
    h2.add(T0, [q("C", "CE", 25050.0, 1000)])
    h2.add(T0 + timedelta(minutes=10), [q("C", "CE", 25050.0, 870)])
    check("CE -13%: NIFTY no", h2.side("NIFTY", 25020, T0 + timedelta(minutes=10))[0] is None)
    check("CE -13%: BANKNIFTY covering -> CE", h2.side("BANKNIFTY", 25020, T0 + timedelta(minutes=10))[0] == "CE")
    h3 = v9.OIHistory()
    late = datetime(2026, 10, 13, 9, 20, tzinfo=IST)
    h3.add(late, [q("P", "PE", 25000.0, 1000)])
    h3.add(late.replace(hour=13), [q("P", "PE", 25000.0, 6500)])
    h3.add(late.replace(hour=13, minute=30), [q("P", "PE", 25000.0, 7000)])
    check("PUT OI +600% after 12:30 -> ambiguous", h3.side("NIFTY", 25020, late.replace(hour=13, minute=30)) == (None, "put_oi_ambiguous"))


def hist(scores, direction="CE", start=T0):
    return [dict(closed_at=start + timedelta(minutes=5 * i), direction=direction, score=s) for i, s in enumerate(scores)]


def test_tiers():
    print("tiers and mandatory rules")
    check("75 once -> WATCH", v9.tier("NIFTY", hist([75]))[0] == "WATCH")
    check("75 held 2 bars -> ACTIVE", v9.tier("NIFTY", hist([75, 75]))[0] == "ACTIVE")
    check("90 held -> CONFIRMED", v9.tier("NIFTY", hist([92, 95]))[0] == "CONFIRMED")
    check("39 -> NONE", v9.tier("NIFTY", hist([39]))[0] == "NONE")
    check("BANKNIFTY 42 -> NONE (watch 45)", v9.tier("BANKNIFTY", hist([42]))[0] == "NONE")
    check("BANKNIFTY 2 bars not enough", v9.tier("BANKNIFTY", hist([75, 75]), banks={"A": True, "B": True})[0] == "WATCH")
    check("BANKNIFTY 3 bars + 2 banks -> ACTIVE", v9.tier("BANKNIFTY", hist([75, 75, 75]), banks={"A": True, "B": True})[0] == "ACTIVE")
    check("BANKNIFTY 1 bank -> WATCH", v9.tier("BANKNIFTY", hist([75, 75, 75]), banks={"A": True, "B": False}) == ("WATCH", "bank_confirmation_missing"))
    check("macro window -> WATCH", v9.tier("NIFTY", hist([95, 95]), macro_clear=False) == ("WATCH", "macro_suppression_or_unknown"))
    gap = [dict(closed_at=T0, direction="CE", score=80), dict(closed_at=T0 + timedelta(minutes=10), direction="CE", score=80)]
    check("non-consecutive bars -> not held", v9.tier("NIFTY", gap)[0] == "WATCH")
    check("direction change -> not held", v9.tier("NIFTY", hist([80]) + hist([80], "PE", T0 + timedelta(minutes=5)))[0] == "WATCH")
    pm = datetime(2026, 10, 13, 13, 5, tzinfo=IST)
    check("pre-expiry -20% after 13:00", v9.capped(90, pm, True) == 72.0 and v9.capped(90, pm, False) == 90)
    check("no cap before 13:00", v9.capped(90, T0, True) == 90)


def test_alert_and_filters():
    print("alert format and shadow filters")
    check("bull alert", v9.alert_line("NIFTY", "ACTIVE", "CE", 25100, 25050) ==
          "\U0001F7E2 BULL SHIELD ACTIVE | Entry: 25100 | Target: 25200-25250 | SL: 25050")
    check("bear alert", v9.alert_line("NIFTY", "CONFIRMED", "PE", 25000, 25040).startswith("\U0001F534 BEAR SHIELD CONFIRMED"))
    check("watch -> NO A-GRADE", v9.alert_line("NIFTY", "WATCH", "CE", 25100, 25050) == "⚪ NO A-GRADE SETUP | Wait")
    f = v9.candidate_filters("CE", dict(vwap="CE", ema="CE", rsi="CE"), datetime(2026, 10, 13, 12, 0, tzinfo=IST), 80, 200, 0.14)
    check("trend-only legs flagged", f["non_trend_leg"] is False)
    check("range day flagged (80 < 0.6 x 200)", f["trend_day"] is False)
    check("midday flagged", f["midday"] is True)
    bars = [dict(start_ts=f"2026-10-1{d}T10:00:00+05:30", high=100 + d * 10, low=90) for d in (0, 1, 2)]
    check("day ranges", v9.day_ranges(bars, date(2026, 10, 12)) == (30.0, 15.0))


def test_live_switch_default_off():
    print("live switch")
    check("off by default", v9.live_enabled() is False)
    os.environ["KADIMA_SHIELD_V9_LIVE"] = "1"
    check("on only when set to 1", v9.live_enabled() is True)
    os.environ.pop("KADIMA_SHIELD_V9_LIVE")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("-" * 40)
    print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1 if FAILED else 0)
