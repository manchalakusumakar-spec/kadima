"""Explicit rest-of-day time grid and evaluation definitions (not a predictor)."""
from datetime import timedelta
import math


def target_minutes(as_of, close):
    if as_of.tzinfo is None or close.tzinfo is None or as_of.date() != close.date():
        raise ValueError('invalid_session_timestamps')
    remaining = (close-as_of).total_seconds()/60
    if not 2 <= remaining <= 375:
        raise ValueError('outside_rest_of_day_window')
    # Five-minute checkpoints preserve existing chart granularity. Include exact
    # close even when input timestamps contain seconds; never forecast past it.
    points = list(range(5, math.ceil(remaining), 5))
    return points + [remaining]


def direction(base, price, deadband_bps=5.):
    change = (price/base-1)*10000
    return 'bullish' if change > deadband_bps else 'bearish' if change < -deadband_bps else 'neutral'


def movement_windows(as_of, base, points):
    start, previous = as_of, base
    windows = []
    for p in points:
        end = as_of+timedelta(minutes=p['minute'])
        windows.append(dict(start_at=start.isoformat(), end_at=end.isoformat(),
            expected_direction=direction(previous,p['expected']),
            expected_end=p['expected'], end_low=p['low'], end_high=p['high']))
        start, previous = end, p['expected']
    return windows
