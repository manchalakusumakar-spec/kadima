"""Replay Gap-and-Go detector from a local JSON fixture. No network or orders.

Input is a JSON array. Each record must contain:
symbol, now, previous_close, session_vwap, prior_vwap, baseline_volume,
bars5, confirmations.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from kadima.gap_and_go import evaluate


def replay(records):
    output = []
    for record in records:
        now = datetime.fromisoformat(record["now"])
        row = evaluate(record["symbol"], record["bars5"],
                       previous_close=record["previous_close"],
                       session_vwap=record["session_vwap"],
                       prior_vwap=record.get("prior_vwap"),
                       baseline_volume=record["baseline_volume"],
                       confirmations=record["confirmations"], now=now)
        output.append(row)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    records = json.loads(args.input.read_text())
    if not isinstance(records, list):
        raise SystemExit("input_must_be_json_array")
    print(json.dumps(replay(records), indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
