"""Guarded live Gap-and-Go worker.

Candidates come from the existing full-universe stock snapshots. Historical
preparation is limited to stocks already showing an opening gap. All real orders
go through the shared AutoExecutor; this module never calls Angel One order APIs.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
import json
import math
import os
import threading
import time

from . import audit, celestial, gap_and_go, gap_baseline, indicators, trade_policy
from .config import MARKET_CLOSE
from .session import SESSION
from .shield_confirmation import ShieldEvidence, confirmation_checks, is_shield_confirmed
from .stock_execution import StockExecutionWorker, parse_candles
from .stock_fast import observations, still_current
from .utils import now_ist

MAX_PREPARED_PER_CYCLE = 3
MACRO_SNAPSHOT_PATH = os.path.expanduser("~/kadima-py/state/macro_snapshot.json")


def _finite(value, minimum=None):
    ok = type(value) in (int, float) and math.isfinite(value)
    return ok and (minimum is None or value >= minimum)


def _macro_clear(now):
    try:
        if not os.path.exists(MACRO_SNAPSHOT_PATH):
            return False
        with open(MACRO_SNAPSHOT_PATH) as stream:
            payload = json.load(stream)
        stamp = datetime.fromisoformat(payload["ts"])
        if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= 900:
            return False
        today = now.date().isoformat()
        for event in payload.get("economic_calendar") or []:
            if event.get("date") <= today <= event.get("end_date"):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _shield_score(snap, spot, oi_confirmed):
    score = 0.0
    if snap.vwap and spot > snap.vwap:
        score += 25
    if snap.ema_fast and snap.ema_slow and snap.ema_fast > snap.ema_slow:
        score += 20
    if snap.rsi is not None and snap.rsi > 50:
        score += 20
    if snap.avg_vol and snap.vol > snap.avg_vol * 1.5 and snap.vwap and spot > snap.vwap:
        score += 15
    if oi_confirmed:
        score += 15
    if snap.failed_breakdown:
        score += 5
    return min(100.0, score)


def _nearest_future(master, name, on):
    rows = []
    for row in master.rows:
        try:
            if row.get("exch_seg") != "NFO" or row.get("instrumenttype") != "FUTSTK":
                continue
            if row.get("name") != name or row.get("permitted_to_trade") is False:
                continue
            expiry = datetime.strptime(row["expiry"], "%d%b%Y").date()
            if expiry < on or not row.get("token"):
                continue
            rows.append((expiry, row))
        except (KeyError, TypeError, ValueError):
            continue
    return min(rows, key=lambda item:item[0])[1] if rows else None


def _future_confirmation(master, name, now):
    contract = _nearest_future(master, name, now.date())
    if contract is None:
        return dict(futures_available=False, futures_ok=False)
    try:
        SESSION.ensure()
        response = SESSION.api.getMarketData(
            mode="FULL", exchangeTokens={"NFO":[str(contract["token"])]})
        rows = (response.get("data") or {}).get("fetched") if isinstance(response, dict) else None
        if not response or response.get("status") is not True or not isinstance(rows, list) or len(rows) != 1:
            return dict(futures_available=True, futures_ok=False)
        row = rows[0]
        ltp, opening = float(row.get("ltp")), float(row.get("open"))
        oi_change = float(row.get("netChangeOpnInterest"))
        ok = all(math.isfinite(v) for v in (ltp, opening, oi_change)) and ltp > opening and oi_change != 0
        return dict(futures_available=True, futures_ok=ok,
                    futures_symbol=contract.get("symbol"),
                    futures_oi_change=oi_change,
                    futures_price_change=ltp-opening)
    except Exception:
        return dict(futures_available=True, futures_ok=False)


class Worker:
    def __init__(self, executor, scrips):
        self.executor = executor
        self.scrips = scrips
        self.helper = StockExecutionWorker(executor, scrips)
        self.stop_event = threading.Event()
        self.thread = None
        self.history_cache = {}
        self.last_history = 0.0
        self.attempted = {}
        self.shield_state = {}
        self.wall_history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=20)))
        self.vwap_state = {}

    def _history(self, index):
        now = now_ist()
        cached = self.history_cache.get(index.name)
        if cached and 0 <= time.monotonic()-cached[0] < 15:
            return cached[1]
        if self.stop_event.wait(max(0, 1.2-(time.monotonic()-self.last_history))):
            raise ValueError("gap_worker_stopping")
        self.last_history = time.monotonic()
        SESSION.ensure()
        response = SESSION.api.getCandleData(dict(
            exchange=index.exchange, symboltoken=index.token, interval="FIVE_MINUTE",
            fromdate=(now-timedelta(days=30)).strftime("%Y-%m-%d 09:15"),
            todate=now.strftime("%Y-%m-%d %H:%M")))
        if not isinstance(response, dict) or response.get("status") is not True:
            raise ValueError("gap_history_request_failed")
        bars = parse_candles(response.get("data"), 5, now)
        self.history_cache[index.name] = (time.monotonic(), bars)
        if len(self.history_cache) > 32:
            oldest = min(self.history_cache, key=lambda key:self.history_cache[key][0])
            self.history_cache.pop(oldest, None)
        return bars

    def _wall_metrics(self, data, now):
        name, spot = data["index"]["name"], data["spot"]
        nearest = None
        for q in data.get("quotes") or []:
            if q.get("opt_type") != "CE":
                continue
            strike, oi, token = q.get("strike"), q.get("oi"), str(q.get("token"))
            if not _finite(strike, 0.000001) or not _finite(oi, 1) or not token:
                continue
            distance = strike-spot
            if 0 < distance <= 50 and (nearest is None or distance < nearest[0]):
                nearest = (distance, token, float(oi))
        if nearest is None:
            return None, None
        distance, token, oi = nearest
        window = self.wall_history[name][token]
        window.append((now, oi))
        while len(window) > 1 and (now-window[0][0]).total_seconds() > 300:
            window.popleft()
        baseline = next((old for ts,old in window if (now-ts).total_seconds() >= 60), None)
        return (oi/baseline if baseline else None), distance

    def _shield(self, name, data, bars, now):
        today = [b for b in bars if datetime.fromisoformat(b["start_ts"]).date() == now.date()]
        if len(today) < 2:
            return False, 0.0, None
        snap = indicators.compute(
            bars, session_vwap=data.get("session_vwap"),
            ema_fast_p=20, ema_slow_p=50, ma_long_p=55, rsi_p=14)
        gamma = data.get("gamma") or {}
        oi_confirmed = gamma.get("oi_direction") == "CE"
        score = _shield_score(snap, data["spot"], oi_confirmed)
        closed_at = datetime.fromisoformat(today[-1]["start_ts"]) + timedelta(minutes=5)
        state = self.shield_state.get(name) or {}
        evidence = ShieldEvidence(
            name, "CE", score, snap, data["spot"], closed_at=closed_at,
            technical_at=closed_at, spot_at=datetime.fromisoformat(data["spot_at"]),
            prior_closed_at=state.get("closed_at"), prior_direction=state.get("direction"),
            prior_ready=state.get("ready", False), oi_confirmed=oi_confirmed,
            oi_at=datetime.fromisoformat(data["chain_at"]), invalidated=False)
        checks = confirmation_checks(evidence, now)
        ready = all(value for key,value in checks.items() if key != "extra_5m_hold")
        if state.get("closed_at") != closed_at:
            self.shield_state[name] = dict(closed_at=closed_at, direction="CE", ready=ready)
        return is_shield_confirmed(evidence, now), score, snap

    def _prepare(self, data):
        now = now_ist()
        name = data["index"]["name"]
        chain = self.helper.restore_chain(data)
        bars = self._history(chain.index)
        if not still_current(data):
            raise ValueError("gap_snapshot_changed_during_history")
        today = [b for b in bars if datetime.fromisoformat(b["start_ts"]).date() == now.date()]
        if len(today) < 4:
            raise ValueError("gap_candle_warmup")
        baseline = gap_baseline.same_time_volume_baseline(
            bars, now.replace(hour=9, minute=15, second=0, microsecond=0), slots=2)
        previous = data.get("previous_close") or gap_baseline.previous_close(bars, now.date())
        shield_ok, shield_score, snap = self._shield(name, data, bars, now)
        gamma = data.get("gamma") or {}
        gamma_ok = (gamma.get("tier") == "CONFIRMED" and gamma.get("direction") == "CE"
                    and gamma.get("oi_direction") == "CE"
                    and _finite(gamma.get("score"), 70))
        news = celestial.news_impact(name, "CE", now)
        wall_ratio, wall_distance = self._wall_metrics(data, now)
        prior_vwap = self.vwap_state.get(name)
        if not _finite(prior_vwap, .000001):
            raise ValueError("gap_vwap_warmup")
        confirmations = dict(
            bull_shield=shield_ok, gamma_oi=gamma_ok,
            futures_available=False, futures_ok=False,
            news_clear=bool(news.get("news_available") and not news.get("news_blocked")),
            macro_clear=_macro_clear(now), ce_wall_ratio=wall_ratio,
            ce_wall_distance=wall_distance, next_resistance=None)
        row = gap_and_go.evaluate(
            name, today, previous_close=previous,
            session_vwap=data.get("session_vwap"),
            prior_vwap=prior_vwap,
            baseline_volume=baseline, confirmations=confirmations, now=now)
        if row.get("stage") == "CONFIRMED" and row.get("eligible"):
            confirmations.update(_future_confirmation(self.scrips, name, now))
            row = gap_and_go.evaluate(
                name, today, previous_close=previous,
                session_vwap=data.get("session_vwap"),
                prior_vwap=data.get("session_vwap"),
                baseline_volume=baseline, confirmations=confirmations, now=now)
        if not row.get("eligible"):
            return row, chain, snap
        picks = gap_and_go.ranked_atm_or_itm_calls(
            chain, data["spot"],
            (datetime.strptime(chain._expiry, "%d%b%Y").replace(
                tzinfo=now.tzinfo, hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1])-now).total_seconds()/86400)
        if not picks:
            row.update(eligible=False)
            row["blocked_reasons"].append("gap_no_liquid_atm_or_itm_call")
            return row, chain, snap
        q = picks[0].quote
        days = (datetime.strptime(chain._expiry, "%d%b%Y").replace(
            tzinfo=now.tzinfo, hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1])-now).total_seconds()/86400
        stop = trade_policy.premium_stop(
            data["spot"], row["metrics"]["stop"], q.strike, q.ltp, days, "CE")
        if not _finite(stop, 0.000001) or stop >= q.ask:
            row.update(eligible=False)
            row["blocked_reasons"].append("gap_option_stop_unavailable")
            return row, chain, snap
        row.update(direction="CE", underlying=name, token=q.token, contract_symbol=q.symbol,
                   exchange=chain.index.opt_exchange, strike=q.strike, entry=q.ask,
                    stop=stop, ltp=q.ltp, quote_at=data['chain_at'],
                    expiry=getattr(chain, '_expiry', ''),
                   spot_stop=row["metrics"]["stop"], days=days,
                   score=round((shield_score + float(gamma.get("score", 0)))/2, 2),
                   episode="gap:"+name+":"+today[0]["start_ts"]+":"+chain._expiry)
        return row, chain, snap

    def process(self, data):
        name = data["index"]["name"]
        row, chain, snap = self._prepare(data)
        from . import trade_audit
        trade_audit.safe_call(trade_audit.record, row, 'GAP_AND_GO', 'gap_and_go')
        if not row.get("eligible"):
            self.executor.fast_rows["GAP:"+name] = row
            audit.log("GAP_LIVE_CANDIDATE", **row)
            return row
        sid = gap_and_go.signal_id(row)
        row["signal_id"] = sid
        trade_audit.safe_call(trade_audit.record, row, "GAP_AND_GO", "gap_and_go", sid)
        snapshot_at = data["chain_at"]
        def revalidate():
            if self.stop_event.is_set() or not still_current(data):
                return None
            latest, _, _ = self._prepare(data)
            if latest.get("episode") != row.get("episode"):
                return None
            return latest
        chain._entry_guard = lambda: (
            not self.stop_event.is_set() and still_current(data)
            and data["chain_at"] == snapshot_at)
        result = self.executor.evaluate_and_enter(
            name, "CE", chain, data["spot"], snap, row["days"],
            "gap_and_go", sid, "CONFIRMED", row["score"], gap_context=revalidate)
        row["dispatch"] = result
        self.executor.fast_rows["GAP:"+name] = row
        audit.log("GAP_LIVE_DISPATCH_RESULT", signal_id=sid, index=name, **result)
        return row

    def protect(self, data_rows):
        latest = {d["index"]["name"]:d for d in data_rows}
        with self.executor._lock:
            positions = list(self.executor.state.positions.values())
        for pos in positions:
            if pos.kind != "gap_and_go" or pos.status not in ("open","entry_pending"):
                continue
            data = latest.get(pos.index)
            if not data:
                continue
            if data["spot"] <= (pos.swing_level or 0):
                self.executor.manual_exit(pos.signal_id, "gap_underlying_structure_failed")
            elif _finite(data.get("session_vwap"), 0.000001) and data["spot"] < data["session_vwap"]:
                self.executor.manual_exit(pos.signal_id, "gap_underlying_lost_vwap")

    def cycle(self):
        now = now_ist()
        rows = observations(now)
        self.protect(rows)
        if not gap_and_go.live_requested():
            celestial.health("gap_and_go_live", dict(ok=True, live_requested=False,
                mode="DISABLED", candidates=0, one_lot_max=True))
            return
        candidates = []
        for data in rows:
            opening, previous = data.get("day_open"), data.get("previous_close")
            if _finite(opening, .000001) and _finite(previous, .000001) and opening >= previous*1.01:
                candidates.append(data)
        candidates.sort(key=lambda d:d["day_open"]/d["previous_close"], reverse=True)
        prepared = 0
        for data in candidates:
            if self.stop_event.is_set() or prepared >= MAX_PREPARED_PER_CYCLE:
                break
            key = data["index"]["name"]
            if self.attempted.get(key) == data["chain_at"]:
                continue
            self.attempted[key] = data["chain_at"]
            prepared += 1
            try:
                self.process(data)
            except Exception as exc:
                audit.log("GAP_LIVE_PREPARATION_REJECTED", index=key,
                          error_type=type(exc).__name__)
            finally:
                vwap = data.get("session_vwap")
                if _finite(vwap, .000001):
                    self.vwap_state[key] = vwap
        celestial.health("gap_and_go_live", dict(ok=True, live_requested=True,
            mode="GUARDED_LIVE", candidates=len(candidates), prepared=prepared,
            one_lot_max=True, scan_window="09:15-10:30_IST"))

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.cycle()
            except Exception as exc:
                audit.log("GAP_LIVE_WORKER_ERROR", error_type=type(exc).__name__)
            self.stop_event.wait(2)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True,
                                           name="gap-and-go-live")
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
