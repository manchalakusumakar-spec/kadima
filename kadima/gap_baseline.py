"""Pure historical helpers for Gap-and-Go paper/replay.

No network, broker, order, environment or production-database access.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import statistics


def _positive(value):
    try:
        value = float(value)
        return math.isfinite(value) and value > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _stamp(row):
    value = row.get("start_ts") if isinstance(row, dict) else None
    if not isinstance(value, str):
        raise ValueError("baseline_timestamp_missing")
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("baseline_timezone_missing")
    return ts


def previous_close(rows, on_date):
    """Last valid closed candle before on_date. Never guesses across bad rows."""
    found = []
    for row in rows:
        ts = _stamp(row)
        close = row.get("close")
        if ts.date() < on_date and _positive(close):
            found.append((ts, float(close)))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[-1][1]


def same_time_volume_baseline(rows, target, *, slots=1, minimum_sessions=3,
                              maximum_sessions=20):
    """Median prior-session volume for the same 5-minute slot(s).

    target is the start of the first slot. slots=2 gives a 10-minute opening
    baseline (09:15 + 09:20). Only dates before target.date() are considered.
    """
    if target.tzinfo is None or type(slots) is not int or not 1 <= slots <= 2:
        raise ValueError("baseline_invalid_request")
    by_day = defaultdict(dict)
    for row in rows:
        ts = _stamp(row)
        if ts.date() >= target.date():
            continue
        volume = row.get("volume")
        if type(volume) is bool:
            continue
        try:
            volume = float(volume)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(volume) or volume < 0:
            continue
        by_day[ts.date()][(ts.hour, ts.minute)] = volume

    wanted = []
    minute = target.hour * 60 + target.minute
    for offset in range(slots):
        slot = minute + 5 * offset
        wanted.append((slot // 60, slot % 60))

    totals = []
    for day in sorted(by_day, reverse=True):
        values = by_day[day]
        if all(key in values for key in wanted):
            totals.append(sum(values[key] for key in wanted))
        if len(totals) >= maximum_sessions:
            break
    if len(totals) < minimum_sessions:
        return None
    return float(statistics.median(totals))


def relative_volume(current_volume, baseline):
    if not _positive(current_volume) or not _positive(baseline):
        return None
    return float(current_volume) / float(baseline)
