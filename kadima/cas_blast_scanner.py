"""
cas_blast_scanner.py -- CAS (Closing Session, 15:15 IST+) far-OTM premium
acceleration scanner.

WHY THIS EXISTS:
    Kusumakar identified that KADIMA's main entry logic (Bull Shield /
    Gamma Blast) is delta-filtered to 0.45-0.60 by design -- correctly,
    for that strategy. But that means it will NEVER look at far-OTM,
    single-digit-premium strikes that occasionally move hundreds of
    percent in the closing session as spot approaches a strike late in
    the day. This is a deliberately separate, smaller, higher-variance
    strategy to catch those moves -- NOT a replacement for or
    modification of the main engine.

DEFAULTS CHOSEN FOR THIS FIRST VERSION (flag any to change):
    - Watchlist: the 5 existing indices only (NIFTY, BANKNIFTY, SENSEX,
      MIDCPNIFTY, BANKEX), far-OTM band delta 0.05-0.20 -- NOT the full
      F&O stock universe yet. Start small, expand once proven.
    - Trigger: premium up >=40% over the last 2 one-minute candles,
      AND volume >= 3x its own trailing 20-candle average.
    - Position size: always exactly 1 lot (no tier-based sizing).
    - Exit: book 50% at +50% gain, trail remainder; hard time-cut exit
      if no favorable move within 2 candles of entry.
    - Runs only 15:15 IST to market close.

SAFETY DESIGN -- OBSERVE AND ALERT ONLY (this version):
    This module does NOT call auto_executor.py or place any live order.
    It scores candidates and sends an alert + audit log entry the
    moment one triggers. This is deliberate: this is new, unreviewed
    code touching real-money decision logic, and it has not yet run
    against a single real session. Wiring it to actually place orders
    is a follow-up step, once you've watched it flag real candidates
    and agree its trigger logic is sound.

    To go from observe-only to live execution later, the hook point is
    marked below (see `_maybe_execute`) -- it currently only logs.

RUNS AS A SEPARATE PROCESS/SERVICE, matching the existing
stock_universe_scanner.py pattern -- deliberately isolated from the
main kadima.service for failure-domain isolation (per this box's
documented OOM history, though RAM is now 2GB).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import alerts, audit
from .config import IST
from .instrument_master import get_lot_size
from .session import SESSION
from .utils import now_ist

# ---------------------------------------------------------------- config
CAS_START_HOUR = 15
CAS_START_MINUTE = 15

MIN_DELTA = 0.05
MAX_DELTA = 0.20          # far-OTM band -- deliberately below the main
                          # engine's 0.45-0.60 filter

TRIGGER_PREMIUM_PCT = 40.0        # premium up >= this %, over...
TRIGGER_CANDLE_WINDOW = 2         # ...this many 1-min candles
TRIGGER_VOLUME_MULT = 3.0         # AND volume >= this x trailing avg
VOLUME_TRAILING_WINDOW = 20       # candles for the trailing average

FIXED_LOTS = 1                    # always exactly 1 lot -- no tier sizing
PROFIT_BOOK_PCT = 50.0            # book this % of position at...
PROFIT_BOOK_TRIGGER_PCT = 50.0    # ...this % gain
TIME_CUT_CANDLES = 2              # exit if no favorable move within N
                                   # candles of entry

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY", "BANKEX"]

POLL_INTERVAL_SEC = 15


# --------------------------------------------------------------- state
@dataclass
class StrikeHistory:
    """Rolling premium/volume history for one far-OTM strike, used to
    detect acceleration relative to its OWN recent behaviour, not an
    absolute threshold (a Rs 2 option's normal noise looks huge in
    absolute terms; relative-to-self is what actually signals a real
    move)."""
    symbol: str
    token: str
    premiums: deque = field(default_factory=lambda: deque(maxlen=TRIGGER_CANDLE_WINDOW + 1))
    volumes: deque = field(default_factory=lambda: deque(maxlen=VOLUME_TRAILING_WINDOW))
    last_alerted_at: Optional[datetime] = None


@dataclass
class CasCandidate:
    symbol: str
    token: str
    strike: float
    option_type: str
    ltp: float
    premium_change_pct: float
    volume: int
    volume_avg: float
    delta: Optional[float]


class CasBlastScanner:
    def __init__(self) -> None:
        self._history: dict[str, StrikeHistory] = {}
        self._running = False

    # ------------------------------------------------------------ window
    def in_cas_window(self) -> bool:
        n = now_ist()
        return (n.hour > CAS_START_HOUR
                or (n.hour == CAS_START_HOUR and n.minute >= CAS_START_MINUTE))

    # ------------------------------------------------------------- scan
    def scan_once(self, chains: dict) -> list[CasCandidate]:
        """
        chains: {index_name: OptionChain}, one per watched index, as
        supplied by the caller (this module does not fetch its own
        chain data -- it reuses whatever the main engine's IndexState
        objects already hold, to avoid a second redundant Angel One
        WebSocket/REST load).

        Returns the list of candidates that triggered THIS scan.
        """
        triggered: list[CasCandidate] = []

        for index_name, chain in chains.items():
            for quote in chain.far_otm_quotes(MIN_DELTA, MAX_DELTA):
                key = quote.token
                hist = self._history.setdefault(
                    key, StrikeHistory(symbol=quote.symbol, token=quote.token))

                hist.premiums.append(quote.ltp)
                hist.volumes.append(quote.volume or 0)

                candidate = self._check_trigger(quote, hist)
                if candidate is not None:
                    triggered.append(candidate)
                    from . import trade_audit
                    trade_audit.safe_call(trade_audit.record, dict(
                        ts=now_ist().isoformat(), underlying=chain.index.name,
                        exchange=chain.index.opt_exchange, token=candidate.token,
                        symbol=candidate.symbol, strike=candidate.strike, direction=candidate.option_type,
                        ltp=candidate.ltp, stage='OBSERVATION_ONLY'), 'CAS', 'cas_blast')
                    hist.last_alerted_at = now_ist()

        return triggered

    def _check_trigger(self, quote, hist: StrikeHistory) -> Optional[CasCandidate]:
        if len(hist.premiums) < TRIGGER_CANDLE_WINDOW + 1:
            return None  # not enough history yet for this strike

        # Debounce: don't re-alert the same strike every poll once it's
        # already triggered once.
        if hist.last_alerted_at is not None:
            return None

        old_premium = hist.premiums[0]
        new_premium = hist.premiums[-1]
        if old_premium is None or old_premium <= 0:
            return None

        premium_change_pct = (new_premium - old_premium) / old_premium * 100.0
        if premium_change_pct < TRIGGER_PREMIUM_PCT:
            return None

        if len(hist.volumes) < VOLUME_TRAILING_WINDOW:
            return None  # not enough volume history to compute a trailing avg

        volume_avg = sum(hist.volumes) / len(hist.volumes)
        current_volume = hist.volumes[-1]
        if volume_avg <= 0 or current_volume < volume_avg * TRIGGER_VOLUME_MULT:
            return None

        candidate = CasCandidate(
            symbol=quote.symbol, token=quote.token, strike=quote.strike,
            option_type=quote.opt_type, ltp=new_premium,
            premium_change_pct=round(premium_change_pct, 1),
            volume=current_volume, volume_avg=round(volume_avg, 1),
            delta=quote.delta,
        )

        audit.log(
            "cas_blast_candidate_triggered",
            symbol=candidate.symbol, strike=candidate.strike,
            option_type=candidate.option_type, ltp=candidate.ltp,
            premium_change_pct=candidate.premium_change_pct,
            volume=candidate.volume, volume_avg=candidate.volume_avg,
            delta=candidate.delta,
        )
        alerts.send(
            f"\U0001F680 <b>CAS BLAST</b> — {candidate.symbol}\n"
            f"Premium {candidate.ltp} (+{candidate.premium_change_pct}%%)\n"
            f"Volume {candidate.volume} (avg {candidate.volume_avg})\n"
            f"Delta {candidate.delta}",
            key=f"cas_blast:{candidate.symbol}",
        )

        self._maybe_execute(candidate)
        return candidate

    # ------------------------------------------------------- execution hook
    def _maybe_execute(self, candidate: CasCandidate) -> None:
        """
        OBSERVE-ONLY in this version. Logs the sizing this WOULD have
        used, but places no order. To go live later: call
        auto_executor.EXECUTOR's order-placement path here directly
        (NOT evaluate_and_enter -- that function's gates are built for
        the main strategy's quality filters, which far-OTM CAS
        candidates will never pass; a CAS-specific execution path
        needs its own, separate, explicit entry function).
        """
        lot_size = get_lot_size(candidate.symbol, candidate.token)
        audit.log(
            "cas_blast_would_execute_dry_run",
            symbol=candidate.symbol, strike=candidate.strike,
            lots=FIXED_LOTS, lot_size=lot_size,
            note="OBSERVE-ONLY: no live order placed. Wire in explicitly "
                 "once trigger logic is confirmed against real sessions.",
        )

    # -------------------------------------------------------------- loop
    def run_forever(self, get_chains) -> None:
        """
        get_chains: a zero-arg callable returning {index_name: OptionChain}
        -- supplied by the caller (e.g. a thin wrapper reading the same
        IndexState objects engine.py already maintains).
        """
        self._running = True
        audit.log("cas_blast_scanner_starting")
        while self._running:
            try:
                if self.in_cas_window():
                    chains = get_chains()
                    self.scan_once(chains)
            except Exception as exc:  # noqa: BLE001
                audit.log("cas_blast_scanner_loop_exception", error=str(exc))
            time.sleep(POLL_INTERVAL_SEC)

    def stop(self) -> None:
        self._running = False
        audit.log("cas_blast_scanner_stopped")


SCANNER = CasBlastScanner()
