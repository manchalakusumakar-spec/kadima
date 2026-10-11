"""Offline checks for intraday trend v2 (Master Checklist v8.0 rules).

No broker, no network. Synthetic 5-minute bars only.
Run:  python tools/test_intraday_trend_v2.py
"""
import os
import socket
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

os.environ["KADIMA_DATA_DIR"] = tempfile.mkdtemp(prefix="kadima-itv2-")
os.environ["HOME"] = os.environ["KADIMA_DATA_DIR"]
os.environ.pop("KADIMA_INTRADAY_TREND_LIVE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _no_network(*a, **k):
    raise RuntimeError("network blocked in offline test")


socket.socket.connect = _no_network

from kadima import intraday_trend as it  # noqa: E402
from kadima.config import IST  # noqa: E402

FAILED = []
DAY = datetime(2026, 10, 13, 9, 15, tzinfo=IST)


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def bars(closes, start=DAY):
    out, prev = [], closes[0] - 5
    for i, c in enumerate(closes):
        o = prev
        out.append(dict(start_ts=(start + timedelta(minutes=5 * i)).isoformat(), open=o,
                        high=max(o, c) + 4, low=min(o, c) - 4, close=c, closed=1))
        prev = c
    return out


UP = [25000 + 8 * i for i in range(14)]
PULL = [UP[-1] - 6, UP[-1] - 14, UP[-1] - 20]
REC = [UP[-1] - 2]
CONF = [UP[-1] + 2]


def scenario(confirm=CONF):
    rows = bars(UP + PULL + REC + confirm)
    now = DAY + timedelta(minutes=5 * len(rows))
    return rows, now


SNAP_BULL = NS(ema_fast=25100.0, ema_slow=25050.0, rsi=60.0, ema_bull=True, above_vwap=True,
               vol=0, avg_vol=None, failed_breakdown=False, failed_breakout=False)
SNAP_BEAR = NS(ema_fast=25000.0, ema_slow=25050.0, rsi=40.0, ema_bull=False, above_vwap=False,
               vol=0, avg_vol=None, failed_breakdown=False, failed_breakout=False)


def shield(now, n=2, score=75.0, direction="CE"):
    return [dict(closed_at=now - timedelta(minutes=5 * i), direction=direction, score=score)
            for i in range(n)][::-1]


def run(rows, now, spot=None, snap=SNAP_BULL, sh="default", banks=None, dte=None, underlying="NIFTY",
        macro=False):
    spot = spot if spot is not None else rows[-1]["close"]
    if sh == "default":
        sh = shield(now)
    return it.detect(rows, spot, snap, now - timedelta(seconds=30), now, shield=sh,
                     banks=banks, days_to_expiry=dte, underlying=underlying, macro_active=macro)


def test_policy():
    print("policy: v2 identity and families")
    check("policy id is v2", it.POLICY["id"] == "intraday_trend_v2" and it.POLICY_ID.startswith("intraday_trend_v2:"))
    check("opening_continuation removed", [f for f, _ in it.FAMILIES] == ["afternoon_breakout", "pullback_recovery"])
    check("checklist thresholds", it.POLICY["shield_min_score"] == 70.0 and it.POLICY["shield_hold_bars"] == 2
          and it.POLICY["shield_hold_bars_banknifty"] == 3 and it.POLICY["target_r"] == 2.0)


def test_confirmed_setup():
    print("setup confirmed by the next bar with Shield held: accepted")
    rows, now = scenario()
    setup, ev = run(rows, now)
    check("pullback setup found", setup is not None and setup["family"] == "pullback_recovery")
    check("direction CE", setup and setup["direction"] == "CE")
    check("episode bar is the confirmation bar",
          setup and setup["bar_start"] == rows[-1]["start_ts"] and setup["setup_bar_start"] == rows[-2]["start_ts"])
    buffer = max(5.0, 0.25 * setup["avg_range"]) if setup else 0
    check("stop beyond structure by the buffer",
          setup and abs((setup["structure_level"] - setup["spot_stop"]) - buffer) < 0.02)


def test_not_confirmed():
    print("next bar does not confirm: rejected")
    rows, now = scenario(confirm=[UP[-1] - 4])
    setup, ev = run(rows, now)
    check("no setup", setup is None)
    check("reason not_confirmed_by_next_bar", ev.get("pullback_recovery") == "not_confirmed_by_next_bar")


def test_shield_gates():
    print("Bull Shield gate (checklist v8.0)")
    rows, now = scenario()
    check("no Shield history -> shield_unavailable", run(rows, now, sh=None)[1]["pullback_recovery"] == "shield_unavailable")
    check("only one bar >=70 -> shield_not_held", run(rows, now, sh=shield(now, n=1))[1]["pullback_recovery"] == "shield_not_held")
    check("score 65 -> shield_below_active", run(rows, now, sh=shield(now, score=65.0))[1]["pullback_recovery"] == "shield_below_active")
    check("Shield points PE -> shield_direction_against",
          run(rows, now, sh=shield(now, direction="PE"))[1]["pullback_recovery"] == "shield_direction_against")
    gap = [dict(closed_at=now - timedelta(minutes=10), direction="CE", score=80.0),
           dict(closed_at=now, direction="CE", score=80.0)]
    check("non-consecutive bars -> shield_not_held", run(rows, now, sh=gap)[1]["pullback_recovery"] == "shield_not_held")
    stale = shield(now - timedelta(minutes=5))
    check("history not ending at this bar -> shield_not_held", run(rows, now, sh=stale)[1]["pullback_recovery"] == "shield_not_held")


def test_pre_expiry_cap():
    print("pre-expiry -20% cap after 13:00")
    afternoon = DAY.replace(hour=11, minute=30)
    rows = bars(UP + PULL + REC + CONF, start=afternoon)
    now = afternoon + timedelta(minutes=5 * len(rows))
    check("decision is after 13:00", now.time() >= datetime(2026, 1, 1, 13, 0).time())
    check("score 80 passes on a normal day", run(rows, now, sh=shield(now, score=80.0), dte=3)[0] is not None)
    check("score 80 x 0.8 = 64 fails pre-expiry",
          run(rows, now, sh=shield(now, score=80.0), dte=1)[1]["pullback_recovery"] == "shield_below_active")
    check("score 90 x 0.8 = 72 passes pre-expiry", run(rows, now, sh=shield(now, score=90.0), dte=1)[0] is not None)


def test_banknifty_variant():
    print("Bank Nifty variant: one more bar and >=2 of 4 banks")
    rows, now = scenario()
    ok_banks = dict(HDFCBANK="CE", ICICIBANK="CE", KOTAKBANK="PE", SBIN=None)
    check("2 bars not enough", run(rows, now, underlying="BANKNIFTY", banks=ok_banks)[1]["pullback_recovery"] == "shield_not_held")
    three = shield(now, n=3)
    check("bank data missing -> bank_confirmation_unavailable",
          run(rows, now, underlying="BANKNIFTY", sh=three)[1]["pullback_recovery"] == "bank_confirmation_unavailable")
    check("only 1 bank aligned -> bank_confirmation_missing",
          run(rows, now, underlying="BANKNIFTY", sh=three, banks=dict(HDFCBANK="CE", ICICIBANK="PE"))[1]["pullback_recovery"]
          == "bank_confirmation_missing")
    check("3 bars + 2 banks -> setup", run(rows, now, underlying="BANKNIFTY", sh=three, banks=ok_banks)[0] is not None)


def test_trend_alignment_still_required():
    print("EMA/RSI alignment still required")
    rows, now = scenario()
    check("bearish snapshot rejects a CE setup", run(rows, now, snap=SNAP_BEAR)[0] is None)


def test_shield_score():
    print("Shield score: checklist v9.0 legs")
    d, sc, legs = it.shield_score(SNAP_BULL)
    check("EMA + RSI>55 only = 40, VWAP/volume sit out without futures", (d, sc) == ("CE", 40.0)
          and "vwap" not in legs and "volume" not in legs)
    fut = dict(vwap=25000.0, close=25050.0, vol=900, prev_vols=[300, 400, 500, 350, 450])
    check("futures VWAP reclaim + volume above previous 5 = +40",
          it.shield_score(SNAP_BULL, futures=fut)[:2] == ("CE", 80.0))
    check("plus OI shift CE = 95", it.shield_score(SNAP_BULL, oi_side="CE", futures=fut)[:2] == ("CE", 95.0))
    weak = dict(fut, vol=450)
    check("volume not above every one of the previous 5 -> no volume leg",
          it.shield_score(SNAP_BULL, futures=weak)[:2] == ("CE", 65.0))
    mid = NS(**{**vars(SNAP_BULL), "rsi": 52.0})
    check("RSI 52 (between 45 and 55) scores neither side", it.shield_score(mid)[2].get("rsi") is None)
    check("bear snapshot + PE OI = PE 55", it.shield_score(SNAP_BEAR, oi_side="PE")[:2] == ("PE", 55.0))
    check("index's own VWAP never counts", "vwap" not in it.shield_score(
        NS(**{**vars(SNAP_BULL), "vwap": 1.0, "close": 2.0}))[2])


def test_oi_side():
    print("ATM OI direction (writing / unwinding) and PUT OI ambiguity")
    now = DAY.replace(hour=11)
    hist = lambda oi: [(now - timedelta(minutes=11), oi)]
    rows = [dict(token="P1", strike=25000.0, opt_type="PE", oi=1200), dict(token="C1", strike=25050.0, opt_type="CE", oi=1000)]
    check("put writing (+20% PE OI) -> CE", it.oi_side(rows, {"P1": hist(1000), "C1": hist(1000)}, 25020, "NIFTY", now) == "CE")
    rows2 = [dict(token="C1", strike=25050.0, opt_type="CE", oi=1200)]
    check("call writing (+20% CE OI) -> PE", it.oi_side(rows2, {"C1": hist(1000)}, 25020, "NIFTY", now) == "PE")
    rows3 = [dict(token="C1", strike=25050.0, opt_type="CE", oi=870)]
    check("CE wall -13%: below 15% for NIFTY", it.oi_side(rows3, {"C1": hist(1000)}, 25020, "NIFTY", now) is None)
    check("CE wall -13%: counts for BANKNIFTY (12%) -> CE", it.oi_side(rows3, {"C1": hist(1000)}, 25020, "BANKNIFTY", now) == "CE")
    late = DAY.replace(hour=13)
    rows4 = [dict(token="P1", strike=25000.0, opt_type="PE", oi=7000)]
    check("PUT OI +600% on the day after 12:30 -> ambiguous, sits out",
          it.oi_side(rows4, {"P1": [(late - timedelta(minutes=11), 1000)]}, 25020, "NIFTY", late, day_open={"P1": 1000}) is None)
    far = [dict(token="P9", strike=26000.0, opt_type="PE", oi=2000)]
    check("strikes beyond 1.5% of spot ignored", it.oi_side(far, {"P9": hist(1000)}, 25020, "NIFTY", now) is None)


def test_structure_and_macro():
    print("market structure and macro suppression")
    rows, now = scenario()
    setup, _ = run(rows, now)
    check("uptrend has HH+HL -> setup", setup is not None)
    check("opening-range side recorded as evidence", setup and setup.get("opening_range") in ("CE", "PE", "inside", None))
    flat = bars([25100] * 6 + [25150, 25140, 25130])
    check("structure_ok rejects too few bars", it.structure_ok(it.closed_bars(flat, DAY + timedelta(minutes=45)), "CE") is False)
    try:
        run(rows, now, macro=True); blocked = False
    except ValueError as exc:
        blocked = str(exc) == "macro_suppression_window_or_unknown"
    check("macro window active -> no entry", blocked)
    try:
        run(rows, now, macro=None); unknown = False
    except ValueError as exc:
        unknown = str(exc) == "macro_suppression_window_or_unknown"
    check("macro status unknown -> no entry", unknown)


def pos(entry=100.0, stop=90.0, minutes=10, peak=None, direction="CE"):
    now = DAY.replace(hour=11)
    return NS(kind=it.STRATEGY, entry_ts=now - timedelta(minutes=minutes), entry_price=entry,
              structural_stop=stop, peak_price=peak or entry, direction=direction), now


def test_exits():
    print("v2 exits")
    p, now = pos()
    check("target at 2R (100 + 2x10 = 120)", it.exit_reason(p, now, ltp=120.0) == "intraday_trend_target_2r")
    check("no target below 2R", it.exit_reason(p, now, ltp=119.0) is None)
    check("EMA flip within 2 candles -> invalidated", it.exit_reason(p, now, snap=SNAP_BEAR) == "intraday_trend_shield_invalidated")
    p2, now2 = pos(minutes=15)
    check("EMA flip after 2 candles -> not invalidated", it.exit_reason(p2, now2, snap=SNAP_BEAR) is None)
    p3, now3 = pos(minutes=46, peak=103.0)
    check("no progress at 45 min still applies", it.exit_reason(p3, now3) == "intraday_trend_no_progress_45min")
    p4, _ = pos()
    late = DAY.replace(hour=15, minute=10)
    check("15:10 time exit first", it.exit_reason(p4, late, ltp=200.0) == "intraday_trend_time_exit")
    p5, now5 = pos()
    check("fresh CE wall -> CE trade exits", it.exit_reason(p5, now5, ce_wall=True) == "intraday_trend_fresh_ce_wall")
    p6, now6 = pos(direction="PE")
    check("CE wall rule not applied to PE trades", it.exit_reason(p6, now6, ce_wall=True) is None)
    wall_now = [dict(token="C1", strike=25050.0, opt_type="CE", oi=2600)]
    check("CE OI +160% within 50 pts = fresh wall", it.fresh_ce_wall(25020, wall_now, {"C1": 1000}) is True)
    check("CE OI +140% is not", it.fresh_ce_wall(25020, [dict(wall_now[0], oi=2400)], {"C1": 1000}) is False)
    check("wall 80 pts away is not", it.fresh_ce_wall(24970, wall_now, {"C1": 1000}) is False)
    other = NS(kind="gamma_blast", entry_ts=None, entry_price=None)
    check("other engines untouched", it.exit_reason(other, now, ltp=500.0, snap=SNAP_BEAR) is None)


def test_live_hook_fails_closed():
    print("live hook without Shield history fails closed")
    rows, now = scenario()
    st = NS(name="NIFTY", spot=rows[-1]["close"], snap=SNAP_BULL, technical_at=now - timedelta(seconds=30),
            chain=NS(index=NS(token="99926000")))
    it.storage.recent_candles = lambda token, interval, n: rows
    lane = it.Lane()
    st.trend_macro_active = False
    row = lane.observe_close(st, now=now)
    check("no setup without Shield history", row["stage"] == "NO_SETUP"
          and row["evaluations"].get("pullback_recovery") == "shield_unavailable")
    del st.trend_macro_active
    row = lane.observe_close(st, now=now)
    check("unknown macro status blocks in the live hook",
          "macro_suppression_window_or_unknown" in row["blocked_reasons"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("-" * 40)
    print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1 if FAILED else 0)
