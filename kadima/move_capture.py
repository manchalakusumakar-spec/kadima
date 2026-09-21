"""Spot-first impulse detection for the F&O universe.

This module is deliberately broker-free. It receives validated spot ticks,
keeps bounded rolling state, computes heuristic impulse features, and exposes a
bounded hot list for shadow/alert use. Scores are relative activity scores, not
win probabilities and not permission to trade.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import statistics


@dataclass(frozen=True)
class SpotIdentity:
    name: str
    exchange: str
    token: str
    kind: str = "STOCK"


@dataclass(frozen=True)
class Sample:
    ts: datetime
    price: float
    volume: float | None


def _number(value, *, positive=False) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


class SymbolTracker:
    """At most ~16 minutes of one-second samples plus tiny return history."""

    def __init__(self, identity: SpotIdentity):
        self.identity = identity
        self.samples: deque[Sample] = deque(maxlen=1000)
        self.minute_returns: deque[float] = deque(maxlen=180)
        self.day = None
        self.high = None
        self.low = None
        self.previous_close = None
        self._minute_key = None
        self._last_cum_volume = None
        self._vwap_value = 0.0
        self._vwap_volume = 0.0

    def _reset_day(self, day):
        self.samples.clear()
        self.minute_returns.clear()
        self.day = day
        self.high = self.low = None
        self.previous_close = None
        self._minute_key = None
        self._last_cum_volume = None
        self._vwap_value = self._vwap_volume = 0.0

    def add(self, ts: datetime, price: float, volume=None, previous_close=None):
        if ts.tzinfo is None or not _number(price, positive=True):
            return False
        if self.day != ts.date():
            self._reset_day(ts.date())
        if _number(previous_close, positive=True):
            self.previous_close = float(previous_close)

        vol = float(volume) if _number(volume) else None
        if vol is not None:
            if self._last_cum_volume is None:
                self._last_cum_volume = vol
            elif vol >= self._last_cum_volume:
                delta = vol - self._last_cum_volume
                if delta:
                    self._vwap_value += float(price) * delta
                    self._vwap_volume += delta
                self._last_cum_volume = vol
            else:
                # Same-session volume reset is suspicious; restart VWAP math
                # rather than manufacturing negative traded volume.
                self._last_cum_volume = vol
                self._vwap_value = self._vwap_volume = 0.0

        self.high = float(price) if self.high is None else max(self.high, float(price))
        self.low = float(price) if self.low is None else min(self.low, float(price))

        bucket = ts.replace(microsecond=0)
        sample = Sample(bucket, float(price), vol)
        if self.samples and self.samples[-1].ts == bucket:
            self.samples[-1] = sample
        else:
            self.samples.append(sample)

        minute = bucket.replace(second=0)
        if self._minute_key is None:
            self._minute_key = minute
        elif minute != self._minute_key:
            r = self._return(timedelta(seconds=60))
            if r is not None:
                self.minute_returns.append(r)
            self._minute_key = minute
        return True

    @property
    def age_seconds(self):
        if len(self.samples) < 2:
            return 0.0
        return (self.samples[-1].ts - self.samples[0].ts).total_seconds()

    @property
    def vwap(self):
        return self._vwap_value / self._vwap_volume if self._vwap_volume > 0 else None

    def _at_or_before(self, target: datetime):
        for sample in reversed(self.samples):
            if sample.ts <= target:
                return sample
        return None

    def _return(self, age: timedelta):
        if not self.samples or self.age_seconds < age.total_seconds():
            return None
        latest = self.samples[-1]
        old = self._at_or_before(latest.ts - age)
        if old is None or old.price <= 0:
            return None
        return latest.price / old.price - 1.0

    def _volume_ratio(self):
        """Recent volume-rate / prior local baseline; time-of-day baseline is v2."""
        if not self.samples:
            return None
        latest = self.samples[-1]
        if latest.volume is None:
            return None

        # Prefer 60s versus preceding 5m after enough history. During the first
        # few minutes use 30s versus preceding 90s so opening impulses can still
        # be observed in shadow mode.
        if self.age_seconds >= 360:
            recent_s, baseline_s = 60, 300
        elif self.age_seconds >= 120:
            recent_s, baseline_s = 30, 90
        else:
            return None

        recent_start = self._at_or_before(latest.ts - timedelta(seconds=recent_s))
        baseline_start = self._at_or_before(
            latest.ts - timedelta(seconds=recent_s + baseline_s))
        if (recent_start is None or baseline_start is None
                or recent_start.volume is None or baseline_start.volume is None):
            return None
        recent_delta = latest.volume - recent_start.volume
        baseline_delta = recent_start.volume - baseline_start.volume
        if recent_delta < 0 or baseline_delta <= 0:
            return None
        recent_rate = recent_delta / recent_s
        baseline_rate = baseline_delta / baseline_s
        return recent_rate / baseline_rate if baseline_rate > 0 else None

    def _sigma_1m(self):
        values = [x for x in self.minute_returns if math.isfinite(x)]
        if len(values) < 5:
            return 0.0025  # startup floor for shadow scoring, not calibration.
        return max(0.0005, statistics.pstdev(values))

    def features(self):
        if not self.samples:
            return None
        latest = self.samples[-1]
        sigma = self._sigma_1m()
        returns = {}
        best = None
        for minutes in (1, 3, 5, 15):
            r = self._return(timedelta(minutes=minutes))
            returns[minutes] = r
            if r is None:
                continue
            z = abs(r) / (sigma * math.sqrt(minutes))
            if best is None or z > best[0]:
                best = (z, r, minutes)

        if best is None:
            return dict(ready=False, score=0.0, direction=None,
                        age_seconds=round(self.age_seconds, 1))

        p60 = self._at_or_before(latest.ts - timedelta(seconds=60))
        p120 = self._at_or_before(latest.ts - timedelta(seconds=120))
        acceleration = None
        if p60 and p120 and p60.price > 0 and p120.price > 0:
            acceleration = (latest.price / p60.price - 1) - (p60.price / p120.price - 1)

        volume_ratio = self._volume_ratio()
        vwap = self.vwap
        vwap_dev = (latest.price / vwap - 1) if vwap and vwap > 0 else None
        day_range = (self.high - self.low) if self.high is not None and self.low is not None else 0
        direction = "CE" if best[1] > 0 else "PE"
        location = 0.5
        if day_range > 0:
            location = ((latest.price - self.low) / day_range if direction == "CE"
                        else (self.high - latest.price) / day_range)
            location = min(1.0, max(0.0, location))

        gap = (latest.price / self.previous_close - 1
               if self.previous_close and self.previous_close > 0 else None)

        move_component = 40 * min(best[0] / 3.0, 1.0)
        accel_z = abs(acceleration) / sigma if acceleration is not None else 0.0
        accel_component = 25 * min(accel_z / 2.0, 1.0)
        volume_component = (20 * min(max(volume_ratio - 1.0, 0.0) / 3.0, 1.0)
                            if volume_ratio is not None else 0.0)
        vwap_z = abs(vwap_dev) / sigma if vwap_dev is not None else 0.0
        vwap_component = 10 * min(vwap_z / 2.0, 1.0)
        location_component = 5 * location
        score = min(100.0, move_component + accel_component + volume_component
                    + vwap_component + location_component)

        return dict(
            ready=self.age_seconds >= 60,
            score=round(score, 2),
            direction=direction,
            price=latest.price,
            observed_at=latest.ts.isoformat(),
            age_seconds=round(self.age_seconds, 1),
            sigma_1m_pct=round(sigma * 100, 4),
            return_1m_pct=None if returns[1] is None else round(returns[1] * 100, 4),
            return_3m_pct=None if returns[3] is None else round(returns[3] * 100, 4),
            return_5m_pct=None if returns[5] is None else round(returns[5] * 100, 4),
            return_15m_pct=None if returns[15] is None else round(returns[15] * 100, 4),
            acceleration_pct=None if acceleration is None else round(acceleration * 100, 4),
            volume_ratio=None if volume_ratio is None else round(volume_ratio, 3),
            vwap=None if vwap is None else round(vwap, 4),
            vwap_deviation_pct=None if vwap_dev is None else round(vwap_dev * 100, 4),
            gap_pct=None if gap is None else round(gap * 100, 4),
            day_high=self.high,
            day_low=self.low,
            location=round(location, 4),
            trigger_model="shadow_v1_intraday_self_normalized",
        )


class MoveCaptureDetector:
    """Bounded universe detector and Top-N hot list; no execution capability."""

    def __init__(self, *, trigger_score=65.0, release_score=45.0,
                 max_hot=10, stale_seconds=20, cooldown_seconds=120):
        if not 0 <= release_score < trigger_score <= 100:
            raise ValueError("invalid_move_capture_thresholds")
        if type(max_hot) is not int or not 1 <= max_hot <= 50:
            raise ValueError("invalid_move_capture_hot_limit")
        self.trigger_score = trigger_score
        self.release_score = release_score
        self.max_hot = max_hot
        self.stale_seconds = stale_seconds
        self.cooldown_seconds = cooldown_seconds
        self.trackers: dict[str, SymbolTracker] = {}
        self.hot: dict[str, dict] = {}
        self.last_event: dict[str, datetime] = {}
        self.tick_count = 0

    def ingest(self, identity: SpotIdentity, ts: datetime, price: float,
               volume=None, previous_close=None):
        tracker = self.trackers.setdefault(identity.name, SymbolTracker(identity))
        if not tracker.add(ts, price, volume, previous_close):
            return None, False
        self.tick_count += 1
        features = tracker.features()
        if not features:
            return None, False
        candidate = dict(
            underlying=identity.name, exchange=identity.exchange,
            token=identity.token, asset_scope=identity.kind,
            strategy="move_capture", **features)
        was_hot = identity.name in self.hot
        if features["ready"] and features["score"] >= self.trigger_score:
            self.hot[identity.name] = candidate
        elif was_hot and features["score"] >= self.release_score:
            self.hot[identity.name] = candidate
        else:
            self.hot.pop(identity.name, None)

        self._trim(ts)
        promoted = False
        if not was_hot and identity.name in self.hot:
            previous = self.last_event.get(identity.name)
            if previous is None or (ts - previous).total_seconds() >= self.cooldown_seconds:
                promoted = True
                self.last_event[identity.name] = ts
        return candidate, promoted

    def _trim(self, now):
        for name, row in list(self.hot.items()):
            try:
                stamp = datetime.fromisoformat(row["observed_at"])
                stale = stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= self.stale_seconds
            except (ValueError, TypeError, KeyError):
                stale = True
            if stale:
                self.hot.pop(name, None)
        if len(self.hot) > self.max_hot:
            keep = {r["underlying"] for r in sorted(
                self.hot.values(), key=lambda x: (-x["score"], x["underlying"]))[:self.max_hot]}
            self.hot = {k: v for k, v in self.hot.items() if k in keep}

    def snapshot(self, now: datetime):
        self._trim(now)
        rows = sorted(self.hot.values(), key=lambda x: (-x["score"], x["underlying"]))
        return dict(
            ts=now.isoformat(),
            mode="SHADOW_OBSERVE_ONLY",
            execution_enabled=False,
            symbols_seen=len(self.trackers),
            ticks_processed=self.tick_count,
            hot_limit=self.max_hot,
            trigger_score=self.trigger_score,
            release_score=self.release_score,
            hot=rows,
            score_note="Heuristic normalized impulse score; not a win probability.",
            baseline_note=("Shadow v1 uses each symbol's intraday rolling volatility and "
                           "recent-volume baseline. Persisted time-of-day baselines require calibration."),
        )
