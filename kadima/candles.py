"""
Candle engine: ticks -> 1/3/5-minute OHLCV + session VWAP.

Correctness note. Angel One's `volume_trade_for_the_day` is CUMULATIVE, not
per-tick. Candle volume is therefore the difference between the last cumulative
value in this candle and the last value in the previous one. Treating it as
per-tick volume - an easy mistake - inflates volume by orders of magnitude and
will make your 15%-weight volume-spike trigger fire constantly.

Before you trust a single indicator, run `tools/verify_candles.py` and diff
these bars against the same bars on your broker chart. Do this for a full
session. Everything downstream inherits these numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Callable

from . import audit, storage
from .config import CANDLE_INTERVALS_MIN, IST
from .utils import candle_start


@dataclass
class Candle:
    token: str
    interval: int
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    cum_vol_start: int | None = None
    cum_vol_last: int | None = None
    pv: float = 0.0          # sum(price * vol) for VWAP
    vol_for_vwap: int = 0
    closed: bool = False
    volume_valid: bool = True

    @property
    def vwap(self) -> float | None:
        return (self.pv / self.vol_for_vwap
                if self.volume_valid and self.vol_for_vwap else None)


class CandleBuilder:
    """One per (token). Maintains all configured intervals simultaneously."""

    def __init__(self, token: str,
                 on_close: Callable[[Candle], None] | None = None) -> None:
        self.token = token
        self.on_close = on_close
        self._live: dict[int, Candle] = {}
        # session VWAP accumulators (reset daily)
        self._sess_pv = 0.0
        self._sess_vol = 0
        self._sess_day: str | None = None
        self._last_ts: datetime | None = None
        self._last_cum: int | None = None
        self._volume_valid = True

    # ------------------------------------------------------------------ public
    def add_tick(self, ts: datetime, ltp: float, cum_volume: int | None) -> None:
        if (not isinstance(ts, datetime) or ts.tzinfo is None
                or ts.utcoffset() is None or isinstance(ltp, bool)
                or not isinstance(ltp, (int, float))
                or not math.isfinite(ltp) or ltp <= 0):
            return
        ts = ts.astimezone(IST)
        # Check before daily reset: an old-session tick must not reset today.
        if self._last_ts is not None and ts < self._last_ts:
            return
        self._roll_session(ts)
        self._last_ts = ts
        # Close every completed interval before adding the new tick to VWAP.
        # Callbacks must see the prefix ending at the old candle's close.
        for interval in CANDLE_INTERVALS_MIN:
            start = candle_start(ts, interval)
            cur = self._live.get(interval)
            if cur is not None and start > cur.start:
                self._close(cur)
            if cur is None or start > cur.start:
                self._live[interval] = self._new(interval, start, ltp, cum_volume)

        valid_volume = type(cum_volume) is int and cum_volume >= 0
        delta = 0
        if not valid_volume or (self._last_cum is not None and cum_volume < self._last_cum):
            # A reset/gap is not negative trading volume. Rebaseline without
            # inventing volume or assigning an unknown interval to this price.
            self._volume_valid = False
            for cur in self._live.values():
                cur.volume_valid = False
        elif self._last_cum is not None:
            delta = cum_volume - self._last_cum
        self._last_cum = cum_volume if valid_volume else None
        self._sess_pv += ltp * delta
        self._sess_vol += delta
        for cur in self._live.values():
            cur.volume_valid = cur.volume_valid and self._volume_valid
            cur.high = max(cur.high, ltp)
            cur.low = min(cur.low, ltp)
            cur.close = ltp
            cur.cum_vol_last = self._last_cum
            cur.volume += delta
            cur.pv += ltp * delta
            cur.vol_for_vwap += delta
            storage.upsert_candle(cur.token, cur.interval,
                                  cur.start.isoformat(), cur.open, cur.high,
                                  cur.low, cur.close, cur.volume, cur.vwap,
                                  False)

    def session_vwap(self) -> float | None:
        return (self._sess_pv / self._sess_vol
                if self._volume_valid and self._sess_vol else None)

    def live(self, interval: int) -> Candle | None:
        return self._live.get(interval)

    # ----------------------------------------------------------------- private
    def _new(self, interval: int, start: datetime, ltp: float,
             cum_volume: int | None) -> Candle:
        return Candle(token=self.token, interval=interval, start=start,
                      open=ltp, high=ltp, low=ltp, close=ltp,
                      cum_vol_start=cum_volume, cum_vol_last=cum_volume)

    def _close(self, c: Candle) -> None:
        c.closed = True
        storage.upsert_candle(c.token, c.interval, c.start.isoformat(),
                              c.open, c.high, c.low, c.close, c.volume,
                              c.vwap, True)
        if self.on_close:
            try:
                self.on_close(c)
            except Exception as exc:                    # noqa: BLE001
                audit.log("candle_callback_error", token=c.token,
                          interval=c.interval, error=str(exc))

    def _roll_session(self, ts: datetime) -> None:
        day = ts.strftime("%Y-%m-%d")
        if self._sess_day != day:
            self._sess_day, self._sess_pv, self._sess_vol = day, 0.0, 0
            self._last_cum = None
            self._volume_valid = True
            self._live.clear()
