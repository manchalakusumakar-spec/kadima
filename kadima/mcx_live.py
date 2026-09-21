"""Guarded live MCX long-option momentum lane.

Uses fresh MCX futures price/volume/OI to establish direction, then requires the
corresponding near-ATM commodity option to show its own five-minute premium,
volume and OI confirmation. Orders are long CE/PE only and use the existing
execution ledger. No futures short-selling path is introduced.

Live activation requires KADIMA_MCX_LIVE=1, global auto enabled, a positive
KADIMA_MCX_CAPITAL_INR cap, healthy broker reconciliation, one-lot affordability,
and the ordinary KADIMA kill/daily-loss controls.
"""
from __future__ import annotations

from datetime import datetime, time
import json
import math
import os
import threading

from . import (audit, capital_guard, celestial, commodity_watch, config,
               execution_ledger, exit_manager, killswitch, storage, trade_policy)
from .auto_executor import Position
from .celestial_scanner import PremiumWindow
from .rate_limiter import QUOTE
from .session import SESSION
from .utils import now_ist

ROOTS = ("CRUDEOIL", "GOLD", "NATURALGAS", "SILVER", "COPPER")
MIN_FUTURE_MOVE_PCT = 0.35
MIN_OPTION_MOVE_PCT = 5.0
MAX_FUTURE_SPREAD_PCT = 0.75
MAX_OPTION_SPREAD_PCT = 1.5
SESSION_EXIT = time(23, 25)
POLL_SECONDS = 10
MACRO_SNAPSHOT_PATH = os.path.expanduser("~/kadima-py/state/macro_snapshot.json")


def live_requested():
    return os.environ.get("KADIMA_MCX_LIVE", "0") == "1"


def capital_limit():
    try:
        value = float(os.environ.get("KADIMA_MCX_CAPITAL_INR", "0"))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def session_open(now=None):
    return commodity_watch.monitoring_window(now or now_ist())


def session_exit_due(now=None):
    now = (now or now_ist()).astimezone(config.IST)
    return now.time().replace(tzinfo=None) >= SESSION_EXIT or not session_open(now)


def _fetch(tokens):
    tokens = [str(t) for t in dict.fromkeys(tokens) if str(t)]
    if not tokens or len(tokens) > 50:
        raise ValueError("mcx_quote_batch_invalid")
    if not QUOTE.wait_and_allow(timeout_sec=3.0):
        raise ValueError("mcx_quote_rate_limited")
    SESSION.ensure()
    try:
        response = SESSION.api.getMarketData(mode="FULL", exchangeTokens={"MCX": tokens})
    except TypeError:
        response = SESSION.api.getMarketData("FULL", {"MCX": tokens})
    return response


def _option_for(contracts, future, direction):
    try:
        future_expiry = datetime.fromisoformat(future["expiry"]).date()
    except (KeyError, ValueError, TypeError):
        return None
    group = []
    for row in contracts:
        if row.get("name") != future.get("name") or row.get("instrumenttype") != "OPTFUT":
            continue
        if row.get("type") != direction:
            continue
        try:
            expiry = datetime.fromisoformat(row["expiry"]).date()
        except (KeyError, ValueError, TypeError):
            continue
        if expiry <= future_expiry:
            group.append(row)
    if not group:
        return None
    expiry = min(datetime.fromisoformat(row["expiry"]).date() for row in group)
    group = [row for row in group if datetime.fromisoformat(row["expiry"]).date() == expiry]
    return min(group, key=lambda row: (abs(row["strike"] - future["ltp"]),
                                      row["strike"], row["symbol"]))


def _score(future_metrics, option_metrics):
    return round(min(100.0, 80.0
                     + min(10.0, abs(future_metrics["premium_move_pct"]) * 10)
                     + min(10.0, option_metrics["premium_move_pct"] / 2)), 2)


def macro_clear(now=None):
    now = now or now_ist()
    try:
        with open(MACRO_SNAPSHOT_PATH) as stream:
            payload=json.load(stream)
        stamp=datetime.fromisoformat(payload["ts"])
        if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= 900:
            return False
        today=now.date().isoformat()
        for event in payload.get("economic_calendar") or []:
            if event.get("date") <= today <= event.get("end_date"):
                return False
        return True
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError):
        return False


def global_blocks(executor, now=None):
    now = now or now_ist()
    reasons = []
    if not live_requested():
        reasons.append("mcx_live_disabled")
    if not session_open(now):
        reasons.append("outside_mcx_window")
    if capital_limit() is None:
        reasons.append("mcx_capital_unset")
    if not macro_clear(now):
        reasons.append("macro_event_suppression_active_or_unknown")
    if not celestial.enabled():
        reasons.append("rollout_disabled")
    else:
        cfg = celestial.read_config()
        if not cfg.get("auto_trade_enabled"):
            reasons.append("auto_paused")
        if cfg.get("kill_latched"):
            reasons.append("kill_latched")
    if getattr(executor, "_reconcile_blocked", True):
        reasons.append("broker_reconciliation_required")
    if killswitch.engaged():
        reasons.append("killswitch_engaged")
    if exit_manager.realized_pnl_today() <= -config.DAILY_LOSS_LIMIT_INR:
        reasons.append("daily_loss_limit_breached")
    if execution_ledger.pending():
        reasons.append("pending_order_exists")
    if any(getattr(p, "kind", None) == "mcx_momentum"
           and getattr(p, "status", None) != "closed"
           for p in executor.state.positions.values()):
        reasons.append("mcx_position_exists")
    return reasons


class Worker:
    def __init__(self, executor, scrips):
        self.executor = executor
        self.scrips = scrips
        self.future_windows = {}
        self.option_windows = {}
        self.last_episode = {}
        self.stop_event = threading.Event()
        self.thread = None
        self.rows = []

    def _contracts(self, now):
        rows = commodity_watch.catalog(self.scrips.rows, now.date())
        return [row for row in rows if row["name"] in ROOTS]

    def _future_rows(self, contracts, now):
        futures = commodity_watch.select_futures(contracts)
        if not futures:
            return []
        return commodity_watch.quote_rows(futures, _fetch([r["token"] for r in futures]), now)

    def _candidate(self, contracts, future, option_rows, option_metrics, now):
        if not future.get("fresh"):
            return None
        if not all(isinstance(future.get(k), (int,float)) and math.isfinite(future[k])
                   for k in ("ltp","volume","oi")):
            return None
        fspread = future.get("spread_pct")
        if not isinstance(fspread,(int,float)) or not math.isfinite(fspread) or fspread > MAX_FUTURE_SPREAD_PCT:
            return None
        fkey = future["name"]+":"+future["token"]
        fwindow = self.future_windows.setdefault(fkey, PremiumWindow())
        fm = fwindow.add(now, future["ltp"], future["volume"], future["oi"])
        if not fm or not fm["volume_spike"] or fm["oi_change"] <= 0:
            return None
        move = fm["premium_move_pct"]
        if move >= MIN_FUTURE_MOVE_PCT:
            direction = "CE"
        elif move <= -MIN_FUTURE_MOVE_PCT:
            direction = "PE"
        else:
            return None
        contract = _option_for(contracts, future, direction)
        if contract is None:
            return None
        row = option_rows.get(contract["token"])
        om = option_metrics.get(contract["token"])
        if not row or not row.get("fresh") or not all(
                isinstance(row.get(k),(int,float)) and math.isfinite(row[k])
                for k in ("ltp","volume","oi")):
            return None
        spread = row.get("spread_pct")
        if not isinstance(spread,(int,float)) or not math.isfinite(spread) or spread > MAX_OPTION_SPREAD_PCT:
            return None
        if (not om or om["premium_move_pct"] < MIN_OPTION_MOVE_PCT
                or not om["volume_spike"] or om["oi_change"] <= 0):
            return None
        samples = list(fwindow.samples)
        if len(samples) < 2:
            return None
        level = min(s[1] for s in samples) if direction == "CE" else max(s[1] for s in samples)
        if not (level < future["ltp"] if direction == "CE" else level > future["ltp"]):
            return None
        # MCX OPTFUT is an option on a futures contract. Do not reuse the
        # equity/spot Black-Scholes structure mapper here. The primary exit is
        # the observed futures structure level; a 15% premium stop is a
        # secondary hard-loss backstop and is still subject to rupee risk sizing.
        stop = row["ltp"] * 0.85
        if not 0 < stop < row["ltp"]:
            return None
        lot_size = row.get("broker_lot_size")
        if (not isinstance(lot_size,(int,float)) or isinstance(lot_size,bool)
                or not math.isfinite(lot_size) or lot_size <= 0 or lot_size != int(lot_size)):
            return None
        score = _score(fm,om)
        return dict(strategy="mcx_momentum", root=future["name"], direction=direction,
                    future_symbol=future["symbol"], future_token=future["token"],
                    future_ltp=future["ltp"], future_move_pct=round(move,3),
                    future_oi_change=fm["oi_change"], token=row["token"],
                    symbol=row["symbol"], strike=row["strike"], expiry=row["expiry"],
                    ltp=row["ltp"], bid=row["bid"], ask=row["ask"],
                    spread_pct=round(spread,3), option_move_pct=round(om["premium_move_pct"],3),
                    option_oi_change=om["oi_change"], stop=stop,
                    underlying_stop=level, lot_size=int(lot_size),
                    score=score, ts=now.isoformat(), baseline_at=fm["baseline_at"])

    def _enter(self, row, now):
        with self.executor._execution_lock:
            blocks = global_blocks(self.executor, now)
            if blocks:
                return dict(ok=False, reason=blocks[0], blocks=blocks)
            cap = capital_limit()
            ask = row.get("ask")
            if not isinstance(ask,(int,float)) or not math.isfinite(ask) or ask <= 0:
                return dict(ok=False, reason="mcx_ask_unavailable")
            limit_price = round(ask * 1.005, 1)
            lot = row["lot_size"]
            resolved_lot = capital_guard.get_lot_size(row["symbol"], row["token"])
            if resolved_lot != lot:
                return dict(ok=False, reason="mcx_lot_size_unverified")
            risk_unit = trade_policy.risk_per_unit(limit_price, row["stop"])
            if risk_unit is None:
                return dict(ok=False, reason="mcx_structure_risk_invalid")
            cost = limit_price * (1 + trade_policy.COST_RESERVE) * lot
            risk = risk_unit * lot
            equity = capital_guard.available_equity()
            if cost > cap or cost > equity:
                return dict(ok=False, reason="mcx_one_lot_cost_exceeds_capital",
                            cost_inr=round(cost,2), cap_inr=cap)
            if risk > cap * trade_policy.RISK_FRACTION:
                return dict(ok=False, reason="mcx_one_lot_risk_exceeds_cap",
                            risk_inr=round(risk,2), cap_inr=cap)
            episode = row["root"]+":"+row["direction"]+":"+row["token"]+":"+row["baseline_at"]
            if self.last_episode.get(row["root"]) == episode:
                return dict(ok=False, reason="mcx_episode_already_attempted")
            self.last_episode[row["root"]] = episode
            sid = storage.insert_signal(
                row["root"], "CONFIRMED", row["score"], row["direction"],
                json.dumps(row, allow_nan=False), kind="mcx_momentum")
            pos = Position(
                signal_id=sid, kind="mcx_momentum", index=row["root"],
                direction=row["direction"], symbol=row["symbol"], token=row["token"],
                exchange="MCX", entry_price=0.0, entry_ts=now, lots=1,
                lot_size=lot, remaining_qty=0, status="entry_pending",
                swing_level=row["underlying_stop"], structural_stop=row["stop"],
                effective_stop=row["stop"], allocated_capital=cost)
            self.executor.state.positions[str(sid)] = pos
            result = execution_ledger.submit(pos, "BUY", lot, limit_price)
            self.executor._last_reconcile = 0.0
            if pos.status == "closed":
                self.executor.state.positions.pop(str(sid), None)
            audit.log("MCX_LIVE_DISPATCH_RESULT", signal_id=sid, root=row["root"],
                      direction=row["direction"], score=row["score"], **result)
            return dict(result, signal_id=sid)

    def _protect(self, futures):
        by_root={row.get("name"):row for row in futures if row.get("fresh")}
        with self.executor._lock:
            positions=list(self.executor.state.positions.values())
        for pos in positions:
            if (getattr(pos,"kind",None)!="mcx_momentum"
                    or pos.status not in ("open","entry_pending")
                    or not isinstance(pos.swing_level,(int,float))):
                continue
            future=by_root.get(pos.index)
            if not future or not isinstance(future.get("ltp"),(int,float)):
                continue
            breached=(future["ltp"] <= pos.swing_level if pos.direction=="CE"
                      else future["ltp"] >= pos.swing_level)
            if breached:
                self.executor.manual_exit(pos.signal_id,"mcx_underlying_structure_failed")

    def cycle(self):
        now = now_ist()
        if not live_requested() or not session_open(now):
            celestial.health("mcx_live", dict(
                ok=True, live_requested=live_requested(), session_open=session_open(now),
                mode="DISABLED" if not live_requested() else "OUTSIDE_WINDOW",
                roots=list(ROOTS), one_lot_max=True))
            return
        contracts = self._contracts(now)
        futures = self._future_rows(contracts,now)
        self._protect(futures)
        option_contracts = {}
        for future in futures:
            if not future.get("fresh"):
                continue
            for direction in ("CE","PE"):
                contract = _option_for(contracts,future,direction)
                if contract is not None:
                    option_contracts[contract["token"]] = contract
        option_rows = {}
        option_metrics = {}
        if option_contracts:
            quoted = commodity_watch.quote_rows(
                list(option_contracts.values()), _fetch(list(option_contracts)), now)
            for row in quoted:
                option_rows[row["token"]] = row
                window = self.option_windows.setdefault(row["token"], PremiumWindow())
                valid = (row.get("fresh") and all(
                    isinstance(row.get(k),(int,float)) and math.isfinite(row[k])
                    for k in ("ltp","volume","oi")))
                option_metrics[row["token"]] = (
                    window.add(now,row["ltp"],row["volume"],row["oi"]) if valid else None)
                if not valid:
                    window.samples.clear()
        candidates = []
        for future in futures:
            try:
                row = self._candidate(contracts,future,option_rows,option_metrics,now)
                if row:
                    candidates.append(row)
            except Exception as exc:
                audit.log("MCX_CANDIDATE_ERROR", root=future.get("name"),
                          error_type=type(exc).__name__)
        candidates.sort(key=lambda row: row["score"], reverse=True)
        self.rows = candidates
        result = None
        if candidates:
            result = self._enter(candidates[0],now)
        celestial.health("mcx_live", dict(
            ok=True, live_requested=True, session_open=True, mode="GUARDED_LIVE",
            roots=list(ROOTS), candidates=candidates[:5], last_dispatch=result,
            one_lot_max=True, capital_limit_inr=capital_limit(),
            window="09:00-23:30_IST"))

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.cycle()
            except Exception as exc:
                audit.log("MCX_LIVE_WORKER_ERROR", error_type=type(exc).__name__)
                celestial.health("mcx_live", dict(ok=False,error_type=type(exc).__name__))
            self.stop_event.wait(POLL_SECONDS)

    def start(self):
        if self.thread is None:
            self.thread=threading.Thread(target=self.run,daemon=True,name="mcx-live")
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)


def main():
    """Standalone monitoring entrypoint is intentionally unsupported.

    MCX live runs inside KADIMA's engine process so execution serialization,
    broker reconciliation and the shared durable ledger remain single-writer.
    """
    raise SystemExit("mcx_live must run inside kadima.service")


if __name__ == "__main__":
    main()
