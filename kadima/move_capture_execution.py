"""Engine-side Move Capture option confirmation and guarded live dispatch.

The independent spot service never places orders. This worker runs inside the
main KADIMA process, reads fresh spot impulses from move_capture_handoff,
confirms a liquid option using two fresh REST samples, then reuses the existing
serialized fast-momentum AutoExecutor path. All existing kill, news,
reconciliation, capital, daily-loss, lot-size, session and broker gates remain
authoritative.
"""
from __future__ import annotations

from datetime import datetime
import math
import os
import threading
import time

from . import audit, celestial, config, fast_momentum, indicators, stock_fast
from . import move_capture_handoff as handoff
from . import strike_selector
from .celestial_universe import active_contracts
from .option_chain import OptionChain
from .utils import in_market_hours, now_ist

CONFIRM_MIN_AGE_SEC = 2.5
CONFIRM_MAX_AGE_SEC = 12.0
MIN_PREMIUM_MOVE_PCT = 0.25
MAX_PREPARES_PER_CYCLE = 2
RETRY_SECONDS = 3.0


def live_requested():
    return os.environ.get("KADIMA_MOVE_CAPTURE_LIVE", "0") == "1"


def _finite(value, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    return minimum is None or value >= minimum


class MoveCaptureExecutionWorker:
    def __init__(self, executor, scrips):
        self.executor = executor
        self.scrips = scrips
        self.stop_event = threading.Event()
        self.thread = None
        self.first_samples = {}
        self.last_attempt = {}
        self.rows = {}
        self._stock_map = None
        self.last_served = {}
        self.stage = 'idle'
        self.last_public = {}
        from .momentum_quote_watch import QuoteWatch
        self.quote_watch = QuoteWatch()

    def _stocks(self):
        if self._stock_map is None:
            contracts = active_contracts(self.scrips.rows, now_ist().date())
            names = {
                row["name"] for row in contracts.values()
                if row.get("instrumenttype") == "OPTSTK"
                and row.get("exch_seg") == "NFO"
            }
            spots = {}
            for row in self.scrips.rows:
                if (row.get("exch_seg") == "NSE"
                        and row.get("instrumenttype", "") == ""
                        and row.get("symbol", "").endswith("-EQ")
                        and row.get("token")):
                    spots.setdefault(row.get("name"), set()).add(str(row["token"]))
            self._stock_map = {}
            for name in names:
                tokens = spots.get(name, set())
                if len(tokens) == 1:
                    self._stock_map[name] = dict(
                        name=name, spot_exchange="NSE",
                        spot_token=next(iter(tokens)), opt_exchange="NFO")
        return self._stock_map

    def _index(self, name):
        stock = self._stocks().get(name)
        if stock is None:
            raise ValueError("move_capture_stock_mapping_missing")
        step = self.scrips.infer_strike_step(name, stock["opt_exchange"])
        if not step:
            raise ValueError("move_capture_strike_step_missing")
        return config.Index(
            name=name,
            exchange=stock["spot_exchange"],
            token=str(stock["spot_token"]),
            opt_exchange=stock["opt_exchange"],
            opt_symbol_root=name,
            strike_step=step,
            weight=0,
        )

    def _chain(self, data):
        now = now_ist()
        observed = datetime.fromisoformat(data["observed_at"])
        age = (now - observed).total_seconds()
        if observed.tzinfo is None or not 0 <= age <= handoff.FRESH_SECONDS:
            raise ValueError("move_capture_spot_stale")
        spot = float(data["price"])
        if not _finite(spot, .000001):
            raise ValueError("move_capture_spot_invalid")
        index = self._index(data["underlying"])
        from . import stock_blast   # STOCK BLAST v1: wider ladder, stock strike rules
        chain = OptionChain(index, self.scrips, strikes_each_side=(
            stock_blast.ladder_width(data.get("score", 0)) if stock_blast.enabled() else 2))
        chain.rebuild_ladder(spot)
        if len(chain.quotes) < 2 or not chain.poll():
            raise ValueError("move_capture_option_poll_incomplete")
        chain._spot_received_at = time.monotonic() - age
        expiry = datetime.strptime(chain._expiry, "%d%b%Y").replace(
            tzinfo=now.tzinfo, hour=config.MARKET_CLOSE[0], minute=config.MARKET_CLOSE[1])
        days = (expiry - now).total_seconds() / 86400
        if not _finite(days, .000001):
            raise ValueError("move_capture_expiry_invalid")
        if stock_blast.enabled():
            all_ranked = stock_blast.rank(chain, data["direction"], spot, days,
                                          data.get("score", 0))
        else:
            all_ranked = strike_selector.rank(chain, data["direction"], spot, days)
        ranked = [r for r in all_ranked if not r.rejected]
        if not ranked:
            stock_blast.log_rejects(data["underlying"], data["direction"],
                                    data.get("score"), all_ranked)
            raise ValueError("move_capture_no_liquid_option")
        # Keep the contract through the short confirmation window when it still
        # passes ranking filters. An invalid contract is never pinned for entry.
        previous = self.first_samples.get(data['underlying'])
        chosen = next((r for r in ranked if previous and
                       previous.get('episode') == data['episode'] and
                       str(r.quote.token) == previous['token']), ranked[0])
        return chain, chosen.quote, days

    @staticmethod
    def _sample(q):
        if not all(_finite(v, .000001) for v in (q.ltp, q.bid, q.ask)):
            raise ValueError("move_capture_option_book_invalid")
        if q.ask < q.bid or not _finite(q.volume, 0):
            raise ValueError("move_capture_option_book_invalid")
        return dict(
            at=time.monotonic(), token=str(q.token), symbol=q.symbol,
            ltp=float(q.ltp), bid=float(q.bid), ask=float(q.ask),
            volume=int(q.volume or 0),
        )

    def _confirm(self, data, q):
        key = data["underlying"]
        current = self._sample(q)
        previous = self.first_samples.get(key)
        if (previous is None or previous.get("episode") != data["episode"]
                or previous["token"] != current["token"]):
            self.first_samples[key] = current | {"episode": data["episode"]}
            return None, "move_capture_option_confirmation_warmup"

        age = current["at"] - previous["at"]
        if age < CONFIRM_MIN_AGE_SEC:
            return None, "move_capture_option_confirmation_warmup"
        if age > CONFIRM_MAX_AGE_SEC:
            self.first_samples[key] = current | {"episode": data["episode"]}
            return None, "move_capture_option_confirmation_reset"

        premium_move = (current["ltp"] / previous["ltp"] - 1) * 100
        volume_delta = current["volume"] - previous["volume"]
        book_not_falling = (
            current["bid"] >= previous["bid"] * 0.995
            and current["ask"] >= previous["ask"] * 0.995
        )
        self.first_samples[key] = current | {"episode": data["episode"]}
        if premium_move < MIN_PREMIUM_MOVE_PCT:
            return None, "move_capture_option_premium_not_confirmed"
        if volume_delta <= 0:
            return None, "move_capture_option_volume_not_confirmed"
        if not book_not_falling:
            return None, "move_capture_option_book_not_confirmed"
        return dict(
            premium_move_pct=round(premium_move, 3),
            option_volume_delta=volume_delta,
            option_confirm_seconds=round(age, 3),
        ), None

    def _context(self, data, q, days, confirmation):
        stop = data.get("spot_stop")
        spot = data.get("price")
        if not _finite(stop, .000001) or not _finite(spot, .000001):
            raise ValueError("move_capture_spot_stop_missing")
        if data["direction"] == "CE" and not stop < spot:
            raise ValueError("move_capture_spot_stop_invalid")
        if data["direction"] == "PE" and not stop > spot:
            raise ValueError("move_capture_spot_stop_invalid")
        if not _finite(q.ask, .000001):
            raise ValueError("move_capture_entry_price_missing")
        return dict(
            underlying=data["underlying"],
            strategy="fast_momentum",       # existing executor live contract
            source_strategy="move_capture",
            asset_scope="STOCK",
            direction=data["direction"],
            score=float(data["score"]),
            ts=now_ist().isoformat(),
            episode=data["episode"],
            token=str(q.token),
            symbol=q.symbol,
            exchange="NFO",
            entry=float(q.ask),
            spot_stop=float(stop),
            days=float(days),
            eligible=True,
            option_confirmation=confirmation,
            lottery=bool(stock_blast.enabled() and stock_blast.is_lottery(q, float(spot))),
        )

    def process(self, row):
        data = row["data"]
        name = row["name"]
        now = now_ist()
        self.stage = 'live_controls'
        if not live_requested():
            self.rows[name] = dict(
                underlying=name, stage="LIVE_DISABLED", eligible=False,
                ts=now.isoformat(), blocked_reasons=["move_capture_live_disabled"])
            return
        try:
            stock_controls = stock_fast.controls()
        except Exception:
            self.rows[name] = dict(
                underlying=name, stage="BLOCKED", eligible=False,
                ts=now.isoformat(), blocked_reasons=["stock_live_control_unavailable"])
            return
        if stock_controls.get("live_enabled") is not True:
            self.rows[name] = dict(
                underlying=name, stage="LIVE_DISABLED", eligible=False,
                ts=now.isoformat(), blocked_reasons=["stock_live_control_disabled"])
            return
        from . import stock_blast   # STOCK BLAST v1: no holding-horizon bar, entries to 15:10
        if stock_blast.enabled():
            if not stock_blast.entry_time_allowed(now):
                raise ValueError('move_capture_outside_entry_window')
        elif not stock_fast.entry_time_allowed(now, stock_controls['holding_minutes']):
            raise ValueError('move_capture_horizon_crosses_session_exit')
        if data.get("asset_scope") != "STOCK":
            return
        if (data.get("underlying") != name or data.get("direction") not in ("CE", "PE")
                or not isinstance(data.get("episode"), str)
                or not handoff.fresh(row.get("updated_at"), now=now)):
            raise ValueError("move_capture_handoff_invalid")
        score = data.get("score")
        if not _finite(score, 0) or score < 0 or score > 100:
            raise ValueError("move_capture_score_invalid")

        last = self.last_attempt.get(data["episode"], 0.0)
        if 0 <= time.monotonic() - last < RETRY_SECONDS:
            return

        first = self.first_samples.get(name)
        if (first and first.get("episode") == data["episode"]
                and 0 <= time.monotonic() - first["at"] < CONFIRM_MIN_AGE_SEC):
            return

        self.stage = 'option_selection'
        chain, q, days = self._chain(data)
        try:
            self.quote_watch.pin(name, q)
        except Exception as exc:
            audit.log('MOMENTUM_QUOTE_WATCH_ERROR', index=name, error_type=type(exc).__name__)
        self.stage = 'option_confirmation'
        confirmation, reason = self._confirm(data, q)
        if confirmation is None:
            self.rows[name] = dict(
                underlying=name, stage="OPTION_CONFIRMATION", eligible=False,
                direction=data["direction"], score=score, symbol=q.symbol,
                ts=now.isoformat(), blocked_reasons=[reason])
            return

        context = self._context(data, q, days, confirmation)
        block = stock_blast.entry_filter(name, data, q, now)   # STOCK BLAST v1.2 loser filters
        if block:
            self.rows[name] = dict(context, stage="BLOCKED", eligible=False, blocked_reasons=[block])
            return
        from . import trade_audit
        trade_audit.safe_call(trade_audit.record_move, context, q, chain, stock_controls['holding_minutes'])
        self.stage = 'entry_gates'
        # Existing fast live gate adds rollout, auto-trade, kill, score and
        # symbol-specific fresh-news checks before the executor can submit.
        blockers = fast_momentum.live_blocks(
            name, data["direction"], now_ist(), score)
        if blockers:
            context["eligible"] = False
            self.rows[name] = dict(
                context, stage="BLOCKED", blocked_reasons=blockers)
            return

        sid = handoff.signal_id(data)
        trade_audit.safe_call(trade_audit.record_move, context, q, chain, stock_controls['holding_minutes'], sid)
        # Persist the selected 5/10/15 minute deadline before any order intent.
        # The existing stock worker protects these fast_momentum positions.
        c = stock_fast._connection()
        with c:
            c.execute('INSERT OR IGNORE INTO stock_fast_policies VALUES (?,?,?,?)',
                      (sid, data['episode'], stock_controls['holding_minutes'],
                       stock_fast.session_exit(now).isoformat()))
        episode = data["episode"]
        direction = data["direction"]
        initial = dict(context)

        def make_revalidate(initial):
          def revalidate():
            latest = handoff.current(name)
            try:
                current_controls = stock_fast.controls()
                stock_live = (current_controls.get("live_enabled") is True
                              and current_controls == stock_controls)
            except Exception:
                stock_live = False
            if (not live_requested() or not stock_live or latest is None
                    or latest["episode"] != episode
                    or latest["direction"] != direction):
                return None
            latest_data = latest["data"]
            if latest_data.get("asset_scope") != "STOCK":
                return None
            price = latest_data.get('price')
            sign = 1 if direction == 'CE' else -1
            if (not _finite(price, .000001) or sign*(price-initial['spot_stop']) <= 0
                    or abs(price/data['price']-1) > .005
                    or not (stock_blast.entry_time_allowed(now_ist()) if stock_blast.enabled()
                            else stock_fast.entry_time_allowed(now_ist(), stock_controls['holding_minutes']))):
                return None
            revised = dict(initial)
            revised["ts"] = latest["updated_at"]
            revised["score"] = float(latest_data.get("score", 0))
            revised["eligible"] = not fast_momentum.live_blocks(
                name, direction, now_ist(), revised["score"])
            return revised if revised["eligible"] else None
          return revalidate

        revalidate = make_revalidate(initial)
        chain._entry_guard = lambda: (
            live_requested() and handoff.still_current(name, episode, direction))
        self.last_attempt[episode] = time.monotonic()
        self.stage = 'executor_dispatch'
        result = self.executor.evaluate_and_enter(
            name, direction, chain, float(data["price"]), indicators.Snapshot(),
            days, "fast_momentum", sid, "EXPERIMENTAL", float(score),
            fast_context=revalidate)
        self.rows[name] = dict(
            context, signal_id=sid, stage="DISPATCHED",
            blocked_reasons=[] if result.get("ok") else [result.get("reason")],
            dispatch=result)
        audit.log(
            "MOVE_CAPTURE_DISPATCH_RESULT", signal_id=sid, index=name,
            direction=direction, score=score, **result)
        if result.get("ok"):
            stock_blast.note_trade(name, now)   # STOCK BLAST v1.2: per-stock daily cap
        if stock_blast.enabled() and score >= stock_blast.LOTTERY_SCORE and not context.get("lottery"):
            try:
                self._lottery_leg(name, data, chain, days, now, context, stock_controls,
                                  make_revalidate, confirmation)
            except Exception as exc:  # noqa: BLE001 - the primary leg is already done
                audit.log("STOCK_BLAST_LOTTERY_ERROR", index=name,
                          error=type(exc).__name__, detail=str(exc)[:120])

    def _lottery_leg(self, name, data, chain, days, now, primary, stock_controls,
                     make_revalidate, confirmation):
        """STOCK BLAST v1.1: on a very strong trigger, a second small order on the best far
        strike (> 8% out or under Rs 2), sized at stock_blast.LOTTERY_SIZE. Own signal id,
        same executor gates."""
        from . import stock_blast
        spot, direction = float(data["price"]), data["direction"]
        picks = [r for r in stock_blast.rank(chain, direction, spot, days, data.get("score", 0))
                 if not r.rejected and 'lottery' in r.reasons
                 and str(r.quote.token) != primary.get("token")]
        if not picks:
            audit.log("STOCK_BLAST_LOTTERY_NONE", index=name, direction=direction)
            return
        picks.sort(key=lambda r: stock_blast.leverage(r.quote, spot), reverse=True)
        q = picks[0].quote
        leg = dict(data, episode=data["episode"] + ":lottery")
        context = self._context(leg, q, days, confirmation)
        context["lottery"] = True
        sid = handoff.signal_id(leg)
        c = stock_fast._connection()
        with c:
            c.execute('INSERT OR IGNORE INTO stock_fast_policies VALUES (?,?,?,?)',
                      (sid, leg['episode'], stock_controls['holding_minutes'],
                       stock_fast.session_exit(now).isoformat()))
        base = make_revalidate(dict(context))

        def revalidate():
            out = base()
            if out is not None:
                out["episode"], out["lottery"] = leg["episode"], True
            return out
        result = self.executor.evaluate_and_enter(
            name, direction, chain, spot, indicators.Snapshot(),
            days, "fast_momentum", sid, "EXPERIMENTAL", float(data["score"]),
            fast_context=revalidate)
        audit.log("STOCK_BLAST_LOTTERY_DISPATCH", signal_id=sid, index=name, symbol=q.symbol,
                  ask=q.ask, direction=direction, score=data["score"], **result)

    def cycle(self):
        now = now_ist()
        if not in_market_hours():
            celestial.health("move_capture_execution", dict(
                ok=True, live_requested=live_requested(), market_hours=False,
                candidates=0, prepared=0))
            return
        rows = handoff.candidates(now)
        names = {r['name'] for r in rows}
        episodes = {r['episode'] for r in rows}
        self.first_samples = {k:v for k,v in self.first_samples.items() if k in names}
        self.last_attempt = {k:v for k,v in self.last_attempt.items() if k in episodes}
        self.last_served = {k:v for k,v in self.last_served.items() if k in names}
        # STOCK BLAST v1.1 (2026-10-27): triggers scoring >= 80 are served before the rotation.
        from . import focus_list   # FOCUS LIST v1: focus names first, then score >= 80
        rows.sort(key=lambda r: (0 if focus_list.is_focus(r['name']) else 1,
                                 0 if r['data'].get('score', 0) >= 80 else 1,
                                 self.last_served.get(r['name'], 0), -r['data']['score']))
        prepared = 0
        failed = 0
        for row in rows:
            if self.stop_event.is_set() or prepared >= MAX_PREPARES_PER_CYCLE:
                break
            try:
                first = self.first_samples.get(row['name'])
                if first and first.get('episode') == row['episode'] and time.monotonic()-first['at'] < CONFIRM_MIN_AGE_SEC:
                    continue  # Warmup does not consume another candidate's slot.
                self.last_served[row['name']] = time.monotonic()
                self.process(row)
                prepared += 1
            except Exception as exc:
                from .preparation_diagnostics import describe
                failed += 1
                prepared += 1
                reason = stock_fast._public_reason(exc, 'move_capture_preparation_error')
                diagnostic = describe(exc, self.stage, reason)
                self.rows[row["name"]] = dict(
                    underlying=row["name"], stage="BLOCKED", eligible=False,
                    ts=now_ist().isoformat(), blocked_reasons=[reason], **diagnostic)
                audit.log(
                    "MOVE_CAPTURE_PREPARATION_REJECTED",
                    index=row["name"], **diagnostic)
            current = self.rows.get(row['name'])
            if current:
                current['quote_at'] = row['data'].get('observed_at')
                self.executor.fast_rows['move_capture:'+row['name']] = dict(
                    current, source_strategy='move_capture', asset_scope='STOCK')
                public = (current.get('stage'), tuple(current.get('blocked_reasons', [])))
                if self.last_public.get(row['name']) != public:
                    audit.log('MOVE_CAPTURE_CANDIDATE_STATE', index=row['name'],
                        episode=row['episode'], stage=current.get('stage'),
                        blocked_reasons=current.get('blocked_reasons', []),
                        symbol=current.get('symbol'), direction=row['direction'])
                    self.last_public[row['name']] = public
        for name in list(self.rows):
            if name not in names:
                self.rows.pop(name, None)
                self.last_public.pop(name, None)
                self.executor.fast_rows.pop('move_capture:'+name, None)
        try:
            self.quote_watch.cycle()
        except Exception as exc:
            audit.log('MOMENTUM_QUOTE_WATCH_ERROR', error_type=type(exc).__name__)
        celestial.health("move_capture_execution", dict(
            ok=failed == 0, preparation_failures=failed, live_requested=live_requested(), market_hours=True,
            candidates=len(rows), prepared=prepared,
            option_confirm_min_seconds=CONFIRM_MIN_AGE_SEC,
            execution="shared_guarded_fast_executor"))

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.cycle()
            except Exception as exc:
                audit.log("MOVE_CAPTURE_WORKER_ERROR", error_type=type(exc).__name__)
                celestial.health("move_capture_execution", dict(
                    ok=False, reason="worker_error", live_requested=live_requested()))
            self.stop_event.wait(.5)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(
                target=self.run, daemon=True, name="move-capture-execution")
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
