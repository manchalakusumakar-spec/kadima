"""
cas_executor.py -- automatic execution for CAS (closing session) blast trades.

Trader's decisions, 2026-09-29:
    * Fully automatic once cas_blast_scanner triggers, 15:15-15:35 IST.
    * CARRYFORWARD product: Angel squares off and blocks INTRADAY (MIS)
      at 15:20, and the big CAS moves come after that.
    * Size = min(CAS_MAX_CAPITAL_INR, live available cash), computed at the
      price cap, so the worst-case fill can never exceed the budget.
    * Enter at the lowest price: first LIMIT at the best bid, then stepped
      up every CAS_ENTRY_STEP_SEC through LTP and the ask, never above
      CAS_ENTRY_PRICE_CAP_MULT x the premium at signal time. Anything still
      unfilled after CAS_ENTRY_MAX_STEPS is cancelled.
    * No stop-loss at entry. The trailing stop arms at +CAS_TRAIL_ARM_PCT
      and exits on a CAS_TRAIL_GIVEBACK_PCT fall from the peak premium.
      The daily loss limit and the 15:38 EOD close still apply.
    * One CAS trade per day, in its own slot (not the main engine's).

Deliberately NOT applied: Shield confirmation, Celestial top-20, trade_policy
quality gates and portfolio capacity -- far-OTM CAS candidates can never pass
filters built for the main strategy. Kept: kill switch, startup reconciliation,
daily loss limit, durable execution ledger (one live order per signal, no
assumed fills), the session calendar/permit gate, and exclusion of contracts
or underlyings the trader holds manually.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from typing import Optional

from . import alerts, angel_orders, audit, capital_guard, execution_ledger, exit_manager
from . import killswitch, market_session_gate, storage
from .angel_orders import CAS_KIND
from .config import (CAS_ENTRY_MAX_STEPS, CAS_ENTRY_PRICE_CAP_MULT, CAS_ENTRY_STEP_SEC,
                     CAS_EXECUTION_ENABLED, CAS_EXIT_WATCH_SEC, CAS_MAX_CAPITAL_INR, CAS_MAX_SPREAD_PCT,
                     CAS_MAX_ORDER_QTY, CAS_MAX_TRADES_PER_DAY, CAS_PRODUCT_TYPE,
                     CAS_TRAIL_ARM_PCT, CAS_TRAIL_GIVEBACK_PCT, DAILY_LOSS_LIMIT_INR,
                     MARKET_CLOSE)
from .instrument_master import get_lot_size
from .session import SESSION
from .utils import now_ist

MAX_SIGNAL_AGE_SEC = 20        # a trigger older than this is stale, not tradeable
TICK = 0.1                     # angel_orders sends prices rounded to 0.1

_inflight = threading.Lock()   # at most one CAS entry being worked at a time
_watcher_lock = threading.Lock()
_watcher_running = False


# ---------------------------------------------------------------- helpers
def _executor():
    from .auto_executor import EXECUTOR
    return EXECUTOR


def _ceil_tick(x: float) -> float:
    return round(math.ceil(round(x / TICK, 6)) * TICK, 1)


def _floor_tick(x: float) -> float:
    return round(math.floor(round(x / TICK, 6)) * TICK, 1)


def fetch_quote(exchange: str, token: str) -> Optional[dict]:
    """Fresh LTP + best bid/ask for one contract (FULL mode carries depth)."""
    try:
        SESSION.ensure()
        payload = {"mode": "FULL", "exchangeTokens": {exchange: [token]}}
        try:
            resp = SESSION.api.getMarketData(**payload)
        except TypeError:
            resp = SESSION.api.getMarketData("FULL", {exchange: [token]})
    except Exception as exc:                            # noqa: BLE001
        audit.log("cas_quote_failed", token=token, error=type(exc).__name__)
        return None
    if not isinstance(resp, dict) or not resp.get("status"):
        return None
    for row in (resp.get("data") or {}).get("fetched", []) or []:
        if str(row.get("symbolToken")) != str(token):
            continue
        depth = row.get("depth") or {}
        buy, sell = depth.get("buy") or [], depth.get("sell") or []

        def _num(v):
            try:
                v = float(v)
                return v if math.isfinite(v) and v > 0 else None
            except (TypeError, ValueError):
                return None
        ltp = _num(row.get("ltp"))
        if ltp is None:
            return None
        return dict(ltp=ltp, bid=_num(buy[0].get("price")) if buy else None,
                    ask=_num(sell[0].get("price")) if sell else None)
    return None


def cas_trades_today() -> int:
    """CAS trades that actually got filled today (rejected/unfilled attempts
    are stored with entry_price 0 and do not count)."""
    day = now_ist().date().isoformat()
    row = storage.conn().execute(
        "SELECT COUNT(*) FROM positions WHERE kind=? AND entry_ts LIKE ? "
        "AND (entry_price > 0 OR status != 'closed')", (CAS_KIND, day + "%")).fetchone()
    return int(row[0] or 0)


def size_order(index: str, budget: float, cap_price: float, lot_size: int) -> tuple[int, int]:
    """(lots, quantity) affordable at the price cap, clamped to the freeze limit."""
    if budget <= 0 or cap_price <= 0 or lot_size <= 0:
        return 0, 0
    lots = int(budget // (cap_price * lot_size))
    freeze = CAS_MAX_ORDER_QTY.get(index)
    if freeze:
        lots = min(lots, freeze // lot_size)
    return max(lots, 0), max(lots, 0) * lot_size


def price_ladder(quote: dict, cap: float) -> list[float]:
    """Planned entry prices, cheapest first, each <= cap: best bid, LTP, ask,
    ask + 2%. ladder[0] is the first order; later steps are re-derived from a
    fresh quote at each step (_step_entry), still capped."""
    bid, ltp, ask = quote.get("bid"), quote["ltp"], quote.get("ask")
    raw = [bid or ltp, ltp, ask or ltp * 1.01, (ask or ltp) * 1.02]
    out = []
    for p in raw:
        p = min(_ceil_tick(p), _floor_tick(cap))
        if p > 0 and (not out or p > out[-1]):
            out.append(p)
    return out[:CAS_ENTRY_MAX_STEPS]


# ---------------------------------------------------------------- exit rule
def exit_reason(pos, ltp: float, daily_loss_breached: bool, force_close: bool) -> Optional[str]:
    """Called by AutoExecutor.check_exits for CAS positions only."""
    if daily_loss_breached:
        return "daily_loss_limit_breached"
    if force_close:
        return "eod_force_close"
    if pos.exit_reason:
        return pos.exit_reason          # a queued exit stays binding
    entry = pos.entry_price
    if not entry or entry <= 0 or pos.status != "open":
        return None                     # no stop-loss at entry, by design
    peak = max(pos.peak_price or entry, ltp)
    if peak >= entry * (1 + CAS_TRAIL_ARM_PCT / 100.0):
        if not pos.trail_active:
            pos.trail_active = True
            pos.effective_stop = round(max(entry, peak * (1 - CAS_TRAIL_GIVEBACK_PCT / 100.0)), 2)
            storage.upsert_position(pos)
            audit.log("cas_trail_armed", signal_id=pos.signal_id, entry=entry, peak=peak)
            alerts.enqueue(f"CAS trail armed {pos.symbol}: entry {entry:.2f}, peak {peak:.2f}",
                           key=f"cas-trail:{pos.signal_id}")
        stop = round(max(entry, peak * (1 - CAS_TRAIL_GIVEBACK_PCT / 100.0)), 2)
        if stop != pos.effective_stop:
            pos.effective_stop = stop
            storage.upsert_position(pos)
        if ltp <= stop:
            return "cas_peak_trail"
    return None


# ---------------------------------------------------------------- entry
def submit(candidate, chain) -> None:
    """Scanner hook. Returns immediately; the entry is worked on its own
    thread so the option-chain poll loop is never blocked by order steps."""
    if not CAS_EXECUTION_ENABLED:
        audit.log("cas_execution_disabled", symbol=candidate.symbol)
        return
    if not _inflight.acquire(blocking=False):
        audit.log("cas_entry_rejected", reason="entry_already_in_progress",
                  symbol=candidate.symbol)
        return
    def run():
        try:
            enter(candidate)
        except Exception as exc:                        # noqa: BLE001
            audit.log("cas_entry_exception", symbol=candidate.symbol, error=str(exc))
        finally:
            _inflight.release()
    threading.Thread(target=run, daemon=True, name="cas-entry").start()


def _reject(reason: str, candidate, **extra) -> dict:
    audit.log("cas_entry_rejected", reason=reason, symbol=candidate.symbol,
              index=candidate.index, **extra)
    return {"ok": False, "reason": reason}


def gate(candidate, executor, now: datetime) -> Optional[str]:
    """Every pre-trade check that does not need the broker. None = pass."""
    from .cas_blast_scanner import in_cas_window
    if killswitch.engaged():
        return "killswitch_engaged"
    if not in_cas_window(now):
        return "outside_cas_window"
    if not 0 <= (now - candidate.detected_at).total_seconds() <= MAX_SIGNAL_AGE_SEC:
        return "signal_stale"
    if executor._reconcile_blocked:
        return "startup_reconcile_not_complete"
    if exit_manager.realized_pnl_today() <= -DAILY_LOSS_LIMIT_INR:
        return "daily_loss_limit_breached"
    if cas_trades_today() >= CAS_MAX_TRADES_PER_DAY:
        return "cas_daily_trade_limit"
    # CAS remains a separate signal model, not a separate account-risk slot.
    # Preserve the operator's one-position-across-strategies rule.
    if any(p.status != "closed" for p in executor.state.positions.values()):
        return "account_position_already_open"
    if execution_ledger.pending():
        return "portfolio_order_pending_wait"
    if candidate.index in getattr(executor, "_manual_carryforward_underlyings", set()):
        return "manual_carryforward_same_underlying"
    if (candidate.exchange, candidate.token) in getattr(executor, "_external_contracts", set()):
        return "contract_held_outside_kadima"
    return market_session_gate.entry_block(candidate.exchange)


def enter(candidate) -> dict:
    executor = _executor()
    from .auto_executor import Position
    cap = _floor_tick(candidate.ltp * CAS_ENTRY_PRICE_CAP_MULT)

    with executor._execution_lock:
        block = gate(candidate, executor, now_ist())
        if block:
            return _reject(block, candidate)
        lot_size = get_lot_size(candidate.symbol, candidate.token)
        if not lot_size:
            return _reject("lot_size_unresolved", candidate)
        funds = capital_guard.broker_available_funds()
        if funds is None:
            return _reject("available_funds_unreadable", candidate)
        budget = min(float(CAS_MAX_CAPITAL_INR), funds)
        quote = fetch_quote(candidate.exchange, candidate.token)
        if quote is None:
            return _reject("fresh_quote_unavailable", candidate)
        if quote["ltp"] > cap:
            return _reject("premium_ran_past_cap", candidate, ltp=quote["ltp"], cap=cap)
        bid, ask = quote.get("bid"), quote.get("ask")
        if not bid or not ask or ask < bid:
            return _reject("invalid_or_missing_top_of_book", candidate)
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        if spread_pct > CAS_MAX_SPREAD_PCT:
            return _reject("spread_too_wide", candidate, spread_pct=round(spread_pct, 2))
        lots, qty = size_order(candidate.index, budget, cap, lot_size)
        if lots < 1:
            return _reject("budget_below_one_lot", candidate, budget=round(budget, 2),
                           cap=cap, lot_size=lot_size)
        ladder = price_ladder(quote, cap)
        if not ladder:
            return _reject("no_valid_entry_price", candidate, cap=cap)

        payload = dict(source="CAS_BLAST", strike=candidate.strike, trigger_ltp=candidate.ltp,
                       premium_change_pct=candidate.premium_change_pct, cap=cap,
                       budget=round(budget, 2), lots=lots, product=CAS_PRODUCT_TYPE)
        signal_id = storage.insert_signal(candidate.index, "CAS", candidate.premium_change_pct,
                                          candidate.option_type, json.dumps(payload), kind=CAS_KIND)
        pos = Position(signal_id=signal_id, kind=CAS_KIND, index=candidate.index,
                       direction=candidate.option_type, symbol=candidate.symbol,
                       token=candidate.token, exchange=candidate.exchange, entry_price=0.0,
                       entry_ts=now_ist(), lots=lots, lot_size=lot_size, remaining_qty=0,
                       status="entry_pending", allocated_capital=round(qty * cap, 2))
        with executor._lock:
            executor.state.positions[str(signal_id)] = pos
        result = execution_ledger.submit(pos, "BUY", qty, ladder[0])
        if pos.status == "closed":
            executor.state.positions.pop(str(signal_id), None)
        audit.log("cas_entry_submitted", signal_id=signal_id, symbol=candidate.symbol,
                  qty=qty, lots=lots, price=ladder[0], cap=cap, ladder=ladder, **result)

    alerts.enqueue(f"CAS ENTRY {candidate.symbol}: {lots} lot(s) @ {ladder[0]} "
                   f"(cap {cap}) -> {result.get('reason')}", key=f"cas-entry:{signal_id}")
    if result.get("ok"):
        # Start reconciliation/protection immediately. A partial fill can occur
        # while the unfilled remainder is still being repriced.
        start_exit_watcher()
    if result.get("ok") and result.get("pending") and result.get("order_id"):
        _step_entry(pos, result["order_id"], qty, ladder[0], cap)
    return result


def _step_entry(pos, order_id: str, qty: int, start: float, cap: float) -> None:
    """Walk an unfilled entry up from the bid: fresh LTP, then fresh ask, then
    ask + 2%, each re-read from the market and never above `cap`; then cancel
    what is left. Fills are never applied here -- execution_ledger.refresh
    (monitor thread) remains the only place that books them."""
    contract = dict(tradingsymbol=pos.symbol, symboltoken=pos.token,
                    exchange=pos.exchange, producttype=CAS_PRODUCT_TYPE)
    targets = (lambda q: q["ltp"], lambda q: q["ask"] or q["ltp"],
               lambda q: (q["ask"] or q["ltp"]) * 1.02)[:max(CAS_ENTRY_MAX_STEPS - 1, 0)]
    current = start
    for step, target in enumerate(targets, start=2):
        time.sleep(CAS_ENTRY_STEP_SEC)
        status = angel_orders.get_order_status(order_id)
        if status is not None and (status.status in execution_ledger.TERMINAL
                                   or status.filled_qty >= qty):
            audit.log("cas_entry_step_done", signal_id=pos.signal_id, status=status.status,
                      filled=status.filled_qty, at_step=step - 1)
            return
        fresh = fetch_quote(pos.exchange, pos.token)
        if fresh is None:
            continue
        price = min(_ceil_tick(target(fresh)), _floor_tick(cap))
        if price <= current:
            continue
        res = angel_orders.modify_order(order_id, price=price, quantity=qty, contract=contract)
        audit.log("cas_entry_repriced", signal_id=pos.signal_id, step=step,
                  old_price=current, new_price=price, cap=cap, accepted=res.ok, reason=res.reason)
        if res.ok:
            current = price
    time.sleep(CAS_ENTRY_STEP_SEC)
    status = angel_orders.get_order_status(order_id)
    if status is not None and (status.status in execution_ledger.TERMINAL
                               or status.filled_qty >= qty):
        return
    res = angel_orders.cancel_order(order_id)
    audit.log("cas_entry_unfilled_remainder_cancelled", signal_id=pos.signal_id,
              order_id=order_id, filled=getattr(status, "filled_qty", None),
              accepted=res.ok, reason=res.reason)


# ---------------------------------------------------------------- exit watch
def _cas_active(executor) -> bool:
    return any(getattr(p, "kind", None) == CAS_KIND and p.status != "closed"
               for p in executor.state.positions.values())


def start_exit_watcher() -> None:
    """Checks CAS positions every CAS_EXIT_WATCH_SEC (main monitor: 10s) so a
    fast CAS reversal is caught sooner. Exits once no CAS position remains
    or the market has closed."""
    global _watcher_running
    with _watcher_lock:
        if _watcher_running:
            return
        _watcher_running = True

    def loop():
        global _watcher_running
        executor = _executor()
        try:
            while True:
                now = now_ist()
                if (now.hour, now.minute) > MARKET_CLOSE or not _cas_active(executor):
                    return
                try:
                    if any(getattr(executor.state.positions.get(str(r["signal_id"])), "kind", None)
                           == CAS_KIND for r in execution_ledger.pending()):
                        executor.refresh_orders()
                    executor.check_exits(kinds={CAS_KIND})
                except Exception as exc:                # noqa: BLE001
                    audit.log("cas_exit_watch_error", error=str(exc))
                time.sleep(CAS_EXIT_WATCH_SEC)
        finally:
            with _watcher_lock:
                _watcher_running = False

    threading.Thread(target=loop, daemon=True, name="cas-exit-watch").start()
