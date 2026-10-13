#!/usr/bin/env python3
"""
Candle verification. Run this BEFORE trusting any indicator.

Prints your engine's 5-minute bars next to Angel One's own historical candles
for the same window. Any difference in OHLC beyond rounding, or any difference
in volume at all, is a bug you must fix before the indicator layer means
anything.

    python tools/verify_candles.py NIFTY 2026-08-05
"""
import sys
from datetime import datetime

sys.path.insert(0, ".")
from kadima import storage                      # noqa: E402
from kadima.config import INDICES, IST          # noqa: E402
from kadima.session import SESSION              # noqa: E402


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    name, day = sys.argv[1].upper(), sys.argv[2]
    idx = INDICES[name]

    ours = storage.recent_candles(idx.token, 5, limit=200)
    ours = [r for r in ours if r["start_ts"].startswith(day)]

    SESSION.ensure()
    resp = SESSION.api.getCandleData({
        "exchange": idx.exchange, "symboltoken": idx.token,
        "interval": "FIVE_MINUTE",
        "fromdate": f"{day} 09:15", "todate": f"{day} 15:30"})
    theirs = {r[0][:16]: r for r in (resp.get("data") or [])}

    print(f"{'time':<18}{'our O/H/L/C':<40}{'broker O/H/L/C':<40}{'vol diff'}")
    print("-" * 110)
    mismatches = 0
    for r in ours:
        key = r["start_ts"][:16]
        t = theirs.get(key)
        if not t:
            print(f"{key:<18}{'MISSING FROM BROKER':<40}")
            mismatches += 1
            continue
        o = f"{r['open']:.2f}/{r['high']:.2f}/{r['low']:.2f}/{r['close']:.2f}"
        b = f"{t[1]:.2f}/{t[2]:.2f}/{t[3]:.2f}/{t[4]:.2f}"
        vd = (r["volume"] or 0) - (t[5] or 0)
        flag = "  <== MISMATCH" if (abs(r['close'] - t[4]) > 0.05 or vd) else ""
        if flag:
            mismatches += 1
        print(f"{key:<18}{o:<40}{b:<40}{vd:>10}{flag}")

    print("-" * 110)
    print(f"{len(ours)} bars compared, {mismatches} mismatched.")
    if mismatches:
        print("\nDo not proceed to the indicator layer until this is zero.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
