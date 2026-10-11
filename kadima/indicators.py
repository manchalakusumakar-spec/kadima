
"""
Indicator engine. Pure functions over closed candles - no I/O, no state
mutation, so it is trivially unit-testable against known series.

RSI uses Wilder's smoothing (the standard). If your broker chart shows a
different RSI, it is almost always because the chart uses simple averaging or
a different period - resolve that discrepancy before trusting the
"RSI cross 50" trigger, which carries 20% of your Bull Shield score.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Snapshot:
    close: float | None = None
    vwap: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    ma_long: float | None = None
    rsi: float | None = None
    tenkan: float | None = None
    kijun: float | None = None
    span_a: float | None = None
    span_b: float | None = None
    vol: int = 0
    avg_vol: float | None = None
    failed_breakout: bool | None = None   # resistance pierced then closed back below it -> bearish reversal
    failed_breakdown: bool | None = None  # support pierced then closed back above it -> bullish reversal

    @property
    def above_vwap(self) -> bool | None:
        if self.close is None or self.vwap is None:
            return None
        return self.close > self.vwap

    @property
    def ema_bull(self) -> bool | None:
        if self.ema_fast is None or self.ema_slow is None:
            return None
        return self.ema_fast > self.ema_slow


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[:-1], closes[1:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ichimoku(highs: list[float], lows: list[float]):
    """Returns (tenkan, kijun, span_a, span_b). Conversion 9 / base 26 / 52."""
    def mid(h, l, n):
        if len(h) < n:
            return None
        return (max(h[-n:]) + min(l[-n:])) / 2

    tenkan = mid(highs, lows, 9)
    kijun = mid(highs, lows, 26)
    span_b = mid(highs, lows, 52)
    span_a = ((tenkan + kijun) / 2) if (tenkan and kijun) else None
    return tenkan, kijun, span_a, span_b


FAILED_BREAK_LOOKBACK = 20  # candles used to establish the reference swing high/low


def failed_break(highs: list[float], lows: list[float], closes: list[float],
                 lookback: int = FAILED_BREAK_LOOKBACK) -> tuple[bool | None, bool | None]:
    """
    Failed breakout/breakdown — the sixth Master Checklist v2.1 trigger
    (5% weight). A failed break is a price-action reversal signal: the
    market attempted to break a recent extreme, could not sustain it, and
    closed back inside the prior range in the SAME candle. That failure
    to hold is the signal — a genuine breakout that holds is not a
    failed-break event, it's just a breakout (and gets picked up by the
    other Bull Shield factors instead: VWAP, EMA, volume).

    Reference levels are the swing high/low over the `lookback` candles
    BEFORE the current one — the current candle is never compared against
    a level that includes itself, or every close would trivially be
    inside its own range.

    Returns (failed_breakout, failed_breakdown):
        failed_breakout  = current high pierced the prior swing high,
                            but current close came back below it (bearish)
        failed_breakdown = current low pierced the prior swing low,
                            but current close came back above it (bullish)

    Once there is enough history, both values are concrete booleans —
    they are never independently None; only the *pair* is (None, None)
    together when there isn't enough history yet (same 'stay silent
    rather than guess' convention as the rest of this module and
    OIVelocityTracker). At most one of the two is ever True (checked in
    the order below); both False means no failed break this candle.

    Needs at least `lookback` + 1 closed candles; returns (None, None)
    otherwise — same 'stay silent rather than guess' convention as the
    rest of this module and OIVelocityTracker.
    """
    if len(highs) < lookback + 1 or len(lows) < lookback + 1 or len(closes) < lookback + 1:
        return None, None

    swing_high = max(highs[-(lookback + 1):-1])
    swing_low = min(lows[-(lookback + 1):-1])
    cur_high, cur_low, cur_close = highs[-1], lows[-1], closes[-1]

    breakdown_failed = cur_low < swing_low and cur_close > swing_low
    if breakdown_failed:
        return False, True

    breakout_failed = cur_high > swing_high and cur_close < swing_high
    if breakout_failed:
        return True, False

    return False, False


def compute(rows, session_vwap: float | None = None,
            ema_fast_p: int = 20, ema_slow_p: int = 50,
            ma_long_p: int = 55, rsi_p: int = 14) -> Snapshot:
    """`rows` = closed candles ascending (sqlite3.Row or dict-like)."""
    if not rows:
        return Snapshot()
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    vols = [int(r["volume"] or 0) for r in rows]

    ef = ema(closes, ema_fast_p)[-1] if len(closes) >= ema_fast_p else None
    es = ema(closes, ema_slow_p)[-1] if len(closes) >= ema_slow_p else None
    tk, kj, sa, sb = ichimoku(highs, lows)
    fbo, fbd = failed_break(highs, lows, closes)

    return Snapshot(
        close=closes[-1],
        vwap=session_vwap if session_vwap is not None else (
            float(rows[-1]["vwap"]) if rows[-1]["vwap"] else None),
        ema_fast=ef, ema_slow=es,
        ma_long=sma(closes, ma_long_p),
        rsi=rsi(closes, rsi_p),
        tenkan=tk, kijun=kj, span_a=sa, span_b=sb,
        vol=vols[-1],
        avg_vol=(sum(vols[-20:]) / min(len(vols), 20)) if vols else None,
        failed_breakout=fbo, failed_breakdown=fbd,
    )


