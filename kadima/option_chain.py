
"""
Option chain: REST-polled OI, IV and Greeks + OI velocity tracking.

THE CADENCE CONSTRAINT - read this before tuning any OI rule.

Angel One's WebSocket 2.0 streams price and volume. It does NOT stream open
interest for option contracts. OI arrives only through the REST
`getMarketData(FULL)` call, which is rate-limited. So OI resolution is
OI_POLL_SEC (default 30s), not tick-level.

Your "wall loses >15% OI in 10 minutes" rule therefore observes ~20 samples per
window, not thousands. That is enough to detect a genuine wall collapse but NOT
enough to detect intra-minute OI micro-structure. Do not tune the threshold as
though you had tick-level OI. If a sample is missed (rate limit, timeout), the
window is marked degraded and velocity alerts are suppressed for that window
rather than computed on partial data - a false Early Warning from a missing
sample is worse than no warning.
"""
from __future__ import annotations

import time
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import audit, storage
from .config import (INDICES, IST, MIN_OI, MIN_VOLUME,
                     OI_VELOCITY_DROP_PCT_BY_INDEX, OI_VELOCITY_WINDOW_MIN,
                     STRIKES_EACH_SIDE)
from .session import SESSION


@dataclass
class OptionQuote:
    token: str
    symbol: str
    strike: float
    opt_type: str          # CE / PE
    ltp: float | None = None
    oi: int | None = None
    oi_change: int | None = None
    volume: int | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    bid: float | None = None
    ask: float | None = None
    option_vwap: float | None = None
    quote_at: str | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None

    @property
    def spread_pct(self) -> float | None:
        if not self.bid or not self.ask or self.bid <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / mid) * 100 if mid else None

    @property
    def tradeable(self) -> bool:
        if (self.oi or 0) < MIN_OI or (self.volume or 0) < MIN_VOLUME:
            return False
        sp = self.spread_pct
        return sp is not None and sp <= 1.5


@dataclass
class OISample:
    ts: datetime
    oi_by_token: dict[str, int]


class OIVelocityTracker:
    """Rolling-window detection of OI walls losing size."""

    def __init__(self, index_name: str) -> None:
        self.index_name = index_name
        self.threshold = OI_VELOCITY_DROP_PCT_BY_INDEX.get(index_name, 15.0)
        self.window = timedelta(minutes=OI_VELOCITY_WINDOW_MIN)
        self.samples: deque[OISample] = deque(maxlen=200)
        self.degraded_until: datetime | None = None

    def mark_degraded(self, seconds: int = 600) -> None:
        self.degraded_until = datetime.now(IST) + timedelta(seconds=seconds)
        audit.log("oi_window_degraded", index=self.index_name)

    def add(self, quotes: list[OptionQuote]) -> None:
        self.samples.append(OISample(
            ts=datetime.now(IST),
            oi_by_token={q.token: (q.oi or 0) for q in quotes}))

    def alerts(self) -> list[dict]:
        """Tokens whose OI dropped more than threshold across the window."""
        now = datetime.now(IST)
        if self.degraded_until and now < self.degraded_until:
            return []
        if len(self.samples) < 2:
            return []

        oldest = None
        for s in self.samples:
            if now - s.ts <= self.window:
                oldest = s
                break
        if oldest is None:
            return []

        expected = self.window.total_seconds() / 30
        span_ok = len([s for s in self.samples
                       if now - s.ts <= self.window]) >= expected * 0.7
        if not span_ok:
            return []                      # partial window: stay silent

        latest = self.samples[-1]
        out = []
        for token, old_oi in oldest.oi_by_token.items():
            new_oi = latest.oi_by_token.get(token)
            if not old_oi or new_oi is None:
                continue
            drop = ((old_oi - new_oi) / old_oi) * 100
            if drop >= self.threshold:
                out.append({"token": token, "drop_pct": round(drop, 2),
                            "from_oi": old_oi, "to_oi": new_oi,
                            "window_min": OI_VELOCITY_WINDOW_MIN})
        return out


class OptionChain:
    """Holds the ATM +/- N ladder for one index and refreshes it via REST."""

    def __init__(self, index_or_object, scrips, strikes_each_side=None) -> None:
        # Accepts EITHER a registered index name (str, existing behavior --
        # engine.py's 4-index calls are untouched) OR an Index object
        # directly (new, 2026-08-23 -- lets the stock-universe scanner
        # construct an ad-hoc Index per underlying, using its own inferred
        # strike_step, and reuse this exact class unmodified rather than
        # duplicating OI polling / ladder-building logic for stocks).
        if isinstance(index_or_object, str):
            self.index = INDICES[index_or_object]
            index_name = index_or_object
        else:
            self.index = index_or_object
            index_name = self.index.name
        self.scrips = scrips            # ScripMaster instance
        self.strikes_each_side = STRIKES_EACH_SIDE if strikes_each_side is None else strikes_each_side
        self.quotes: dict[str, OptionQuote] = {}
        self.velocity = OIVelocityTracker(index_name)
        self.spot: float | None = None
        self.atm: float | None = None
        self._last_poll = 0.0
        self._expiry = None

    # -------------------------------------------------------------- ladder
    def rebuild_ladder(self, spot: float) -> None:
        step = self.index.strike_step
        atm = round(spot / step) * step
        expiry = self.scrips.nearest_expiry(self.index)
        if atm == self.atm and expiry == self._expiry:
            return
        self._expiry = expiry
        self._last_poll = 0.0
        self._forecast_sample = None
        self.atm = atm
        contracts = self.scrips.strikes_around(
            self.index, atm, self.strikes_each_side)
        self.quotes = {c["token"]: OptionQuote(
            token=c["token"], symbol=c["symbol"], strike=c["strike"],
            opt_type=c["opt_type"]) for c in contracts}
        audit.log("ladder_rebuilt", index=self.index.name, atm=atm,
                  contracts=len(self.quotes))

    # --------------------------------------------------------------- polling
    def poll(self) -> bool:
        """One REST refresh. Returns False if the sample was lost."""
        if not self.quotes:
            return False
        tokens = list(self.quotes.keys())
        payload = {"mode": "FULL",
                   "exchangeTokens": {self.index.opt_exchange: tokens}}
        try:
            SESSION.ensure()
            try:
                resp = SESSION.api.getMarketData(**payload)
            except TypeError:
                resp = SESSION.api.getMarketData("FULL", {self.index.opt_exchange: tokens})
        except Exception as exc:                        # noqa: BLE001
            audit.log("oi_poll_failed", index=self.index.name, error=str(exc))
            self.velocity.mark_degraded()
            return False

        if not resp or not resp.get("status"):
            audit.log("oi_poll_bad_response", index=self.index.name,
                      response=str(resp)[:300])
            self.velocity.mark_degraded()
            return False

        fetched = resp.get("data", {}).get("fetched", [])
        return self.accept_full_sample(fetched)

    def accept_full_sample(self, fetched) -> bool:
        """Atomically accept one complete ladder, including shared batch polls."""
        tokens = set(self.quotes)
        if not tokens or not isinstance(fetched, list):
            return False
        received = {str(row.get('symbolToken')) for row in fetched}
        if not set(tokens).issubset(received):
            self.velocity.mark_degraded()
            audit.log('oi_poll_incomplete', index=self.index.name,
                      expected=len(tokens), received=len(received))
            return False
        updated = {}
        for row in fetched:
            token = str(row.get("symbolToken"))
            old = self.quotes.get(token)
            if not old:
                continue
            if token in updated or row.get('exchange', self.index.opt_exchange) != self.index.opt_exchange:
                self.velocity.mark_degraded()
                return False
            q = OptionQuote(old.token, old.symbol, old.strike, old.opt_type)
            q.ltp = _f(row.get("ltp"))
            q.oi = _i(row.get("opnInterest"))
            q.oi_change = _i(row.get("netChangeOpnInterest"))
            q.volume = _i(row.get("tradeVolume"))
            depth = row.get("depth") or {}
            buy, sell = depth.get("buy") or [], depth.get("sell") or []
            q.bid = _f(buy[0].get("price")) if buy else None
            q.ask = _f(sell[0].get("price")) if sell else None
            # CAS requires the broker's average traded price and source time;
            # missing fields block only CAS, never synthesize a VWAP from LTP.
            q.option_vwap = _f(row.get('avgPrice'))
            q.quote_at = row.get('exchFeedTime')
            q.bid_qty = _i(buy[0].get('quantity')) if buy else None
            q.ask_qty = _i(sell[0].get('quantity')) if sell else None
            if (q.ltp is None or not math.isfinite(q.ltp) or q.ltp <= 0
                    or q.oi is None or q.volume is None or q.oi < 0 or q.volume < 0):
                self.velocity.mark_degraded()
                return False
            updated[token] = q

        self.quotes = updated
        self.velocity.add(list(self.quotes.values()))
        storage.insert_oi_snapshot(self.index.name, [
            {"token": q.token, "symbol": q.symbol, "strike": q.strike,
             "opt_type": q.opt_type, "ltp": q.ltp, "oi": q.oi,
             "oi_change": q.oi_change, "volume": q.volume, "iv": q.iv,
             "delta": q.delta, "gamma": q.gamma, "theta": q.theta,
             "vega": q.vega} for q in self.quotes.values()])
        self._last_poll = time.monotonic()
        # Publish a completed immutable-by-convention sample for the observer.
        # Never expose a half-refreshed ladder while poll mutates quote objects.
        self._forecast_sample = dict(mono=self._last_poll, expiry=self._expiry,
            quotes=[dict(token=q.token,symbol=q.symbol,strike=q.strike,opt_type=q.opt_type,
                ltp=q.ltp,oi=q.oi,oi_change=q.oi_change,volume=q.volume,iv=q.iv,
                delta=q.delta,gamma=q.gamma,theta=q.theta,vega=q.vega,bid=q.bid,ask=q.ask,
                option_vwap=q.option_vwap,quote_at=q.quote_at,bid_qty=q.bid_qty,ask_qty=q.ask_qty)
                for q in self.quotes.values()])
        return True

    # ------------------------------------------------------------- analytics
    def pcr(self) -> float | None:
        ce = sum(q.oi or 0 for q in self.quotes.values() if q.opt_type == "CE")
        pe = sum(q.oi or 0 for q in self.quotes.values() if q.opt_type == "PE")
        return (pe / ce) if ce else None

    def max_pain(self) -> float | None:
        strikes = sorted({q.strike for q in self.quotes.values()})
        if not strikes:
            return None
        best, best_pain = None, None
        for k in strikes:
            pain = 0.0
            for q in self.quotes.values():
                oi = q.oi or 0
                if q.opt_type == "CE" and k > q.strike:
                    pain += (k - q.strike) * oi
                elif q.opt_type == "PE" and k < q.strike:
                    pain += (q.strike - k) * oi
            if best_pain is None or pain < best_pain:
                best, best_pain = k, pain
        return best

    def walls(self, n: int = 3) -> dict[str, list[OptionQuote]]:
        ce = sorted((q for q in self.quotes.values() if q.opt_type == "CE"),
                    key=lambda q: q.oi or 0, reverse=True)[:n]
        pe = sorted((q for q in self.quotes.values() if q.opt_type == "PE"),
                    key=lambda q: q.oi or 0, reverse=True)[:n]
        return {"CE": ce, "PE": pe}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None



