"""Summarise one day's SHADOW_COMPARE events (VWAP and expiry-time fixes).

    python tools/shadow_report.py               # today
    python tools/shadow_report.py 2026-10-07    # a given day

Reads DATA_DIR/audit/<day>.jsonl. Read only.
"""
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kadima.config import AUDIT_PATH, IST  # noqa: E402


def main(day):
    path = os.path.join(AUDIT_PATH, f"{day}.jsonl")
    if not os.path.exists(path):
        print(f"No audit file for {day}: {path}")
        return 1
    rows = defaultdict(list)
    errors = Counter()
    with open(path) as fh:
        for line in fh:
            if '"SHADOW_COMPARE"' not in line and '"shadow_' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "SHADOW_COMPARE":
                rows[r["index"]].append(r)
            elif r.get("event", "").startswith("shadow_"):
                errors[r["event"]] += 1
    if not rows:
        print(f"No SHADOW_COMPARE events on {day}.")
        if errors:
            print("Shadow events seen:", dict(errors))
        return 1

    print(f"Shadow comparison for {day}\n")
    for index, items in sorted(rows.items()):
        n = len(items)
        live_dir = Counter(r["live"]["direction"] for r in items)
        new_dir = Counter(r["vwap_fix"]["direction"] for r in items)
        live_above = sum(1 for r in items if r["live"]["above_vwap"])
        fut_above = sum(1 for r in items if r["vwap_fix"]["future_above_vwap"])
        live_65 = sum(1 for r in items if (r["live"]["score"] or 0) >= 65)
        new_65 = sum(1 for r in items if r["vwap_fix"]["meets_score"])
        new_65_ce = sum(1 for r in items if r["vwap_fix"]["meets_score"]
                        and r["vwap_fix"]["direction"] == "CE")
        sit = Counter(r["vwap_fix"]["sit_out"] for r in items if r["vwap_fix"]["sit_out"])
        flips = sum(1 for r in items if r["changed"]["direction"])
        crosses = sum(1 for r in items if r["changed"]["crosses_threshold"])
        mismatch = sum(1 for r in items
                       if r["live"]["recomputed_score"] != r["live"]["score"])

        print(f"== {index}  ({n} five-minute bars)")
        print(f"  above VWAP     live {live_above}/{n}   future-based {fut_above}/{n}")
        print(f"  direction      live {dict(live_dir)}   with fix {dict(new_dir)}")
        print(f"  score >= 65    live {live_65}   with fix {new_65} (of which CE {new_65_ce})")
        print(f"  direction flips {flips}   threshold crossings {crosses}")
        if sit:
            print(f"  VWAP sat out   {dict(sit)}")
        if mismatch:
            print(f"  WARNING: shadow copy of the score disagrees with live on {mismatch} bars"
                  " -- engine._score() may have changed; update shadow_compare.bull_bear()")

        for side in ("CE", "PE"):
            div, ddiff = [], []
            for r in items:
                old = r["dte_fix"]["atm_old"].get(side) or {}
                new = r["dte_fix"]["atm_new"].get(side) or {}
                if old.get("iv") and new.get("iv"):
                    div.append(new["iv"] - old["iv"])
                if old.get("delta") is not None and new.get("delta") is not None:
                    ddiff.append(abs(new["delta"]) - abs(old["delta"]))
            if div:
                print(f"  ATM {side} IV     new-old median {statistics.median(div):+.2f} pts"
                      f"  (range {min(div):+.2f} .. {max(div):+.2f})")
            if ddiff:
                print(f"  ATM {side} |delta| new-old median {statistics.median(ddiff):+.3f}")
        days = [(r["dte_fix"]["old_days"], r["dte_fix"]["new_days"]) for r in items]
        print(f"  days to expiry first bar old {days[0][0]} new {days[0][1]}; "
              f"last bar old {days[-1][0]} new {days[-1][1]}\n")
    if errors:
        print("Shadow housekeeping events:", dict(errors))
    return 0


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(IST).strftime("%Y-%m-%d")
    sys.exit(main(day))
