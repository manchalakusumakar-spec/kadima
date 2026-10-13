
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from .config import (IST, MARKET_CLOSE, MARKET_OPEN, PREMARKET_START,
                     SIGNAL_CUTOFF)


def now_ist() -> datetime:
    return datetime.now(IST)


def _t(hm) -> dtime:
    return dtime(hm[0], hm[1])


def is_weekday(dt: datetime | None = None) -> bool:
    return (dt or now_ist()).weekday() < 5


def in_market_hours(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    return is_weekday(dt) and _t(MARKET_OPEN) <= dt.time() <= _t(MARKET_CLOSE)


def in_premarket(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    return is_weekday(dt) and _t(PREMARKET_START) <= dt.time() < _t(MARKET_OPEN)


def past_cutoff(cutoff_hhmm: tuple[int, int], dt: datetime | None = None) -> bool:
    """General form: is dt (default now) at or past the given HH:MM?
    past_signal_cutoff() below is the Bull Shield-specific wrapper kept
    for backward compatibility; callers needing a different cutoff (e.g.
    engine.py's gamma path using config.GAMMA_SIGNAL_CUTOFF) call this
    directly rather than each defining their own copy of the same
    time-comparison logic."""
    return (dt or now_ist()).time() >= _t(cutoff_hhmm)


def past_signal_cutoff(dt: datetime | None = None) -> bool:
    return past_cutoff(SIGNAL_CUTOFF, dt)


def candle_start(dt: datetime, interval_min: int) -> datetime:
    """Floor a timestamp to its candle boundary, anchored to 09:15."""
    anchor = dt.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                        second=0, microsecond=0)
    if dt < anchor:
        return anchor
    elapsed = int((dt - anchor).total_seconds() // 60)
    return anchor + timedelta(minutes=(elapsed // interval_min) * interval_min)


