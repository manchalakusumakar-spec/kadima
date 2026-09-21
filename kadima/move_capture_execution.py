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

from . import audit, celestial, config, fast_momentum, indicators
from . import move_capture_handoff as handoff
from . import strike_selector
from .instrument_master import stock_universe
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

    def _stocks(self):
        if self._stock_map is None:
            self._stock_map = {
                s["name"]: s for s in stock_universe()
                if s.get("opt_exchange") == "NFO"
            }
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
        chain = OptionChain(index, self.scrips, strikes_each_side=2)
        chain.rebuild_ladder(spot)
        if len(chain.quotes) < 2 or not chain.poll():
            raise ValueError("move_capture_option_poll_incomplete")
        chain._spot_received_at = time.monotonic() - age
        expiry = datetime.strptime(chain._expiry, "%d%b%Y").replace(
            tzinfo=now.tzinfo, hour=config.MARKET_CLOSE[0], minute=config.MARKET_CLOSE[1])
        days = (expiry - now).total_seconds() / 86400
        if not _finite(days, .000001):
            raise ValueError("move_capture_expiry_invalid")
        ranked = [r for r in strike_selector.rank(
            chain, data["direction"], spot, days) if not r.rejected]
        if not ranked:
            raise ValueError("move_capture_no_liquid_option")
        return chain, ranked[0].quote, days

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
        )

    def process(self, row):
        data = row["data"]
        name = row["name"]
        now = now_ist()
        if not live_requested():
            self.rows[name] = dict(
                underlying=name, stage="LIVE_DISABLED", eligible=False,
                ts=now.isoformat(), blocked_reasons=["move_capture_live_disabled"])
            return
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

        chain, q, days = self._chain(data)
        confirmation, reason = self._confirm(data, q)
        if confirmation is None:
            self.rows[name] = dict(
                underlying=name, stage="OPTION_CONFIRMATION", eligible=False,
                direction=data["direction"], score=score, symbol=q.symbol,
                ts=now.isoformat(), blocked_reasons=[reason])
            return

        context = self._context(data, q, days, confirmation)
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
        episode = data["episode"]
        direction = data["direction"]
        initial = dict(context)

        def revalidate():
            latest = handoff.current(name)
            if (not live_requested() or latest is None
                    or latest["episode"] != episode
                    or latest["direction"] != direction):
                return None
            latest_data = latest["data"]
            if latest_data.get("asset_scope") != "STOCK":
                return None
            revised = dict(initial)
            revised["ts"] = latest["updated_at"]
            revised["score"] = float(latest_data.get("score", 0))
            revised["eligible"] = not fast_momentum.live_blocks(
                name, direction, now_ist(), revised["score"])
            return revised if revised["eligible"] else None

        chain._entry_guard = lambda: (
            live_requested() and handoff.still_current(name, episode, direction))
        self.last_attempt[episode] = time.monotonic()
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

    def cycle(self):
        now = now_ist()
        if not in_market_hours():
            celestial.health("move_capture_execution", dict(
                ok=True, live_requested=live_requested(), market_hours=False,
                candidates=0, prepared=0))
            return
        rows = handoff.candidates(now)
        prepared = 0
        for row in rows:
            if self.stop_event.is_set() or prepared >= MAX_PREPARES_PER_CYCLE:
                break
            try:
                self.process(row)
                prepared += 1
            except Exception as exc:
                reason = str(exc) if type(exc) is ValueError else "move_capture_preparation_error"
                self.rows[row["name"]] = dict(
                    underlying=row["name"], stage="BLOCKED", eligible=False,
                    ts=now_ist().isoformat(), blocked_reasons=[reason])
                audit.log(
                    "MOVE_CAPTURE_PREPARATION_REJECTED",
                    index=row["name"], reason=reason)
        celestial.health("move_capture_execution", dict(
            ok=True, live_requested=live_requested(), market_hours=True,
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
