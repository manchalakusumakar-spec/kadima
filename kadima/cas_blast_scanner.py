"""CAS premium acceleration on existing chain polls; execution is default-off.

Signal weights/thresholds are unchanged. Only fresh, continuous, expiry-day,
whole-lot executable contracts enter the shortlist. Blocked dispatch is not a
consumed trade: subsequent fresh samples can retry through every executor gate.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import threading
import time
from typing import Optional

from . import alerts, audit, cas_safety as safety
from .config import (CAS_ENTRY_END, CAS_ENTRY_START, CAS_MAX_PREMIUM, CAS_MAX_SPREAD_PCT,
                     CAS_MIN_PREMIUM, CAS_STRIKES_OTM, CAS_TRIGGER_POLLS,
                     CAS_TRIGGER_PREMIUM_PCT, CAS_TRIGGER_VOLUME_MULT, CAS_VOLUME_TRAILING_POLLS)
from .utils import now_ist

MIN_VOLUME_SAMPLES = 5


@dataclass
class StrikeHistory:
    samples: deque = field(default_factory=lambda: deque(maxlen=CAS_VOLUME_TRAILING_POLLS+2))
    alerted_at: Optional[datetime] = None


@dataclass(frozen=True)
class CasCandidate:
    index: str
    symbol: str
    token: str
    exchange: str
    strike: float
    option_type: str
    ltp: float
    bid: float
    ask: float
    premium_change_pct: float
    volume: int
    volume_avg: float
    spot: float
    spot_change: float
    detected_at: datetime
    expiry: str
    lot_size: int
    quote_at: str
    spot_at: str
    base_spot: float
    bid_qty: int
    ask_qty: int


def in_cas_window(dt=None):
    n = dt or now_ist()
    return (isinstance(n, datetime) and n.tzinfo is not None
            and n.astimezone(safety.IST).weekday() < 5
            and CAS_ENTRY_START <= (n.astimezone(safety.IST).hour, n.astimezone(safety.IST).minute) < CAS_ENTRY_END)


def select_strikes(quotes, spot, strike_step):
    if not safety.number(spot, positive=True) or not safety.number(strike_step, positive=True):
        return []
    atm = round(spot/strike_step)*strike_step
    reach = CAS_STRIKES_OTM*strike_step
    return [q for q in quotes if safety.number(q.ltp, positive=True)
            and CAS_MIN_PREMIUM <= q.ltp <= CAS_MAX_PREMIUM
            and safety.number(q.strike, positive=True)
            and ((q.opt_type == 'CE' and atm <= q.strike <= atm+reach)
                 or (q.opt_type == 'PE' and atm-reach <= q.strike <= atm))]


class CasBlastScanner:
    def __init__(self, executor_hook=None):
        self._history = {}
        self._day = None
        self._lock = threading.Lock()
        self._executor_hook = executor_hook

    def on_chain(self, index_name, chain, spot):
        now = now_ist()
        if not safety.number(spot, positive=True):
            return []
        spot_at = getattr(chain, '_cas_spot_at', None)
        if (not safety.fresh(spot_at, now, 30)
                or not 0 <= time.monotonic()-getattr(chain, '_last_poll', 0) <= 30):
            return []
        triggered = []
        with self._lock:
            day = now.astimezone(safety.IST).date().isoformat()
            if day != self._day:
                self._day = day
                self._history.clear()
            wanted = {q.token for q in select_strikes(chain.quotes.values(), spot, chain.index.strike_step)}
            current_keys = set()
            for q in list(chain.quotes.values()):
                spec = safety.contract(chain, q, now)
                if spec is None:
                    continue
                key = (chain.index.opt_exchange, q.token, spec['expiry'])
                current_keys.add(key)
                hist = self._history.setdefault(key, StrikeHistory())
                at = safety.stamp(getattr(q, 'quote_at', None))
                valid = (safety.fresh(at, now) and safety.fresh(getattr(q, 'trade_at', None), now)
                         and safety.number(q.ltp, positive=True)
                         and type(q.volume) is int and q.volume >= 0)
                if not valid:
                    hist.samples.clear()
                    continue
                if hist.samples:
                    delta = (at-hist.samples[-1][0]).total_seconds()
                    if delta == 0:
                        continue  # The same exchange sample is never a second poll.
                    if delta < 0 or delta > safety.HISTORY_MAX_GAP or q.volume < hist.samples[-1][2]:
                        hist.samples.clear()
                hist.samples.append((at, q.ltp, q.volume, spot))
                if q.token not in wanted or not in_cas_window(now):
                    continue
                cand = self._check(index_name, chain, q, hist, spec, spot_at, now)
                if cand is not None:
                    triggered.append(cand)
            # Bound memory to current ladders plus other index ladders, not every token ever seen.
            self._history = {k:h for k,h in self._history.items()
                             if k[0] != chain.index.opt_exchange or k in current_keys
                             or (h.samples and (now-h.samples[-1][0]).total_seconds() <= safety.HISTORY_MAX_GAP)}
        if not triggered:
            return []
        # Original acceleration ranks only candidates which already pass book/depth gates.
        best = max(triggered, key=lambda c: (c.premium_change_pct,
                   min(c.bid_qty, c.ask_qty)//c.lot_size, -(c.ask-c.bid)/((c.ask+c.bid)/2), c.token))
        for c in triggered:
            audit.log('cas_blast_candidate_triggered', index=c.index, symbol=c.symbol,
                      token=c.token, exchange=c.exchange, expiry=c.expiry, quote_at=c.quote_at,
                      spot_at=c.spot_at, ltp=c.ltp, premium_change_pct=c.premium_change_pct,
                      volume=c.volume, volume_avg=c.volume_avg, selected=c is best,
                      model_version=safety.VERSION)
        with self._lock:
            h = self._history[(best.exchange, best.token, best.expiry)]
            announce = h.alerted_at is None or (now-h.alerted_at).total_seconds() >= 30
            if announce:
                h.alerted_at = now
        if announce:
            alerts.enqueue(f'CAS candidate {best.symbol}: {best.ltp:.2f}, '
                           f'+{best.premium_change_pct:.1f}%; awaiting execution gates.',
                           key=f'cas_blast:{best.exchange}:{best.token}')
        self._dispatch(best, chain)
        return triggered

    def _check(self, index_name, chain, q, hist, spec, spot_at, now):
        rows = list(hist.samples)
        if len(rows) < max(CAS_TRIGGER_POLLS+1, MIN_VOLUME_SAMPLES+1):
            return None
        start = rows[-CAS_TRIGGER_POLLS-1]
        if (rows[-1][0]-start[0]).total_seconds() < safety.MIN_TRIGGER_SPAN:
            return None
        change = (rows[-1][1]/start[1]-1)*100
        increments = [b[2]-a[2] for a,b in zip(rows, rows[1:])]
        latest, prior = increments[-1], increments[:-1]
        avg = sum(prior)/len(prior)
        move = rows[-1][3]-start[3]
        if (change+1e-10 < CAS_TRIGGER_PREMIUM_PCT or avg <= 0
                or latest < avg*CAS_TRIGGER_VOLUME_MULT
                or (move <= 0 if q.opt_type == 'CE' else move >= 0)):
            return None
        book = dict(ltp=q.ltp, bid=q.bid, ask=q.ask, bid_qty=getattr(q,'bid_qty',None),
                    ask_qty=getattr(q,'ask_qty',None), quote_at=q.quote_at, trade_at=q.trade_at)
        if safety.entry_book_reason(book, now, spec['lot_size'], CAS_MAX_SPREAD_PCT):
            return None
        return CasCandidate(index_name,q.symbol,q.token,chain.index.opt_exchange,q.strike,q.opt_type,
            q.ltp,q.bid,q.ask,round(change,2),latest,avg,rows[-1][3],move,rows[-1][0],
            spec['expiry'],spec['lot_size'],rows[-1][0].isoformat(),
            safety.stamp(spot_at).isoformat(),start[3],q.bid_qty,q.ask_qty)

    def _dispatch(self, candidate, chain):
        try:
            if self._executor_hook is None:
                from .cas_executor import submit
                submit(candidate, chain)
            else:
                self._executor_hook(candidate, chain)
        except Exception as exc:
            audit.log('cas_blast_dispatch_failed', symbol=candidate.symbol, error_type=type(exc).__name__)


SCANNER = CasBlastScanner()
