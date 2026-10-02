
"""
KADIMA orchestrator.

Wires: feed -> candles -> indicators -> option chain -> four-index
confirmation -> strike ranking -> alerts.

The scoring weights below mirror Bull Shield v2.1. They are declared as data,
not code, so that when the forecast-vs-actual tracker tells you a weight is
miscalibrated you change one number here and nothing else.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from . import alerts, audit, indicators, killswitch, last_known, storage, strike_selector
from . import auto_executor
from .fast_momentum import Lane as FastLane
from .cas_engine import Lane as CASLane
from .shield_confirmation import (SHIELD_MIN_SCORE, BANK_NAMES, ShieldEvidence,
    alignment, fresh, confirmation_checks, is_shield_confirmed)
from .stock_execution import StockExecutionWorker
from .forecast_observer import ForecastObserver
from . import gamma_blast_scanner as gamma
from . import cas_blast_scanner
from .candles import Candle, CandleBuilder
from .config import (EMA_FAST, EMA_SLOW, GAMMA_EXPIRY_ONLY, GAMMA_INDICES,
                      GAMMA_SIGNAL_CUTOFF, IST, INDICES,
                     MA_LONG, MARKET_CLOSE, NO_NEW_SIGNALS_AFTER_HOUR,
                     OI_POLL_SEC, ORDER_PLACEMENT_ENABLED, RSI_PERIOD)
from .feed import TickFeed
from .option_chain import OptionChain
from .scrip_master import ScripMaster
from .session import SESSION
from .utils import in_market_hours, now_ist, past_cutoff, past_signal_cutoff

# ---------------------------------------------------------------- Bull Shield
WEIGHTS = {
    "vwap": 25.0,
    "ema_cross": 20.0,
    "rsi_50": 20.0,
    "volume_spike": 15.0,
    "oi_shift": 15.0,
    "failed_break": 5.0,
}
TIER_WATCH = 30.0
TIER_ACTIVE = 50.0
TIER_CONFIRMED = SHIELD_MIN_SCORE  # Both CE and PE; other entry gates still apply.

EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}


@dataclass
class IndexState:
    name: str
    spot: float | None = None
    builder: CandleBuilder | None = None
    chain: OptionChain | None = None
    snap: indicators.Snapshot = field(default_factory=indicators.Snapshot)
    score: float = 0.0
    direction: str | None = None
    tier: str = "NONE"
    tier_since: datetime | None = None
    components: dict = field(default_factory=dict)
    technical_at: datetime | None = None
    shield_evidence: ShieldEvidence | None = None
    shield_ready: bool = False
    shield_invalidated: bool = False
    shield_signal_id: int | None = None
    # Gamma blast scanner: a SEPARATE signal stream from Bull Shield above.
    # Same tier convention (WATCH/ACTIVE/CONFIRMED) so both read the same
    # way, but scored on different factors (OI velocity, IV expansion,
    # strike concentration — see gamma_blast_scanner.py's own docstring
    # for why this is 7 factors, not the "11" referenced in earlier notes).
    gamma_history: "gamma.GammaHistory" = field(default_factory=lambda: gamma.GammaHistory())
    gamma_result: "gamma.GammaResult | None" = None
    # The signal ID is reused after pre-submission rejection. The durable
    # execution ledger prevents a second submission for that episode.
    last_signaled_tier_since: datetime | None = None
    # Last-known persistence (added so /state and the dashboard show
    # real last-traded data outside market hours / right after a
    # restart, instead of None/0/NONE -- see last_known.py). Seeded
    # from disk in Engine.__init__ when no live tick has arrived yet
    # this process lifetime; is_live_tick flips True the moment a
    # real tick lands in _on_tick().
    last_updated: datetime | None = None
    is_live_tick: bool = False


class Engine:
    def __init__(self) -> None:
        self.scrips = ScripMaster.load()
        from . import celestial
        from .celestial_scanner import Radar
        self.radar = Radar()
        self.fast_lane = FastLane()
        self.cas_lane = CASLane()
        self._cas_reported = {}
        self._fast_reported = {}
        self.scan_indices = GAMMA_INDICES
        if celestial.enabled():
            from .celestial_universe import discover_indices
            self.scan_indices, missing = discover_indices(self.scrips, now_ist().date())
            celestial.health('index_universe',dict(active=list(self.scan_indices),unresolved=missing))
        auto_executor.EXECUTOR.celestial_master = self.scrips
        self.states: dict[str, IndexState] = {}
        self.token_to_index: dict[str, str] = {}
        self.feed = TickFeed(self._on_tick)
        self._stop = threading.Event()
        # 2026-10-02: candle-close work (indicator reads, scoring, Bull Shield
        # dispatch and its broker calls) runs on one signal-worker thread in
        # arrival order, never on the WebSocket tick callback. The lock keeps
        # tick-time invalidation and bar-close scoring from interleaving.
        self._signal_q: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
        self._shield_lock = threading.Lock()

        for name, idx in self.scan_indices.items():
            st = IndexState(name=name)
            st.builder = CandleBuilder(idx.token, on_close=self._queue_candle_close)
            st.chain = OptionChain(idx, self.scrips)
            self.states[name] = st
            self.token_to_index[idx.token] = name

        self.bank_states = {}
        self.bank_tokens = {}
        for name in BANK_NAMES:
            matches = [r for r in self.scrips.rows if r.get('exch_seg') == 'NSE'
                       and r.get('symbol') == name + '-EQ' and r.get('token')]
            if len(matches) != 1:
                audit.log('bank_confirmation_source_unavailable', bank=name)
                continue
            token = str(matches[0]['token'])
            bank = IndexState(name=name)
            bank.builder = CandleBuilder(token, on_close=self._queue_bank_close)
            self.bank_states[name] = bank
            self.bank_tokens[token] = name

        self.bank_history = StockExecutionWorker(auto_executor.EXECUTOR, self.scrips)
        self._bank_warmup_at = {}
        self.stock_worker = StockExecutionWorker(auto_executor.EXECUTOR, self.scrips)
        self._seed_last_known()
        self.forecaster = ForecastObserver(self, auto_executor.EXECUTOR)
        from .trade_learning import Learner
        self.learner = Learner()

    def _seed_last_known(self) -> None:
        """Pre-populate spot/score/tier/direction from the last
        persisted snapshot (previous session's close, or wherever the
        engine last got to before a restart) so /state has real
        last-traded data immediately, rather than None/0/NONE until
        the first live tick of the new run arrives."""
        saved = last_known.load_all()
        for name, st in self.states.items():
            entry = saved.get(name)
            if not entry:
                continue
            st.spot = entry.get("spot")
            st.score = entry.get("score", 0.0)
            # Persisted display data is not live confirmation evidence.
            st.tier = "NONE"
            st.tier_since = None
            st.shield_evidence = None
            st.shield_ready = False
            st.shield_signal_id = None
            st.last_signaled_tier_since = None
            st.direction = entry.get("direction")
            ts = entry.get("ts")
            if ts:
                try:
                    st.last_updated = datetime.fromisoformat(ts)
                except ValueError:
                    st.last_updated = None
            # is_live_tick stays False -- this is last-known, not live,
            # until _on_tick() actually fires for this index.
        audit.log("engine_seeded_last_known", indices=list(saved))

    def _persist_last_known_loop(self) -> None:
        """Low-frequency background snapshot -- every 20s, not on
        every tick (ticks can arrive several times a second across 5
        indices; fsync-ing that often is needless disk pressure on
        this box's documented memory/IO constraints). Purely so the
        NEXT restart has something real to seed from."""
        while not self._stop.is_set():
            time.sleep(20)
            try:
                last_known.save_all(self.states)
            except Exception as exc:                    # noqa: BLE001
                audit.log("last_known_persist_failed", error=str(exc))

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        audit.log("engine_starting", indices=list(self.states))
        # Broker's position book is ground truth for anything auto_executor
        # might already have open (a restart, a redeploy, a crash) — this
        # must resolve BEFORE the feed starts producing signals that could
        # try to open a second position on top of one we can't yet see.
        auto_executor.EXECUTOR.reconcile_on_startup()
        auto_executor.EXECUTOR.start_monitor_thread()
        self.stock_worker.start()
        self.forecaster.start()
        self.learner.start()
        threading.Thread(target=self._signal_loop, daemon=True,
                         name="signal-worker").start()
        for name, idx in self.scan_indices.items():
            self.feed.subscribe(EXCHANGE_TYPE[idx.exchange], [idx.token])
        if self.bank_tokens:
            self.feed.subscribe(EXCHANGE_TYPE['NSE'], list(self.bank_tokens))
        self.feed.start()
        threading.Thread(target=self._oi_loop, daemon=True, name="oi-poll").start()
        threading.Thread(target=self._shutdown_loop, daemon=True,
                         name="scheduler").start()
        threading.Thread(target=self._persist_last_known_loop, daemon=True,
                         name="last-known-persist").start()
        mode = "LIVE ORDERS ENABLED" if ORDER_PLACEMENT_ENABLED else "advisory mode — no orders"
        alerts.enqueue(f"🟢 KADIMA engine started ({mode}).")

    def stop(self) -> None:
        self._stop.set()
        self.forecaster.stop()
        self.learner.stop()
        self.stock_worker.stop()
        self.bank_history.stop()
        self.feed.stop()
        auto_executor.EXECUTOR.stop()
        # Explicit daily logout, not just an abandoned session. The feed
        # teardown above only closes the WebSocket; it never invalidates
        # the underlying SmartAPI login. Angel One's own compliance
        # guidance (SEBI retail algo framework, in force since Apr 2026)
        # calls for API sessions to be logged out before each new trading
        # day rather than left to expire on their own. SESSION.logout()
        # already existed in session.py but was never actually called
        # anywhere — the next morning's SESSION.ensure() would silently
        # paper over that by logging in fresh regardless, so this was a
        # correctness/hygiene gap rather than something visibly broken.
        try:
            SESSION.logout()
        except Exception as exc:                        # noqa: BLE001
            audit.log("logout_on_stop_failed", error=str(exc))
        try:
            last_known.save_all(self.states)
        except Exception as exc:                        # noqa: BLE001
            audit.log("last_known_persist_on_stop_failed", error=str(exc))
        audit.log("engine_stopped")
        alerts.enqueue("🔴 KADIMA engine stopped.")

    # ------------------------------------------------------------------ ticks
    def _on_tick(self, tick: dict) -> None:
        bank_name = self.bank_tokens.get(tick["token"])
        if bank_name:
            bank = self.bank_states[bank_name]
            bank.spot, bank.last_updated = tick['ltp'], now_ist()
            bank.builder.add_tick(tick['ts'], tick['ltp'], tick['volume'])
            return
        name = self.token_to_index.get(tick["token"])
        if not name:
            return
        st = self.states[name]
        st.spot = tick["ltp"]
        st.chain._spot_received_at = time.monotonic()
        st.last_updated = now_ist()
        st.is_live_tick = True
        self._invalidate_shield(st)
        st.builder.add_tick(tick["ts"], tick["ltp"], tick["volume"])
        # Keep the reviewed socket/feed implementation untouched. Its callback
        # supplies receipt time only; broker opening references come separately
        # from the exchange-timestamped Move Capture feed.
        from . import price_history
        price_history.capture(dict(exchange=st.chain.index.exchange, token=str(tick['token']),
            symbol=name, underlying=name, asset='INDEX', price=tick['ltp'],
            volume=tick['volume'], at=tick['ts'], received_at=tick['ts'], basis='RECEIVED'))

    # ------------------------------------------------------- signal worker
    def _queue_candle_close(self, candle: Candle) -> None:
        self._enqueue(self._on_candle_close, candle)

    def _queue_bank_close(self, candle: Candle) -> None:
        self._enqueue(self._on_bank_close, candle)

    def _enqueue(self, handler, candle: Candle) -> None:
        """Called on the tick thread: hand the closed bar off and return."""
        try:
            self._signal_q.put_nowait((handler, candle))
        except queue.Full:
            audit.log("signal_queue_full", token=candle.token,
                      interval=candle.interval, start=candle.start.isoformat())

    def _signal_loop(self) -> None:
        while not self._stop.is_set():
            try:
                handler, candle = self._signal_q.get(timeout=1.0)
            except queue.Empty:
                continue
            lag = (now_ist() - candle.start).total_seconds() - candle.interval * 60
            if lag > 30:
                audit.log("signal_worker_lag", token=candle.token,
                          interval=candle.interval, lag_sec=round(lag, 1),
                          queued=self._signal_q.qsize())
            try:
                handler(candle)
            except Exception as exc:                    # noqa: BLE001
                audit.log("candle_callback_error", token=candle.token,
                          interval=candle.interval, error=str(exc))

    def _on_bank_close(self, candle: Candle) -> None:
        if candle.interval != 5:
            return
        bank = self.bank_states[self.bank_tokens[candle.token]]
        bank.snap = indicators.compute(storage.recent_candles(candle.token, 5, limit=150),
            session_vwap=bank.builder.session_vwap(), ema_fast_p=20, ema_slow_p=50,
            ma_long_p=55, rsi_p=14)
        bank.technical_at = candle.start + timedelta(minutes=5)

    def _warm_bank_history(self):
        # Read-only history uses the existing validated candle loader. A separate
        # worker keeps its history cache/throttle independent of stock execution.
        for token, name in self.bank_tokens.items():
            bank = self.bank_states[name]
            if bank.snap.ema_slow is not None:
                continue
            clock = time.monotonic()
            if clock - self._bank_warmup_at.get(name, -60) < 60:
                continue
            self._bank_warmup_at[name] = clock
            try:
                bars = self.bank_history.history(SimpleNamespace(name=name, exchange='NSE', token=token))
                bank.snap = indicators.compute(bars[5], session_vwap=bank.builder.session_vwap(),
                    ema_fast_p=20, ema_slow_p=50, ma_long_p=55, rsi_p=14)
                bank.technical_at = datetime.fromisoformat(bars[5][-1]['start_ts']) + timedelta(minutes=5)
            except Exception as exc:
                audit.log('bank_confirmation_warmup_pending', bank=name, reason=type(exc).__name__)

    def _bank_votes(self, direction, now):
        return {name: (fresh(bank.technical_at, now, 360)
                       and fresh(bank.last_updated, now, 30)
                       and all(alignment(direction, bank.snap, bank.spot).values()))
                for name, bank in self.bank_states.items()}

    @staticmethod
    def _oi_direction(chain):
        if chain is None:
            return None
        alerts_now = chain.velocity.alerts()
        ce = {q.token for q in chain.quotes.values() if q.opt_type == 'CE'}
        pe = {q.token for q in chain.quotes.values() if q.opt_type == 'PE'}
        ce_hits = len({v['token'] for v in alerts_now if v['token'] in ce})
        pe_hits = len({v['token'] for v in alerts_now if v['token'] in pe})
        return 'CE' if ce_hits > pe_hits else 'PE' if pe_hits > ce_hits else None

    def _shield_context(self, st):
        if st.shield_evidence is None:
            return None
        now = now_ist()
        poll = getattr(st.chain, '_last_poll', 0)
        oi_at = now - timedelta(seconds=time.monotonic() - poll) if poll else None
        return replace(st.shield_evidence, snap=st.snap, spot=st.spot,
            technical_at=st.technical_at, spot_at=st.last_updated, oi_at=oi_at,
            oi_confirmed=self._oi_direction(st.chain) == st.direction,
            invalidated=st.shield_invalidated,
            banks=self._bank_votes(st.direction, now))

    def _invalidate_shield(self, st):
        with self._shield_lock:
            self._invalidate_shield_locked(st)

    def _invalidate_shield_locked(self, st):
        if st.shield_evidence and not all(alignment(st.direction, st.snap, st.spot).values()):
            st.shield_invalidated = True
            st.shield_ready = False
            if st.tier == 'CONFIRMED':
                st.tier, st.tier_since = 'ACTIVE', now_ist()
                audit.log('shield_invalidated', index=st.name, direction=st.direction)

    def _on_candle_close(self, candle: Candle) -> None:
        name = self.token_to_index.get(candle.token)
        if not name:
            return
        st = self.states[name]
        if candle.interval == 3:
            # Keep existing real-time indicator/Gamma lookbacks unchanged.
            rows = storage.recent_candles(candle.token, 3, limit=150)
            st.snap = indicators.compute(rows, session_vwap=st.builder.session_vwap(),
                ema_fast_p=EMA_FAST, ema_slow_p=EMA_SLOW,
                ma_long_p=MA_LONG, rsi_p=RSI_PERIOD)
            st.technical_at = candle.start + timedelta(minutes=3)
            st.chain._alignment_at = time.monotonic()
            self._invalidate_shield(st)
        elif candle.interval == 5:
            self._score(st, candle.start + timedelta(minutes=5), candle.close)
            self._evaluate()

    # ------------------------------------------------------------------ OI
    def _scan_cas(self, st):
        try:
            executor = auto_executor.EXECUTOR
            with executor._execution_lock:
                rows = self.cas_lane.scan(st, executor.state.positions)
                executor.cas_rows[st.name] = rows
                for row in rows:
                    key = (st.name, row['direction'])
                    signature = (row.get('episode'), row['stage'], tuple(row['blocked_reasons']))
                    if self._cas_reported.get(key) != signature:
                        self._cas_reported[key] = signature
                        audit.log('CAS_CANDIDATE', **row)
                    if row['eligible']:
                        row['dispatch'] = executor.evaluate_cas(st, self.cas_lane, row)
        except Exception as exc:
            auto_executor.EXECUTOR.cas_rows[st.name] = [dict(underlying=st.name,
                ts=now_ist().isoformat(), stage='CAS WATCH', eligible=False,
                blocked_reasons=['cas_lane_error'])]
            audit.log('CAS_LANE_ERROR', index=st.name, error_type=type(exc).__name__)

    def _scan_fast(self, st):
        """Optional lane failure must not suppress the established scanners."""
        try:
            row = self.fast_lane.scan(st)
            from . import trade_audit
            trade_audit.safe_call(trade_audit.record, row, 'SCALP', 'index_fast')
            auto_executor.EXECUTOR.fast_rows[st.name] = row
            signature = (row.get('episode'),row['stage'],tuple(row['blocked_reasons']))
            if self._fast_reported.get(st.name) != signature:
                self._fast_reported[st.name] = signature
                audit.log('FAST_CANDIDATE', **row)
            if row['eligible']:
                from .fast_momentum import signal_id
                sid = signal_id(row)
                trade_audit.safe_call(trade_audit.record, row, 'SCALP', 'index_fast', sid)
                result = auto_executor.EXECUTOR.evaluate_and_enter(
                    st.name,row['direction'],st.chain,st.spot,st.snap,row['days'],
                    'fast_momentum',sid,'EXPERIMENTAL',row['score'],
                    fast_context=lambda state=st: self.fast_lane.scan(state,record=False))
                row.update(signal_id=sid,dispatch=result)
                audit.log('FAST_DISPATCH_RESULT', signal_id=sid,index=st.name,**result)
        except Exception as exc:  # isolation boundary; never retry an order here
            auto_executor.EXECUTOR.fast_rows[st.name] = dict(underlying=st.name,
                ts=now_ist().isoformat(),stage='BLOCKED',eligible=False,
                blocked_reasons=['fast_lane_error'],scenarios=[])
            audit.log('FAST_LANE_ERROR',index=st.name,error_type=type(exc).__name__)

    def _oi_loop(self) -> None:
        from . import option_chain
        while not self._stop.is_set():
            if in_market_hours() and not killswitch.engaged():
                self._warm_bank_history()
                active = []
                for st in self.states.values():
                    from . import celestial
                    if not celestial.enabled() and st.name in GAMMA_EXPIRY_ONLY and self._dte(st) > 1.0:
                        continue  # monthly-only index -- scan only on its own expiry day
                    if st.spot:
                        try:
                            st.chain.rebuild_ladder(st.spot)
                            active.append(st)
                        except Exception as exc:        # noqa: BLE001
                            audit.log("oi_loop_error", index=st.name,
                                      error=str(exc))
                # 2026-10-02: same-exchange ladders share one quote call (<=50
                # tokens). Each batch is polled right before its indices are
                # processed, so a sample never waits behind the whole cycle.
                by_chain = {id(st.chain): st for st in active}
                for batch in option_chain.plan_batches([st.chain for st in active]):
                    try:
                        results = option_chain.poll_many(batch)
                    except Exception as exc:            # noqa: BLE001
                        audit.log("oi_loop_error", index=batch[0].index.name,
                                  error=str(exc))
                        results = {}
                    for chain in batch:
                        st = by_chain[id(chain)]
                        try:
                            self._after_poll(st, results.get(id(chain), False))
                        except Exception as exc:        # noqa: BLE001
                            audit.log("oi_loop_error", index=st.name,
                                      error=str(exc))
                    time.sleep(1)                       # spread REST load
            time.sleep(OI_POLL_SEC)

    def _after_poll(self, st: IndexState, polled: bool) -> None:
        """Everything the OI loop does with one index's fresh sample.
        Moved verbatim from _oi_loop on 2026-10-02 (batched polling)."""
        from . import celestial
        if polled:
            auto_executor.EXECUTOR.state.premium_tracker.record_chain(st.chain)
            self._scan_cas(st)
            # Separate price-led lane consumes the same successful poll.
            # Observation by default; never dispatch from the tick callback.
            self._scan_fast(st)
            # CAS blast lane: same fresh sample, own gates
            # and execution (cas_executor). Never raises.
            try:
                cas_blast_scanner.SCANNER.on_chain(st.name, st.chain, st.spot)
            except Exception as exc:  # noqa: BLE001
                audit.log("cas_scan_error", index=st.name, error=str(exc))
        if polled:
            auto_executor.EXECUTOR.update_structure(st.name, st.chain, st.spot)
            self._score_gamma(st)
            self.radar.publish_chain(st.chain,st.spot,
                st.last_updated.isoformat() if st.last_updated and st.is_live_tick else None,
                st.gamma_result,shield_score=st.score,shield_direction=st.direction,
                vwap=st.snap.vwap)
            if celestial.enabled():
                auto_executor.EXECUTOR.prepare_celestial_context(
                    st.name,st.chain,st.spot,st.snap,self._dte(st))

    # -------------------------------------------------------- gamma scanner
    def _score_gamma(self, st: IndexState) -> None:
        """Separate signal stream from Bull Shield's _score() above — same
        tier convention, different factors. Costs zero extra network calls:
        st.chain has already been polled this cycle by the caller."""
        previous = (st.gamma_result.tier, st.gamma_result.direction) if st.gamma_result else None
        result = gamma.score(st.name, st.chain, st.gamma_history, st.spot,
                             days_to_expiry=self._dte(st), snap=gamma.GammaSnapshot(
                                 above_vwap=st.snap.above_vwap, ema_bull=st.snap.ema_bull))
        st.gamma_result = result

        episode_changed = previous != (result.tier, result.direction)
        if episode_changed:
            st._gamma_entry_signal = None
            audit.log("gamma_tier_change", index=st.name, tier=result.tier,
                      score=result.score, direction=result.direction,
                      components=result.components.as_dict())

        if result.tier != "CONFIRMED":
            return  # WATCH/ACTIVE are visible via /state; only CONFIRMED alerts,
                     # matching the bar Bull Shield already uses for alerts.send_signal

        # Gamma gets its OWN, later cutoff (GAMMA_SIGNAL_CUTOFF, 15:35) —
        # not Bull Shield's SIGNAL_CUTOFF (15:10) and NOT the
        # NO_NEW_SIGNALS_AFTER_HOUR=13 hour-gate either. That gate was
        # inherited structurally when gamma alerting was first built to
        # mirror _evaluate()'s pattern, but it's documented elsewhere as
        # "your own hard rule" for Bull Shield specifically — gamma blasts
        # are a different phenomenon and the trader's own live observation
        # (2026-08-20) is that they cluster 3:15-3:35, which this old gate
        # would have silently blocked entirely. Removed here on purpose,
        # not an oversight — Bull Shield's _evaluate() below is unchanged.
        if killswitch.engaged() or not in_market_hours():
            return
        if past_cutoff(GAMMA_SIGNAL_CUTOFF):
            return

        payload = {"index": st.name, "tier": result.tier, "score": result.score,
                   "direction": result.direction, "spot": st.spot,
                   "components": result.components.as_dict()}
        sid = getattr(st, "_gamma_entry_signal", None)
        if sid is None:
            sid = storage.insert_signal(st.name, result.tier, result.score,
                                        result.direction, json.dumps(payload, default=str),
                                        kind="gamma_blast")
            audit.log("gamma_signal_emitted", signal_id=sid, **payload)
            alerts.enqueue(
                f"🌊 <b>GAMMA {result.tier}</b> — {st.name} {result.direction}\n"
                f"Score {result.score}% · Spot {st.spot}\n"
                f"<code>signal #{sid}</code>",
                key=f"{st.name}:gamma:{result.tier}")
    
            st._gamma_entry_signal = sid

        auto_executor.EXECUTOR.evaluate_and_enter(
            index_name=st.name, direction=result.direction, chain=st.chain,
            spot=st.spot, snap=st.snap, days_to_expiry=self._dte(st),
            kind="gamma_blast", source_signal_id=sid,
            tier=result.tier, score=result.score)

    # --------------------------------------------------------------- scoring
    def _score(self, st: IndexState, closed_at=None, closed_price=None) -> None:
        with self._shield_lock:
            self._score_locked(st, closed_at, closed_price)

    def _score_locked(self, st: IndexState, closed_at=None, closed_price=None) -> None:
        s = st.snap
        bull = bear = 0.0
        comp = {}

        if s.above_vwap is not None:
            if s.above_vwap:
                bull += WEIGHTS["vwap"]
            else:
                bear += WEIGHTS["vwap"]
            comp["vwap"] = "above" if s.above_vwap else "below"

        if s.ema_bull is not None:
            if s.ema_bull:
                bull += WEIGHTS["ema_cross"]
            else:
                bear += WEIGHTS["ema_cross"]
            comp["ema"] = "20>50" if s.ema_bull else "20<50"

        if s.rsi is not None:
            if s.rsi > 50:
                bull += WEIGHTS["rsi_50"]
            else:
                bear += WEIGHTS["rsi_50"]
            comp["rsi"] = round(s.rsi, 1)

        if s.avg_vol and s.vol > s.avg_vol * 1.5:
            if s.above_vwap:
                bull += WEIGHTS["volume_spike"]
            else:
                bear += WEIGHTS["volume_spike"]
            comp["volume_spike"] = True

        # OI direction shift from velocity tracker
        vel = st.chain.velocity.alerts() if st.chain else []
        if vel:
            comp["oi_velocity"] = vel
            ce_tokens = {q.token for q in st.chain.quotes.values()
                         if q.opt_type == "CE"}
            ce_hits = sum(1 for v in vel if v["token"] in ce_tokens)
            pe_hits = len(vel) - ce_hits
            # CE walls dissolving = resistance lifting = bullish
            if ce_hits > pe_hits:
                bull += WEIGHTS["oi_shift"]
            elif pe_hits > ce_hits:
                bear += WEIGHTS["oi_shift"]

        # Failed breakout/breakdown — the sixth Master Checklist v2.1
        # trigger. Was declared in WEIGHTS from day one but never actually
        # computed here until now; indicators.compute() fills these in.
        if s.failed_breakdown:
            bull += WEIGHTS["failed_break"]
            comp["failed_break"] = "breakdown_failed(bullish)"
        elif s.failed_breakout:
            bear += WEIGHTS["failed_break"]
            comp["failed_break"] = "breakout_failed(bearish)"

        previous_direction = st.direction
        st.direction = "CE" if bull >= bear else "PE"
        st.score = max(bull, bear)
        st.components = comp

        prior = st.shield_evidence
        st.shield_evidence = ShieldEvidence(st.name, st.direction, st.score, s, st.spot,
            closed_at=closed_at, prior_closed_at=prior.closed_at if prior else None,
            prior_direction=prior.direction if prior else None, prior_ready=st.shield_ready)
        context = self._shield_context(st)
        # Both the completed bar and the current quote must align.
        if not all(alignment(st.direction, s, closed_price).values()):
            st.shield_invalidated = True
            context = replace(context, invalidated=True)
        checks = confirmation_checks(context, now_ist())
        st.shield_ready = all(v for k, v in checks.items() if k != 'extra_5m_hold')
        tier = ('CONFIRMED' if is_shield_confirmed(context, now_ist()) else
                'ACTIVE' if st.score >= TIER_ACTIVE else
                'WATCH' if st.score >= TIER_WATCH else 'NONE')
        comp['confirmation'] = checks
        comp['bank_confirmations'] = context.banks
        audit.log('SHIELD_SCORE', index=st.name, score=st.score, direction=st.direction,
                  closed_at=closed_at, components=comp)
        audit.log('CONFIRMATION_GATE', index=st.name, direction=st.direction,
                  passed=tier == 'CONFIRMED', checks=checks, closed_at=closed_at)
        if tier != st.tier or previous_direction != st.direction or st.tier_since is None:
            st.tier, st.tier_since = tier, now_ist()
            st.shield_signal_id = None
            audit.log('tier_change', index=st.name, tier=tier, score=st.score,
                      direction=st.direction, components=comp)
        # A flip during this bar prevents its hold. The next bar starts fresh.
        st.shield_invalidated = False

    # ---------------------------------------------------- four-index confirm
    def _evaluate(self) -> None:
        if killswitch.engaged():
            return
        if not in_market_hours() or past_signal_cutoff():
            return
        # NO_NEW_SIGNALS_AFTER_HOUR=13 (1 PM) removed from this gate on
        # 2026-08-24. This was a separate, older discretionary rule ("your
        # own hard rule" per the original comment), predating and distinct
        # from every broker-timing constraint touched this session. It
        # directly conflicted with the trader's explicit instruction the
        # same night to keep auto-trading active through 3:40 PM -- left
        # in place, it would have silently made that extension a no-op for
        # Bull Shield specifically, since 1 PM is earlier than the new
        # 15:35 SIGNAL_CUTOFF. The constant itself stays in config.py for
        # reference; it's just no longer enforced here.

        votes_ce = sum(INDICES[n].weight for n, s in self.states.items()
                       if n in INDICES and s.direction == "CE" and s.tier in ("ACTIVE", "CONFIRMED"))
        votes_pe = sum(INDICES[n].weight for n, s in self.states.items()
                       if n in INDICES and s.direction == "PE" and s.tier in ("ACTIVE", "CONFIRMED"))
        total = sum(i.weight for i in INDICES.values())

        candidates = sorted((s for n, s in self.states.items() if n in INDICES),
                            key=lambda s: s.score, reverse=True)
        for lead in candidates:
            context = self._shield_context(lead)
            if lead.tier != 'CONFIRMED' or not is_shield_confirmed(context, now_ist()):
                continue
            agreement = (votes_ce if lead.direction == 'CE' else votes_pe) / total
            if agreement < 0.5:
                audit.log('SHIELD_DISPATCH_BLOCKED', index=lead.name,
                          reason='four_index_agreement', agreement=agreement)
                continue
            from . import celestial
            candidate_id = None
            if celestial.enabled():
                eligible = [r for r in celestial.top20() if r['eligible']
                            and r['underlying'] == lead.name and r['direction'] == lead.direction]
                if not eligible:
                    audit.log('SHIELD_DISPATCH_BLOCKED', index=lead.name,
                              reason='celestial_top20_or_controls')
                    continue
                candidate_id = eligible[0]['id']
            if lead.shield_signal_id is None:
                pick = strike_selector.best(lead.chain, lead.direction, lead.spot,
                                            days_to_expiry=self._dte(lead))
                if candidate_id:
                    pick = next((r for r in strike_selector.rank(lead.chain, lead.direction,
                        lead.spot, self._dte(lead)) if not r.rejected
                        and lead.chain.index.opt_exchange + ':' + r.quote.token == candidate_id), None)
                if not pick:
                    audit.log('SHIELD_DISPATCH_BLOCKED', index=lead.name,
                              reason='no_tradeable_strike')
                    continue
                payload = {'index': lead.name, 'tier': lead.tier, 'score': lead.score,
                    'direction': lead.direction, 'agreement': round(agreement, 2),
                    'spot': lead.spot, 'since': lead.tier_since.isoformat(),
                    'closed_at': context.closed_at.isoformat(),
                    'components': lead.components, 'strike': pick.as_dict(),
                    'pcr': lead.chain.pcr(), 'max_pain': lead.chain.max_pain()}
                sid = storage.insert_signal(lead.name, lead.tier, lead.score,
                    lead.direction, json.dumps(payload, default=str))
                from . import trade_audit
                trade_audit.safe_call(trade_audit.record_shield, payload, lead, pick, sid)
                lead.shield_signal_id = sid
                lead.last_signaled_tier_since = lead.tier_since
                audit.log('CONFIRMED_EVENT', signal_id=sid, **payload)
                alerts.send_signal(sid, payload)
            sid = lead.shield_signal_id
            if auto_executor.execution_ledger.seen(sid):
                continue
            result = auto_executor.EXECUTOR.evaluate_and_enter(
                index_name=lead.name, direction=lead.direction, chain=lead.chain,
                spot=lead.spot, snap=lead.snap, days_to_expiry=self._dte(lead),
                kind='bull_shield' if lead.direction == 'CE' else 'bear_shield',
                source_signal_id=sid, tier=lead.tier, score=lead.score,
                shield_context=lambda st=lead: self._shield_context(st), candidate_id=candidate_id)
            audit.log('SHIELD_DISPATCH_RESULT', signal_id=sid, index=lead.name, **result)

    def _dte(self, st: IndexState) -> float:
        expiry = self.scrips.nearest_expiry(st.chain.index)
        if not expiry:
            return 1.0
        try:
            d = datetime.strptime(expiry, "%d%b%Y").date()
            return max(0.5, (d - now_ist().date()).days + 0.5)
        except ValueError:
            return 1.0

    # ------------------------------------------------------------- scheduler
    def _shutdown_loop(self) -> None:
        # Reads MARKET_CLOSE from config rather than hardcoding the hour/
        # minute here a second time — this used to duplicate run.py's own
        # close-time check with an independent hardcoded (15, 30), which
        # meant a CAS-driven timing change (config.py updated 2026-08-19,
        # 15:30 -> 15:40) had to be remembered in two places. One source
        # of truth now; a future timing change only needs to happen once.
        close_hour, close_min = MARKET_CLOSE
        while not self._stop.is_set():
            time.sleep(20)
            n = now_ist()
            if n.hour > close_hour or (n.hour == close_hour and n.minute >= close_min):
                try:
                    from . import mcx_live, commodity_watch
                    keep_for_mcx = (mcx_live.live_requested()
                                    and commodity_watch.monitoring_window(n))
                except Exception:
                    keep_for_mcx = False
                if keep_for_mcx:
                    continue
                audit.log("market_close_shutdown")
                self.stop()
                return
