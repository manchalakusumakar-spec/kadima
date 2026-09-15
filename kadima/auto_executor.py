"""Automatic entry evaluation, confirmed-fill tracking and position monitoring.

See docs/executor_safeguards.md for lifecycle, recovery, limitations and tests.
Shared policy lives in trade_policy.py. Installation preserves live-order and
forecast feature flags; no model forecast is fabricated.
"""

from __future__ import annotations

import math
from functools import wraps
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from . import (alerts, audit, capital_guard, entry_quality, exit_manager,
              killswitch, risk_budget, storage, strike_selector)
from . import angel_orders, forecast_trail, execution_ledger, trade_policy
from .config import (DAILY_LOSS_LIMIT_INR, IST, MAX_CONCURRENT_POSITIONS,
                     MAX_LOTS_PER_SIGNAL, ORDER_PLACEMENT_ENABLED)
from .instrument_master import get_lot_size
from . import config
from .rate_limiter import ORDER_STATUS, QUOTE
from .session import SESSION
from .utils import in_market_hours, now_ist, past_signal_cutoff

# ---------------------------------------------------------------- config
MIN_PROJECTED_RETURN_PCT = 10.0   # entry criterion, not a target

# Shared profit-trail constants; fixed percentage retained only for legacy restores.
GIVEBACK_FLOOR_PCT = exit_manager.GAIN_ACTIVATION_PCT * 100.0   # trail only protects gains above this
GIVEBACK_TRAIL_PCT = exit_manager.GIVEBACK_PCT * 100.0          # exit once this much of the PEAK GAIN is lost
FIXED_STOPLOSS_PCT = exit_manager.INITIAL_SL_PCT * 100.0        # legacy restored positions only

# Existing settings retained; these are not exchange-calendar assertions.
EOD_FORCE_CLOSE_HHMM = (15, 38)
LIMIT_ORDER_BUFFER_PCT = 2.5
POSITION_MONITOR_INTERVAL_SEC = 10


# ---------------------------------------------------------------- premium history
@dataclass
class _PremiumHistory:
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    samples: deque = field(default_factory=lambda: deque(maxlen=400))  # (ts, ltp)


class PremiumTracker:
    """Per-contract intraday premium day-high/day-low and a rolling
    recent-price history, fed by the SAME option-chain poll cycle
    engine.py already runs -- no extra API calls. Exists because
    entry_quality.py's chase-prevention gate needs premium_day_high,
    premium_day_low, and a ~30-minutes-ago base price, none of which
    option_chain.py tracks itself (it only holds the latest snapshot)."""

    def __init__(self) -> None:
        self._hist: dict[str, _PremiumHistory] = {}
        self._day: Optional[str] = None
        self._lock = threading.Lock()

    def _roll_day(self, today: str) -> None:
        if self._day != today:
            self._hist = {}
            self._day = today

    def record(self, token: str, ltp: Optional[float], ts: datetime) -> None:
        if ltp is None or ltp <= 0:
            return
        with self._lock:
            self._roll_day(ts.date().isoformat())
            h = self._hist.setdefault(token, _PremiumHistory())
            h.day_high = ltp if h.day_high is None else max(h.day_high, ltp)
            h.day_low = ltp if h.day_low is None else min(h.day_low, ltp)
            h.samples.append((ts, ltp))

    def record_chain(self, chain) -> None:
        now = now_ist()
        for q in chain.quotes.values():
            self.record(q.token, q.ltp, now)

    def snapshot(self, token: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Returns (day_high, day_low, base_30min_ago)."""
        with self._lock:
            h = self._hist.get(token)
            if h is None:
                return None, None, None
            cutoff = now_ist() - timedelta(minutes=30)
            base = None
            for ts, ltp in h.samples:
                if ts <= cutoff:
                    base = ltp
                else:
                    break
            return h.day_high, h.day_low, base


# ---------------------------------------------------------------- position
@dataclass
class Position:
    signal_id: int
    kind: str                      # "bull_shield" | "gamma_blast"
    index: str
    direction: str                 # CE | PE
    symbol: str
    token: str
    exchange: str
    entry_price: float
    entry_ts: Optional[datetime]
    lots: int
    lot_size: int
    remaining_qty: int             # capital_guard reads this directly
    order_id: Optional[str] = None
    status: str = "open"
    peak_price: Optional[float] = None
    trail_active: bool = False
    exit_price: Optional[float] = None
    exit_ts: Optional[datetime] = None
    exit_reason: Optional[str] = None
    exit_order_id: Optional[str] = None
    structural_stop: Optional[float] = None
    effective_stop: Optional[float] = None
    allocated_capital: Optional[float] = None
    swing_level: Optional[float] = None  # confirmed underlying pivot


class ExecutorState:
    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}   # keyed by str(signal_id)
        self.premium_tracker = PremiumTracker()


# ---------------------------------------------------------------- broker calls
# Order placement itself lives in angel_orders.py (rate limiting, LIMIT-only
# enforcement, dry-run gating, response parsing — all tested there). This
# module only decides WHEN to call it, never talks to SESSION.api directly
# for order placement, so there's exactly one place that logic can go wrong.

def _poll_ltp(exchange: str, tokens: list[str]) -> dict[str, float]:
    """Direct LTP poll for specific tokens, independent of any
    OptionChain's ladder -- see module docstring for why this can't
    reuse option_chain's polling."""
    if not tokens:
        return {}
    payload = {"mode": "LTP", "exchangeTokens": {exchange: tokens}}
    try:
        SESSION.ensure()
        try:
            resp = SESSION.api.getMarketData(**payload)
        except TypeError:
            resp = SESSION.api.getMarketData("LTP", {exchange: tokens})
    except Exception as exc:                        # noqa: BLE001
        audit.log("position_ltp_poll_failed", error=str(exc), tokens=tokens)
        return {}
    if not resp or not resp.get("status"):
        return {}
    out: dict[str, float] = {}
    for row in resp.get("data", {}).get("fetched", []):
        token = str(row.get("symbolToken"))
        ltp = row.get("ltp")
        if ltp is not None:
            try:
                price = float(ltp)
                if math.isfinite(price) and price > 0:
                    out[token] = price
            except (TypeError, ValueError):
                pass
    return out


def fetch_broker_positions() -> Optional[list[dict]]:
    """Ground truth from the broker, for startup reconciliation. Returns
    None if unreadable -- callers must NOT treat that as 'no positions',
    same fail-closed convention as capital_guard's margin check."""
    if not ORDER_STATUS.wait_and_allow(timeout_sec=5.0):
        audit.log("broker_position_fetch_rate_limited")
        return None
    try:
        SESSION.ensure()
        resp = SESSION.api.position()
    except Exception as exc:                        # noqa: BLE001
        audit.log("broker_position_fetch_failed", error=str(exc))
        return None
    if not resp or not resp.get("status"):
        return None
    return resp.get("data") or []


# ---------------------------------------------------------------- entry math
def projected_return_pct(quote, spot: float, days_to_expiry: float) -> Optional[float]:
    """Delta x expected-underlying-move estimate of premium appreciation,
    as a % of current premium. This is the MINIMUM ENTRY CRITERION
    (>=10%), not a promise of that return -- it's a linear approximation
    (real option payoff is convex, not linear), whose job is only to
    stop 'cheap premium, needs an implausible move' entries from ever
    reaching the order path. Same purpose risk_budget.py already serves
    for rupee risk; this is the return-side equivalent. Returns None if
    IV or delta aren't available -- caller must treat that as a reason to
    reject the candidate, not as an assumed pass."""
    if not quote.iv or not quote.delta or not spot or spot <= 0:
        return None
    if not quote.ltp or quote.ltp <= 0:
        return None
    expected_move = spot * (quote.iv / 100.0) * math.sqrt(max(days_to_expiry, 0) / 365.0)
    projected_premium_move = abs(quote.delta) * expected_move
    return projected_premium_move / quote.ltp * 100.0


# ---------------------------------------------------------------- executor
def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._execution_lock:
            return method(self, *args, **kwargs)
    return call


class AutoExecutor:
    def __init__(self) -> None:
        self.state = ExecutorState()
        self._lock = threading.RLock()
        self._execution_lock = threading.RLock()
        self._positions_verified = False
        self._restored = False
        self._last_reconcile = 0.0
        self._reconcile_blocked = True   # stays True until reconcile_on_startup() runs
        self._stop = threading.Event()
        self._profit_projections: dict[int, forecast_trail.Projection] = {}

    def set_profit_projection(self, projection: forecast_trail.Projection) -> None:
        """Called by a 30-minute forecast producer; no order or HTTP endpoint."""
        with self._lock:
            pos = self.state.positions.get(str(projection.signal_id))
            if pos is None or pos.status != "open":
                raise ValueError("Projection must identify an open position")
            if projection.token != pos.token or projection.exchange != pos.exchange:
                raise ValueError("Projection contract does not match position")
            self._profit_projections[projection.signal_id] = projection

    # -------------------------------------------------------- startup
    @serialized
    def reconcile_on_startup(self) -> None:
        """Broker's position book is ground truth. Rebuilds in-memory
        state from it; local storage only fills in metadata the broker's
        response doesn't carry. Fails closed on an unreadable broker call."""
        self._last_reconcile = time.monotonic()
        self._reconcile_blocked = True
        if not self._restored:
            for row in storage.load_open_positions():
                for key in ('entry_ts', 'exit_ts'):
                    row[key] = datetime.fromisoformat(row[key]) if row.get(key) else None
                row['trail_active'] = bool(row['trail_active'])
                self.state.positions[str(row['signal_id'])] = Position(**row)
            self._restored = True
        if not execution_ledger.refresh(self.state.positions):
            return
        broker_positions = fetch_broker_positions()
        if broker_positions is None:
            audit.log('startup_reconcile_broker_unreadable')
            return
        book = angel_orders.fetch_order_book()
        if book is None:
            return
        pending_rows = execution_ledger.pending()
        own_ids = {r['order_id'] for r in pending_rows}
        own_tags = {r['intent'] for r in pending_rows}
        if any(o.status not in execution_ledger.TERMINAL and
               o.order_id not in own_ids and o.raw.get('ordertag') not in own_tags for o in book):
            self._positions_verified = False
            audit.log('reconcile_external_pending_orders')
            return
        live = {}
        for row in broker_positions:
            qty = float(row.get('netqty') or 0)
            if not qty:
                continue
            key = (str(row.get('exchange')), str(row.get('symboltoken')),
                   str(row.get('producttype') or ''))
            live[key] = live.get(key, 0) + qty
        expected = {}
        for pos in list(self.state.positions.values()):
            if pos.status == 'closed':
                self.state.positions.pop(str(pos.signal_id), None)
                continue
            if pos.remaining_qty:
                key = (pos.exchange, pos.token, 'INTRADAY')
                expected[key] = expected.get(key, 0) + pos.remaining_qty
        if live != expected:
            # Never turn an external/manual quantity change into an assumed fill.
            self._positions_verified = False
            audit.log('reconcile_position_quantity_mismatch',
                      broker_contracts=len(live), local_contracts=len(expected))
            return
        self._reconcile_blocked = False
        self._positions_verified = True
        audit.log('startup_reconcile_complete', restored=len(self.state.positions))

    @serialized
    def manual_exit(self, signal_id: int, reason: str = "manual_exit_dashboard") -> dict:
        """Manually exit one open position by signal_id, via the dashboard's
        Live Trade panel. Reuses the exact _close_position() path check_exits()
        already uses for automatic exits — no new order logic, no new risk logic."""
        if self._reconcile_blocked:
            return {"ok": False, "reason": "startup_reconcile_not_complete"}

        with self._lock:
            pos = self.state.positions.get(str(signal_id))
            if pos is None:
                return {"ok": False, "reason": "position_not_found", "signal_id": signal_id}
            if pos.status == "entry_pending":
                return dict(execution_ledger.request_entry_exit(pos, reason), signal_id=signal_id)
            if pos.status != "open":
                return {"ok": False, "reason": f"position_not_open (status={pos.status})", "signal_id": signal_id}

        ltp_map = _poll_ltp(pos.exchange, [pos.token])
        ltp = ltp_map.get(pos.token)
        if ltp is None:
            ltp = pos.peak_price or pos.entry_price
            audit.log("manual_exit_ltp_unavailable_fallback", signal_id=signal_id,
                       token=pos.token, fallback_price=ltp)

        try:
            result = self._close_position(pos, ltp, reason)
        except Exception as exc:                    # noqa: BLE001
            audit.log("manual_exit_failed", signal_id=signal_id, error=str(exc))
            return {"ok": False, "reason": f"close_position_failed: {exc}", "signal_id": signal_id}

        return dict(result, signal_id=signal_id)

    def _structure(self, chain, direction, spot):
        now = now_ist()
        try:
            expiry = datetime.strptime(chain.scrips.nearest_expiry(chain.index), '%d%b%Y')
            expiry = expiry.replace(tzinfo=now.tzinfo, hour=config.MARKET_CLOSE[0],
                                    minute=config.MARKET_CLOSE[1])
            days = (expiry - now).total_seconds() / 86400
            bars = storage.recent_candles(chain.index.token, 5, limit=100)
            level = trade_policy.swing(bars, direction, spot, now)
            if days <= 0 or level is None:
                return None
            return level, days
        except (AttributeError, TypeError, ValueError):
            return None

    @serialized
    def update_structure(self, index_name, chain, spot):
        """Use confirmed bars and existing fresh chain polls; no extra broker calls."""
        if not 0 <= time.monotonic() - getattr(chain, '_last_poll', 0) <= 30:
            return
        for pos in list(self.state.positions.values()):
            if pos.index != index_name or pos.status != 'open':
                continue
            result = self._structure(chain, pos.direction, spot)
            quote = chain.quotes.get(pos.token)
            if result is None or quote is None:
                continue
            level, days = result
            improves = pos.swing_level is None or (level > pos.swing_level if pos.direction == 'CE'
                                                   else level < pos.swing_level)
            if not improves:
                continue
            stop = trade_policy.premium_stop(spot, level, quote.strike, quote.ltp, days, pos.direction)
            if stop is not None:
                pos.swing_level = level
                pos.structural_stop = max(pos.structural_stop or 0, stop)
                pos.effective_stop = max(pos.effective_stop or 0, pos.structural_stop)
                storage.upsert_position(pos)

    # -------------------------------------------------------- entry
    @serialized
    def evaluate_and_enter(self, index_name: str, direction: str, chain, spot: float,
                           snap, days_to_expiry: float, kind: str,
                           source_signal_id: int, tier: str, score: float) -> None:
        """snap: indicators.Snapshot from the calling IndexState, used for
        real VWAP (not a stub) in the entry-quality check."""
        if not getattr(chain, '_entry_guard', lambda: True)():
            return self._reject('stock_signal_expired_or_replaced', index_name, direction)
        if self._reconcile_blocked:
            return self._reject("startup_reconcile_not_complete", index_name, direction)
        if killswitch.engaged():
            return self._reject("killswitch_engaged", index_name, direction)
        if not in_market_hours():
            return self._reject("outside_market_hours", index_name, direction)
        if past_signal_cutoff():
            return self._reject("past_signal_cutoff", index_name, direction)
        if exit_manager.realized_pnl_today() <= -DAILY_LOSS_LIMIT_INR:
            # Was never actually wired in until 2026-08-23: config declared
            # this limit, exit_manager had a check function, but nothing
            # called it and nothing fed it real rupee P&L (see
            # realized_pnl_today()'s fix, same date). Live trading had
            # zero daily-loss enforcement until now.
            return self._reject("daily_loss_limit_breached", index_name, direction)
        if execution_ledger.seen(source_signal_id):
            return self._reject("signal_already_submitted", index_name, direction)
        with self._lock:
            open_count = len(self.state.positions)
        if open_count >= min(1, MAX_CONCURRENT_POSITIONS):
            return self._reject("max_concurrent_positions", index_name, direction)

        if not 0 <= time.monotonic() - getattr(chain, '_spot_received_at', 0) <= 30:
            return self._reject('underlying_quote_stale', index_name, direction)
        if not 0 <= time.monotonic() - getattr(chain, '_alignment_at', 0) <= 240:
            return self._reject('alignment_snapshot_stale', index_name, direction)
        if not trade_policy.eligible(kind, tier, score, direction, snap, spot):
            return self._reject('eligibility_alignment_or_gamma_score', index_name, direction)
        structure = self._structure(chain, direction, spot)
        if structure is None:
            return self._reject('fresh_structure_or_valid_expiry_unavailable', index_name, direction)
        swing_level, days_to_expiry = structure

        last_poll = getattr(chain, '_last_poll', 0)
        if not last_poll or not 0 <= time.monotonic() - last_poll <= 30:
            return self._reject('option_chain_stale', index_name, direction)
        self.state.premium_tracker.record_chain(chain)
        # The ladder may straddle an expiry refresh; confirm selected tokens
        # against the current instrument-master expiry, not a symbol guess.
        try:
            current_contracts = chain.scrips.strikes_around(
                chain.index, chain.atm, config.STRIKES_EACH_SIDE)
            valid_tokens = {str(c['token']) for c in current_contracts}
        except (AttributeError, TypeError, KeyError):
            return self._reject('contract_expiry_unverifiable', index_name, direction)

        ranked = [r for r in strike_selector.rank(chain, direction, spot, days_to_expiry)
                  if not r.rejected and str(r.quote.token) in valid_tokens]
        if not ranked:
            return self._reject("no_tradeable_strike", index_name, direction)

        candidates = [
            risk_budget.StrikeCandidate(
                symbol=r.quote.symbol, token=r.quote.token, strike=r.quote.strike,
                option_type=r.quote.opt_type, ltp=r.quote.ltp, bid=r.quote.bid,
                ask=r.quote.ask, delta=r.quote.delta, volume=r.quote.volume,
                open_interest=r.quote.oi,
                stop_price=trade_policy.premium_stop(spot, swing_level, r.quote.strike,
                                                    r.quote.ltp, days_to_expiry, direction))
            for r in ranked
        ]

        allocation = capital_guard.get_capital_for_signal(kind, tier)

        verdict = risk_budget.select_within_budget(
            candidates, spot, allocated_capital=allocation, max_lots=MAX_LOTS_PER_SIGNAL)
        if verdict.candidate is None:
            return self._reject(f"risk_budget_{verdict.reason}", index_name, direction)

        chosen_quote = next(r.quote for r in ranked
                            if r.quote.token == verdict.candidate.token)

        proj_return = projected_return_pct(chosen_quote, spot, days_to_expiry)
        if proj_return is None:
            return self._reject("projected_return_uncomputable", index_name, direction)
        if proj_return < MIN_PROJECTED_RETURN_PCT:
            return self._reject(
                f"projected_return_{proj_return:.1f}pct_under_{MIN_PROJECTED_RETURN_PCT:.0f}pct_minimum",
                index_name, direction)

        day_high, day_low, base_30min = self.state.premium_tracker.snapshot(chosen_quote.token)
        ctx = entry_quality.EntryContext(
            ltp=chosen_quote.ltp,
            premium_day_high=day_high or chosen_quote.ltp,
            premium_day_low=day_low or chosen_quote.ltp,
            premium_base_30min=base_30min,
            spot=spot,
            vwap=snap.vwap if snap and snap.vwap else spot,
            swing_level=verdict.candidate.stop_price,
            target_premium=None,
            direction=direction,
            spread_pct=chosen_quote.spread_pct,
            stop_override=verdict.candidate.stop_price,
        )
        eq_verdict = entry_quality.evaluate_entry(ctx)
        if not eq_verdict.allowed:
            return self._reject(f"entry_quality_{eq_verdict.reason}", index_name, direction)

        allowed_lots, cap_reason = capital_guard.enforce(
            self, SimpleNamespace(quote=chosen_quote), direction, verdict.lots)
        if allowed_lots == 0:
            return self._reject(f"capital_guard_{cap_reason}", index_name, direction)

        lot_size = get_lot_size(chosen_quote.symbol, chosen_quote.token)
        if lot_size is None:
            return self._reject("lot_size_unresolvable_at_order_time", index_name, direction)

        # Require a fresh received LTP; do not fall back to a stale chain quote.
        fresh_ltp_map = _poll_ltp(chain.index.opt_exchange, [chosen_quote.token])
        pricing_ltp = fresh_ltp_map.get(chosen_quote.token)
        if pricing_ltp is None:
            return self._reject("fresh_entry_quote_unavailable", index_name, direction)

        limit_price = round(pricing_ltp * (1 + LIMIT_ORDER_BUFFER_PCT / 100.0), 1)
        stop = verdict.candidate.stop_price
        allowed_lots = trade_policy.lots_within_allocation(
            allocation, limit_price, stop, lot_size, allowed_lots)
        if allowed_lots < 1 or pricing_ltp <= stop:
            return self._reject('fresh_quote_exceeds_risk_or_invalidates_stop', index_name, direction)
        clock = time.monotonic()
        if (not 0 <= clock - chain._last_poll <= 30
                or not 0 <= clock - chain._spot_received_at <= 30
                or not 0 <= clock - chain._alignment_at <= 240):
            return self._reject('entry_data_expired_before_submission', index_name, direction)
        quantity = allowed_lots * lot_size

        if killswitch.engaged() or not in_market_hours() or past_signal_cutoff():
            return self._reject('entry_gate_changed_before_submission', index_name, direction)
        if not getattr(chain, '_entry_guard', lambda: True)():
            return self._reject('stock_signal_changed_before_submission', index_name, direction)
        pos = Position(
            signal_id=source_signal_id, kind=kind, index=index_name, direction=direction,
            symbol=chosen_quote.symbol, token=chosen_quote.token,
            exchange=chain.index.opt_exchange, entry_price=0.0,
            entry_ts=now_ist(), lots=allowed_lots, lot_size=lot_size,
            remaining_qty=0, status='entry_pending', swing_level=swing_level,
            structural_stop=stop, effective_stop=stop, allocated_capital=allocation)
        with self._lock:
            self.state.positions[str(source_signal_id)] = pos
        result = execution_ledger.submit(pos, 'BUY', quantity, limit_price)
        if pos.status == 'closed':
            self.state.positions.pop(str(source_signal_id), None)
        audit.log('entry_order_result', signal_id=source_signal_id, **result)
        alerts.enqueue(f"ENTRY order {pos.symbol}: {result['reason']} | signal #{source_signal_id}",
                       key=f'entry:{source_signal_id}')

    def _reject(self, reason: str, index_name: str, direction: str) -> None:
        audit.log("auto_executor_entry_rejected", reason=reason,
                  index=index_name, direction=direction)

    # -------------------------------------------------------- exit
    @serialized
    def check_exits(self) -> None:
        """Call periodically (e.g. every POSITION_MONITOR_INTERVAL_SEC)
        from a dedicated thread. Independent of any OptionChain's ladder."""
        if not self._positions_verified:
            return
        with self._lock:
            open_positions = [p for p in self.state.positions.values()
                              if p.status in ("open", "entry_pending") and p.remaining_qty > 0]
        if not open_positions:
            return

        by_exchange: dict[str, list[str]] = {}
        for p in open_positions:
            by_exchange.setdefault(p.exchange, []).append(p.token)

        ltp_by_token: dict[str, float] = {}
        for exch, tokens in by_exchange.items():
            ltp_by_token.update(_poll_ltp(exch, tokens))

        now = now_ist()
        eod_hh, eod_mm = EOD_FORCE_CLOSE_HHMM
        force_close = now.hour > eod_hh or (now.hour == eod_hh and now.minute >= eod_mm)
        # Computed once per cycle, not per position -- same realized_pnl_today()
        # query the entry gate uses. Checked with top priority below: if the
        # day's loss limit is breached, that overrides even an active trail,
        # matching exit_manager's original documented intent ("refuse new
        # entries and flatten open positions") which was written but never
        # actually connected to anything until 2026-08-23.
        daily_loss_breached = exit_manager.realized_pnl_today() <= -DAILY_LOSS_LIMIT_INR

        for pos in open_positions:
            ltp = ltp_by_token.get(pos.token)
            if ltp is None:
                audit.log("position_ltp_unavailable", signal_id=pos.signal_id,
                          token=pos.token)
                if force_close or daily_loss_breached:
                    # EOD close must not silently skip just because this
                    # cycle's LTP poll came back empty -- fall back to
                    # last known peak_price so the position still exits.
                    ltp = pos.peak_price or pos.entry_price
                else:
                    continue

            previous_peak = pos.peak_price
            pos.peak_price = ltp if pos.peak_price is None else max(pos.peak_price, ltp)
            if pos.peak_price != previous_peak:
                storage.upsert_position(pos)
            gain_pct = ((ltp - pos.entry_price) / pos.entry_price * 100.0
                       if pos.entry_price else 0.0)
            peak_gain = (pos.peak_price - pos.entry_price) if pos.peak_price else 0.0

            if not pos.trail_active and gain_pct >= GIVEBACK_FLOOR_PCT:
                pos.trail_active = True
                storage.upsert_position(pos)
                audit.log("trail_activated", signal_id=pos.signal_id,
                          gain_pct=round(gain_pct, 1))

            # Flat-only installer prevents introducing unprotected legacy positions.
            # Keep legacy emergency protection for a pre-amendment restore only.
            structural = pos.structural_stop or pos.entry_price * (1 - FIXED_STOPLOSS_PCT / 100)
            pos.effective_stop, active = trade_policy.ratchet(
                pos.entry_price, pos.peak_price, pos.effective_stop, structural)
            pos.trail_active = pos.trail_active or active
            storage.upsert_position(pos)
            # A queued exit remains binding after cancellation, restart or rebound.
            reason = pos.exit_reason
            if daily_loss_breached:
                reason = 'daily_loss_limit_breached'
            elif force_close:
                reason = 'eod_force_close'
            elif reason:
                pass
            elif ltp <= structural:
                reason = 'structural_stop_breached'
            elif pos.trail_active and ltp <= pos.effective_stop:
                reason = 'dynamic_profit_trail_breached'
                if getattr(config, 'FORECAST_TRAIL_ENABLED', False):
                    projection = self._profit_projections.get(pos.signal_id)
                    decision = forecast_trail.evaluate(
                        entry=pos.entry_price, peak=pos.peak_price,
                        previous_stop=pos.effective_stop, ltp=ltp,
                        quantity=pos.remaining_qty, giveback_fraction=0.5,
                        token=pos.token, exchange=pos.exchange,
                        signal_id=pos.signal_id, now=now, projection=projection)
                    audit.log('forecast_trail_decision', signal_id=pos.signal_id,
                              reason=decision.reason, stop=decision.stop,
                              should_exit=decision.should_exit)
                    reason = decision.reason if decision.should_exit else None

            if reason:
                self._close_position(pos, ltp, reason)

    @serialized
    def _close_position(self, pos: Position, ltp: float, reason: str) -> dict:
        if pos.status == 'entry_pending':
            return execution_ledger.request_entry_exit(pos, reason)
        if pos.status != 'open' or pos.remaining_qty <= 0:
            return {'ok': False, 'reason': 'position_not_open_or_order_pending'}
        if execution_ledger.pending(pos.signal_id):
            return {'ok': False, 'reason': 'order_already_pending'}
        pos.exit_reason = reason
        limit_price = round(ltp * (1 - LIMIT_ORDER_BUFFER_PCT / 100.0), 1)
        result = execution_ledger.submit(pos, 'SELL', pos.remaining_qty, limit_price)
        if pos.status == 'closed':
            self.state.positions.pop(str(pos.signal_id), None)
            self._profit_projections.pop(pos.signal_id, None)
        return result

    @serialized
    def refresh_orders(self):
        healthy = execution_ledger.refresh(self.state.positions)
        if not healthy:
            self._reconcile_blocked = True
        for key, pos in list(self.state.positions.items()):
            if pos.status == 'closed':
                self.state.positions.pop(key, None)
                self._profit_projections.pop(pos.signal_id, None)

    # -------------------------------------------------------- lifecycle
    def start_monitor_thread(self) -> None:
        threading.Thread(target=self._monitor_loop, daemon=True,
                         name="auto-executor-monitor").start()

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.refresh_orders()
                if time.monotonic() - self._last_reconcile >= 30:
                    self.reconcile_on_startup()
                from . import forecast_connection
                forecast_connection.refresh(self)
                self.check_exits()
            except Exception as exc:                    # noqa: BLE001
                audit.log("position_monitor_loop_error", error=str(exc))
            self._stop.wait(max(0.1, POSITION_MONITOR_INTERVAL_SEC - (time.monotonic() - started)))

    def stop(self) -> None:
        self._stop.set()


EXECUTOR = AutoExecutor()



