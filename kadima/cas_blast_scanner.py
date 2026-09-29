"""
cas_blast_scanner.py -- CAS (Closing Session, 15:15 IST+) cheap-premium
acceleration scanner.

WHY THIS EXISTS:
    KADIMA's main entry logic (Bull Shield / Gamma Blast) is delta-filtered
    to 0.45-0.60 by design, so it never looks at the cheap OTM strikes that
    move hundreds of percent in the closing session (e.g. NIFTY 22650 CE,
    Rs 5 -> Rs 66 on 2026-09-29, after 15:20). This is a separate, smaller,
    higher-variance strategy for those moves -- not a change to the main
    engine.

2026-09-29 REWRITE (trader's decisions, see cas_executor.py for execution):
    - Fed in-process by engine._oi_loop after every successful chain poll
      (on_chain), reusing the same REST sample -- no extra broker load.
      The previous version was never started and called a chain method
      (far_otm_quotes) that does not exist.
    - Candidates are selected by premium band and strike distance, not by
      delta: chain quotes carry no computed delta, and on expiry day delta
      bands are unstable anyway.
    - Volume uses per-poll traded volume (tradeVolume is cumulative for the
      day; the old trigger compared cumulative totals and could not fire).
    - Direction must agree with spot: CE only when spot rose over the
      trigger window, PE only when it fell.
    - A triggered candidate is handed to cas_executor, which applies every
      execution gate and places the order. With CAS_EXECUTION_ENABLED off,
      this module only alerts and logs, as before.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import alerts, audit
from .config import (CAS_ENTRY_END, CAS_ENTRY_START, CAS_MAX_PREMIUM,
                     CAS_MIN_PREMIUM, CAS_STRIKES_OTM, CAS_TRIGGER_POLLS,
                     CAS_TRIGGER_PREMIUM_PCT, CAS_TRIGGER_VOLUME_MULT,
                     CAS_VOLUME_TRAILING_POLLS)
from .utils import now_ist

MIN_VOLUME_SAMPLES = 5      # per-poll volume increments needed before a volume test


@dataclass
class StrikeHistory:
    premiums: deque = field(default_factory=lambda: deque(maxlen=CAS_TRIGGER_POLLS + 1))
    vol_increments: deque = field(default_factory=lambda: deque(maxlen=CAS_VOLUME_TRAILING_POLLS + 1))
    last_cum_volume: Optional[int] = None
    triggered: bool = False


@dataclass
class CasCandidate:
    index: str
    symbol: str
    token: str
    exchange: str
    strike: float
    option_type: str
    ltp: float
    bid: Optional[float]
    ask: Optional[float]
    premium_change_pct: float
    volume: int
    volume_avg: float
    spot: float
    spot_change: float
    detected_at: datetime


def in_cas_window(dt: Optional[datetime] = None) -> bool:
    n = dt or now_ist()
    return CAS_ENTRY_START <= (n.hour, n.minute) < CAS_ENTRY_END


def select_strikes(quotes, spot: float, strike_step: float) -> list:
    """Cheap OTM strikes near the money: CE at/above ATM, PE at/below ATM,
    up to CAS_STRIKES_OTM steps away, premium inside the CAS band."""
    if not spot or not strike_step:
        return []
    atm = round(spot / strike_step) * strike_step
    reach = CAS_STRIKES_OTM * strike_step
    out = []
    for q in quotes:
        if q.ltp is None or not (CAS_MIN_PREMIUM <= q.ltp <= CAS_MAX_PREMIUM):
            continue
        if q.opt_type == "CE" and atm <= q.strike <= atm + reach:
            out.append(q)
        elif q.opt_type == "PE" and atm - reach <= q.strike <= atm:
            out.append(q)
    return out


class CasBlastScanner:
    def __init__(self, executor_hook=None) -> None:
        self._history: dict[str, StrikeHistory] = {}
        self._spot: dict[str, deque] = {}
        self._day: Optional[str] = None
        self._lock = threading.Lock()
        self._executor_hook = executor_hook   # tests inject; default is cas_executor.submit

    def _roll_day(self, now: datetime) -> None:
        day = now.date().isoformat()
        if day != self._day:
            self._day = day
            self._history.clear()
            self._spot.clear()

    # --------------------------------------------------------------- feed
    def on_chain(self, index_name: str, chain, spot: Optional[float]) -> list[CasCandidate]:
        """Called after every successful poll of `chain`. Updates history for
        all CAS-band strikes every poll (so history exists when the window
        opens) and returns the candidates that triggered on this poll."""
        now = now_ist()
        if not spot:
            return []
        with self._lock:
            self._roll_day(now)
            spots = self._spot.setdefault(index_name, deque(maxlen=CAS_TRIGGER_POLLS + 1))
            spots.append(float(spot))
            triggered = []
            for q in select_strikes(chain.quotes.values(), spot, chain.index.strike_step):
                hist = self._history.setdefault(q.token, StrikeHistory())
                hist.premiums.append(q.ltp)
                cum = q.volume or 0
                if hist.last_cum_volume is not None and cum >= hist.last_cum_volume:
                    hist.vol_increments.append(cum - hist.last_cum_volume)
                hist.last_cum_volume = cum
                if not in_cas_window(now):
                    continue
                cand = self._check(index_name, chain, q, hist, spots, now)
                if cand is not None:
                    hist.triggered = True
                    triggered.append(cand)
        if triggered:
            best = max(triggered, key=lambda c: c.premium_change_pct)
            for c in triggered:
                audit.log("cas_blast_candidate_triggered", index=c.index, symbol=c.symbol,
                          strike=c.strike, option_type=c.option_type, ltp=c.ltp,
                          premium_change_pct=c.premium_change_pct, volume=c.volume,
                          volume_avg=c.volume_avg, spot_change=c.spot_change,
                          selected=c is best)
            alerts.enqueue(
                f"\U0001F680 <b>CAS BLAST</b> — {best.symbol}\n"
                f"Premium {best.ltp} (+{best.premium_change_pct}%)\n"
                f"Volume {best.volume} (avg {best.volume_avg}) | spot {best.spot_change:+.1f}",
                key=f"cas_blast:{best.symbol}")
            self._dispatch(best, chain)
        return triggered

    def _check(self, index_name, chain, q, hist, spots, now) -> Optional[CasCandidate]:
        if hist.triggered or len(hist.premiums) < CAS_TRIGGER_POLLS + 1:
            return None
        old, new = hist.premiums[0], hist.premiums[-1]
        if not old or old <= 0:
            return None
        change = (new - old) / old * 100.0
        if change < CAS_TRIGGER_PREMIUM_PCT:
            return None
        if len(hist.vol_increments) < MIN_VOLUME_SAMPLES:
            return None
        latest = hist.vol_increments[-1]
        prior = list(hist.vol_increments)[:-1]
        avg = sum(prior) / len(prior)
        if avg <= 0 or latest < avg * CAS_TRIGGER_VOLUME_MULT:
            return None
        if len(spots) < 2:
            return None
        spot_change = spots[-1] - spots[0]
        if (q.opt_type == "CE" and spot_change <= 0) or (q.opt_type == "PE" and spot_change >= 0):
            return None
        return CasCandidate(
            index=index_name, symbol=q.symbol, token=q.token,
            exchange=chain.index.opt_exchange, strike=q.strike, option_type=q.opt_type,
            ltp=new, bid=q.bid, ask=q.ask, premium_change_pct=round(change, 1),
            volume=latest, volume_avg=round(avg, 1), spot=spots[-1],
            spot_change=round(spot_change, 2), detected_at=now)

    def _dispatch(self, candidate: CasCandidate, chain) -> None:
        hook = self._executor_hook
        if hook is None:
            from . import cas_executor
            hook = cas_executor.submit
        try:
            hook(candidate, chain)
        except Exception as exc:                        # noqa: BLE001
            audit.log("cas_blast_dispatch_failed", symbol=candidate.symbol, error=str(exc))


SCANNER = CasBlastScanner()
