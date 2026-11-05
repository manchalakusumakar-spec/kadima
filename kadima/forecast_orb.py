"""Opening-range breakout lean for the hourly NIFTY slots (05-Nov-2026, SHADOW ONLY).

Why: since 27-Oct the hourly model has called FLAT on every slot, and across 588 scored slots
none of its parts points the right way more than ~52% of the time. A 513-session replay of the
5-minute candles (Oct-2024 .. Nov-2026) found two simple, stable patterns:

  * Opening-range breakout (12:15 and 13:15 slots): if the last close before the slot is
    above the first hour's high (09:15-10:15), the slot tends to close up; below the first
    hour's low, down.  Inside the range: no lean.
  * Closing fade (15:15 slot): the last 15 minutes tend to give back part of the move of the
    hour before (14:15-15:15).

This module only computes the lean from candles. It never changes the hourly forecast, its band,
the weights, or anything trading reads. The forecaster records the lean in its own table so it
can be scored live before anyone decides to use it.
"""
from dataclasses import dataclass

OR_BARS = 12                          # first hour = 12 five-minute bars, 09:15 .. 10:10
ORB_SLOTS = ('12:15', '13:15')       # 11:15 dropped: 53% hit but -2 pts average in the replay
FADE_SLOT = '15:15'
FADE_BARS = 12                        # the hour before 15:15
FADE_MIN_PTS = 10.0                   # ignore a last hour that barely moved
VERSION = 'orb-1.0'


@dataclass
class Lean:
    slot: str
    lean: str                          # 'UP', 'DOWN' or 'NONE'
    rule: str                          # 'orb', 'fade' or ''
    ref: float                         # last close before the slot
    or_high: float = None
    or_low: float = None
    why: str = ''


def _ok(*xs):
    return all(x is not None and x == x for x in xs)


def lean_for(slot, opens, highs, lows, closes):
    """Lean for `slot` ('HH:MM') from today's bars BEFORE the slot (index 0 = 09:15).

    The lists may be longer than the bars before the slot; only bars before the slot start are
    read, so a caller can never leak the slot's own candles into the lean.
    """
    hh, mm = (int(x) for x in slot.split(':'))
    i = ((hh * 60 + mm) - (9 * 60 + 15)) // 5
    if i <= 0 or i > len(closes):
        return None
    c = closes[:i]
    if not c or not _ok(c[-1]):
        return None
    ref = c[-1]
    if slot in ORB_SLOTS:
        if i < OR_BARS or not all(_ok(h, lo) for h, lo in zip(highs[:OR_BARS], lows[:OR_BARS])):
            return None
        hi, lo = max(highs[:OR_BARS]), min(lows[:OR_BARS])
        if ref > hi:
            return Lean(slot, 'UP', 'orb', ref, hi, lo, f'{ref:.0f} above first-hour high {hi:.0f}')
        if ref < lo:
            return Lean(slot, 'DOWN', 'orb', ref, hi, lo, f'{ref:.0f} below first-hour low {lo:.0f}')
        return Lean(slot, 'NONE', 'orb', ref, hi, lo, 'inside the first-hour range')
    if slot == FADE_SLOT:
        if i < FADE_BARS or not _ok(opens[i - FADE_BARS]):
            return None
        move = ref - opens[i - FADE_BARS]
        if abs(move) < FADE_MIN_PTS:
            return Lean(slot, 'NONE', 'fade', ref, why=f'last hour moved only {move:+.0f}')
        return Lean(slot, 'DOWN' if move > 0 else 'UP', 'fade', ref,
                    why=f'fade the last hour ({move:+.0f} pts)')
    return Lean(slot, 'NONE', '', ref, why='no rule for this slot')


SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_forecast_orb_shadow (
    day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL,
    lean TEXT NOT NULL, rule TEXT NOT NULL, ref REAL NOT NULL,
    or_high REAL, or_low REAL, why TEXT, version TEXT NOT NULL, made_at TEXT NOT NULL,
    PRIMARY KEY (day, index_name, slot) )
"""


def record(conn, day, index_name, lean, now):
    """Store a lean once per slot (INSERT OR IGNORE). Shadow only: nothing reads this table to
    make a forecast, a band or a trade."""
    if lean is None:
        return False
    conn.execute(SCHEMA)
    with conn:
        cur = conn.execute(
            'INSERT OR IGNORE INTO hourly_forecast_orb_shadow VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (day, index_name, lean.slot, lean.lean, lean.rule, round(lean.ref, 2),
             None if lean.or_high is None else round(lean.or_high, 2),
             None if lean.or_low is None else round(lean.or_low, 2), lean.why, VERSION, now))
    return cur.rowcount == 1
