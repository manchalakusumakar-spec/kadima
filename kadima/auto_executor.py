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
from . import celestial
from . import fast_momentum, gap_and_go
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
    mode = 'FULL' if celestial.enabled() else 'LTP'
    payload = {"mode": mode, "exchangeTokens": {exchange: tokens}}
    try:
        SESSION.ensure()
        try:
            resp = SESSION.api.getMarketData(**payload)
        except TypeError:
            resp = SESSION.api.getMarketData(mode, {exchange: tokens})
    except Exception as exc:                        # noqa: BLE001
        audit.log("position_ltp_poll_failed", error=str(exc), tokens=tokens)
        return {}
    if not resp or not resp.get("status"):
        return {}
    out: dict[str, float] = {}
    for row in resp.get("data", {}).get("fetched", []):
        token = str(row.get("symbolToken"))
        if token not in tokens or row.get('exchange', exchange) != exchange:
            continue
        ltp = row.get("ltp")
        if ltp is not None:
            try:
                price = float(ltp)
                if math.isfinite(price) and price > 0:
                    out[token] = price
                    if celestial.enabled():
                        from .celestial_exit import publish_quote
                        publish_quote(exchange, token, row)
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
        self.celestial_master = None
        self._celestial_contexts = {}
        self._celestial_verified = set()
        self._celestial_quotes = {}
        # Verified broker-side manual carry-forward positions are observed but
        # never adopted into executor state. Their underlyings are entry-blocked
        # until a later successful reconciliation no longer sees them.
        self._manual_carryforward_underlyings = set()
        self.fast_rows = {}
        self.entry_diagnostics = deque(maxlen=100)

    @serialized
    def prepare_celestial_context(self, name, chain, spot, snap, days):
        if not celestial.enabled():
            return
        rows = [r for r in celestial.top20(manual=True) if r['underlying']==name and r['eligible']]
        clock = time.monotonic()
        self._celestial_contexts = {k:v for k,v in self._celestial_contexts.items() if clock-v['at']<=30}
        for row in rows:
            sid = celestial.signal_for(row)
            args = dict(index_name=name,direction=row['direction'],chain=chain,spot=spot,
                snap=snap,days_to_expiry=days,kind='gamma_blast',source_signal_id=sid,
                tier='ACTIVE',score=row['final_score'],candidate_id=row['id'])
            self._celestial_contexts[row['id']] = dict(at=clock,args=args)
            if celestial.read_config()['auto_trade_enabled']:
                self.evaluate_and_enter(**args)

    @serialized
    def buy_candidate(self, candidate_id):
        context = self._celestial_contexts.get(candidate_id)
        if context is None or not 0 <= time.monotonic()-context['at']<=30:
            return {'ok':False,'reason':'fresh_execution_context_unavailable'}
        return self.evaluate_and_enter(**context['args'],manual_request=True)

    @serialized
    def squareoff(self, reason='squareoff_dashboard'):
        celestial.update_config({'auto_trade_enabled':False})
        results = []
        for pos in list(self.state.positions.values()):
            if pos.status in ('open','entry_pending'):
                pos.exit_reason = reason
                storage.upsert_position(pos)
                results.append(self.manual_exit(pos.signal_id,reason))
        unresolved = celestial.health('reconciliation') or {}
        return {'ok':all(r.get('ok') for r in results) and unresolved.get('ok') is True,
                'reason':'exit_requests_submitted_not_fill_confirmation','results':results,
                'reconciliation':unresolved}

    @serialized
    def tighten_stop(self, signal_id, price):
        pos = self.state.positions.get(str(signal_id))
        if not pos or pos.status not in ('open','entry_pending') or pos.remaining_qty<=0:
            return {'ok':False,'reason':'owned_position_not_found'}
        if not celestial.finite(price,.000001) or price < (pos.effective_stop or pos.structural_stop or 0):
            return {'ok':False,'reason':'stop_may_only_tighten'}
        ltp = _poll_ltp(pos.exchange,[pos.token]).get(pos.token)
        if ltp is None or price>=ltp:
            return {'ok':False,'reason':'fresh_quote_required_and_stop_must_be_below_ltp'}
        pos.effective_stop = price
        storage.upsert_position(pos)
        return {'ok':True,'signal_id':signal_id,'effective_stop':price,'protection':'engine_managed_not_broker_hosted'}

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
        if celestial.enabled():
            self._positions_verified = False
            self._celestial_verified.clear()
            self._manual_carryforward_underlyings.clear()
            celestial.health('reconciliation',dict(ok=False,reason='checking_broker_books'))
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
        if celestial.enabled():
            if self.celestial_master is None:
                celestial.health('reconciliation',dict(ok=False,reason='contract_master_unavailable'))
                return
            from . import celestial_adoption
            from .external_reconciliation import reconcile as reconcile_external
            unresolved_external = reconcile_external(self, broker_positions, book)
            live, expected, observations, proposals = celestial_adoption.inspect(
                self,broker_positions,book,own_ids,own_tags,self.celestial_master)
            if any(r.get('reason')=='invalid_broker_position' for r in observations):
                celestial.health('reconciliation',dict(ok=False,positions=observations))
                return
            celestial_adoption.adopt(self,proposals)
            self._manual_carryforward_underlyings = {
                r.get('underlying') for r in observations
                if r.get('source') == 'MANUAL_CARRYFORWARD'
                and r.get('managed') is True and r.get('underlying')
            }
            # Capture the existing external book as the adoption baseline.
            reconcile_external(self, broker_positions, book)
            if unresolved_external:
                observations.append(dict(source='BROKER_EXTERNAL', managed=False,
                    reason='external_fill_history_unresolved', signal_ids=sorted(unresolved_external)))
            for p in self.state.positions.values():
                key = p.exchange,p.token,'INTRADAY'
                if p.remaining_qty>0 and live.get(key)==p.remaining_qty and str(p.signal_id) not in unresolved_external:
                    self._celestial_verified.add(str(p.signal_id))
            healthy = all(r.get('managed') is True for r in observations)
            if any(r.get('reason')=='external_pending_orders' for r in observations):
                self._celestial_verified.clear()
                self._positions_verified = False
            else:
                self._positions_verified = bool(self._celestial_verified) or healthy
            self._reconcile_blocked = not healthy
            celestial.health('reconciliation',dict(ok=healthy,positions=observations))
            return
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
        if self._reconcile_blocked and not (celestial.enabled() and str(signal_id) in self._celestial_verified):
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
                           source_signal_id: int, tier: str, score: float,
                           candidate_id=None, manual_request=False, shield_context=None,
                           fast_context=None, gap_context=None):
        """snap: indicators.Snapshot from the calling IndexState, used for
        real VWAP (not a stub) in the entry-quality check."""
        if not getattr(chain, '_entry_guard', lambda: True)():
            return self._reject('stock_signal_expired_or_replaced', index_name, direction)
        selected = None
        minimum = None
        fast = None
        gap = None
        if kind == 'fast_momentum':
            if not ORDER_PLACEMENT_ENABLED:
                return self._reject('broker_orders_disabled',index_name,direction)
            fast = fast_context() if callable(fast_context) else None
            if (not fast or not fast.get('eligible') or fast.get('strategy')!='fast_momentum'
                    or fast.get('underlying')!=index_name or fast.get('direction')!=direction
                    or not celestial.fresh(fast.get('ts'))
                    or fast_momentum.live_blocks(index_name,direction,now_ist(),fast.get('score',0))):
                return self._reject('fast_context_or_live_gate_blocked',index_name,direction)
        if kind == 'gap_and_go':
            if not ORDER_PLACEMENT_ENABLED:
                return self._reject('broker_orders_disabled',index_name,direction)
            gap = gap_context() if callable(gap_context) else None
            if (not gap or not gap.get('eligible') or gap.get('strategy')!='gap_and_go'
                    or gap.get('underlying')!=index_name or gap.get('direction')!='CE'
                    or direction!='CE' or not celestial.fresh(gap.get('ts'))
                    or gap_and_go.live_blocks(index_name, now_ist())):
                return self._reject('gap_context_or_live_gate_blocked',index_name,direction)
        if celestial.enabled() and fast is None and gap is None:
            if candidate_id is None:
                return self._reject('legacy_entry_disabled_in_celestial_mode',index_name,direction)
            selected = celestial.entry_candidate(index_name,direction,manual=manual_request,candidate_id=candidate_id)
            if selected is None:
                return self._reject('celestial_top20_gate',index_name,direction)
            minimum = celestial.read_config()['score_threshold']
            if kind not in ('bull_shield', 'bear_shield'):
                score = selected['final_score']
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
        if index_name in self._manual_carryforward_underlyings:
            return self._reject("manual_carryforward_same_underlying", index_name, direction)
        with self._lock:
            open_count = len(self.state.positions)
            same_underlying = any(
                getattr(p, 'index', None) == index_name
                and getattr(p, 'status', None) != 'closed'
                and (kind == 'gap_and_go' or getattr(p, 'kind', None) == 'gap_and_go')
                for p in self.state.positions.values())
        if same_underlying:
            return self._reject("same_underlying_position_exists", index_name, direction)
        if open_count >= min(1, MAX_CONCURRENT_POSITIONS):
            return self._reject("max_concurrent_positions", index_name, direction)

        if not 0 <= time.monotonic() - getattr(chain, '_spot_received_at', 0) <= 30:
            return self._reject('underlying_quote_stale', index_name, direction)
        if fast is None and gap is None and not 0 <= time.monotonic() - getattr(chain, '_alignment_at', 0) <= 240:
            return self._reject('alignment_snapshot_stale', index_name, direction)
        evidence = shield_context() if callable(shield_context) else None
        if kind in ('bull_shield', 'bear_shield') and (
                evidence is None or evidence.index != index_name):
            return self._reject('shield_confirmation_evidence_missing', index_name, direction)
        if fast is None and gap is None and not trade_policy.eligible(kind, tier, score, direction, snap, spot,minimum=minimum,
                                                     shield_evidence=evidence, now=now_ist()):
            return self._reject('eligibility_alignment_or_gamma_score', index_name, direction)
        special = fast or gap
        structure = (special['spot_stop'],special['days']) if special else self._structure(chain, direction, spot)
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
        if fast:
            ranked = [r for r in ranked if r.quote.token==fast['token'] and r.quote.symbol==fast['symbol']
                      and chain.index.opt_exchange==fast['exchange']]
        if gap:
            ranked = [r for r in ranked if r.quote.token==gap['token']
                      and r.quote.symbol==gap['contract_symbol']
                      and chain.index.opt_exchange==gap['exchange']]
        if gap:
            latest_gap = gap_context()
            if (not latest_gap or not latest_gap.get('eligible')
                    or not celestial.fresh(latest_gap.get('ts'))
                    or any(latest_gap.get(k)!=gap.get(k)
                           for k in ('episode','token','contract_symbol','exchange','direction','spot_stop'))
                    or gap_and_go.live_blocks(index_name, now_ist())):
                return self._reject('gap_invalidated_before_submission',index_name,direction)
        if selected is not None:
            ranked = [r for r in ranked if chain.index.opt_exchange+':'+r.quote.token == selected['id']]
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

        # Reuse the existing Shield allocation; do not introduce an unlimited budget.
        allocation = (capital_guard.get_capital_for_signal('bull_shield','CONFIRMED')
                      if special else capital_guard.get_capital_for_signal(kind, tier))

        verdict = risk_budget.select_within_budget(
            candidates, spot, allocated_capital=allocation,
            max_lots=min(1, MAX_LOTS_PER_SIGNAL) if gap else MAX_LOTS_PER_SIGNAL)
        if verdict.candidate is None:
            return self._reject(f"risk_budget_{verdict.reason}", index_name, direction)

        chosen_quote = next(r.quote for r in ranked
                            if r.quote.token == verdict.candidate.token)

        proj_return = projected_return_pct(chosen_quote, spot, days_to_expiry)
        if fast is None and gap is None and proj_return is None:
            return self._reject("projected_return_uncomputable", index_name, direction)
        if fast is None and gap is None and proj_return < MIN_PROJECTED_RETURN_PCT:
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
        if fast is None and gap is None and not eq_verdict.allowed:
            return self._reject(f"entry_quality_{eq_verdict.reason}", index_name, direction)

        allowed_lots, cap_reason = capital_guard.enforce(
            self, SimpleNamespace(quote=chosen_quote), direction, verdict.lots)
        if gap:
            allowed_lots = min(1, allowed_lots)
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
        if special:
            # Bounded entry price, no indefinite chasing after detection.
            cap = math.floor(special['entry']*1.005*20)/20
            if pricing_ltp>cap:
                reason = 'gap_entry_price_ran_away' if gap else 'fast_entry_price_ran_away'
                return self._reject(reason,index_name,direction)
            limit_price = min(limit_price,cap)
        stop = verdict.candidate.stop_price
        allowed_lots = trade_policy.lots_within_allocation(
            allocation, limit_price, stop, lot_size, allowed_lots)
        if allowed_lots < 1 or pricing_ltp <= stop:
            return self._reject('fresh_quote_exceeds_risk_or_invalidates_stop', index_name, direction)
        clock = time.monotonic()
        if (not 0 <= clock - chain._last_poll <= 30
                or not 0 <= clock - chain._spot_received_at <= 30
                or (fast is None and gap is None and not 0 <= clock - chain._alignment_at <= 240)):
            return self._reject('entry_data_expired_before_submission', index_name, direction)
        quantity = allowed_lots * lot_size

        if killswitch.engaged() or not in_market_hours() or past_signal_cutoff():
            return self._reject('entry_gate_changed_before_submission', index_name, direction)
        if not getattr(chain, '_entry_guard', lambda: True)():
            return self._reject('stock_signal_changed_before_submission', index_name, direction)
        if fast:
            latest = fast_context()
            if (not latest or not latest.get('eligible') or not celestial.fresh(latest.get('ts'))
                    or any(latest.get(k)!=fast.get(k) for k in ('episode','token','symbol','exchange','direction','spot_stop'))
                    or fast_momentum.live_blocks(index_name,direction,now_ist(),latest.get('score',0))):
                return self._reject('fast_invalidated_before_submission',index_name,direction)
        if gap:
            latest_gap = gap_context()
            if (not latest_gap or not latest_gap.get('eligible')
                    or not celestial.fresh(latest_gap.get('ts'))
                    or any(latest_gap.get(k)!=gap.get(k)
                           for k in ('episode','token','contract_symbol','exchange','direction','spot_stop'))
                    or gap_and_go.live_blocks(index_name, now_ist())):
                return self._reject('gap_invalidated_before_submission',index_name,direction)
        if selected is not None:
            latest = celestial.entry_candidate(index_name,direction,manual=manual_request,candidate_id=selected['id'])
            if latest is None or latest['last_updated'] != selected['last_updated']:
                return self._reject('celestial_candidate_changed_before_submission',index_name,direction)
        if kind in ('bull_shield', 'bear_shield'):
            latest_evidence = shield_context()
            if (latest_evidence is None or latest_evidence.index != index_name
                    or latest_evidence.direction != direction or latest_evidence.score != score
                    or not trade_policy.is_shield_confirmed(latest_evidence, now_ist(),
                                                            minimum or trade_policy.SHIELD_MIN_SCORE)):
                return self._reject('shield_invalidated_before_submission', index_name, direction)
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
        self._last_reconcile = 0.0
        if pos.status == 'closed':
            self.state.positions.pop(str(source_signal_id), None)
        audit.log('entry_order_result', signal_id=source_signal_id, **result)
        self.entry_diagnostics.append(dict(ts=now_ist().isoformat(),index=index_name,
            direction=direction,signal_id=source_signal_id,stage='SUBMISSION_RESULT',**result))
        alerts.enqueue(f"ENTRY order {pos.symbol}: {result['reason']} | signal #{source_signal_id}",
                       key=f'entry:{source_signal_id}')
        return result

    def _reject(self, reason: str, index_name: str, direction: str) -> dict:
        self.entry_diagnostics.append(dict(ts=now_ist().isoformat(),index=index_name,
                                          direction=direction,stage='BLOCKED',reason=reason))
        audit.log("auto_executor_entry_rejected", reason=reason,
                  index=index_name, direction=direction)
        return {'ok':False,'reason':reason}

    # -------------------------------------------------------- exit
    @serialized
    def check_exits(self) -> None:
        """Call periodically (e.g. every POSITION_MONITOR_INTERVAL_SEC)
        from a dedicated thread. Independent of any OptionChain's ladder."""
        if not self._positions_verified:
            return
        with self._lock:
            open_positions = [p for p in self.state.positions.values()
                              if p.status in ("open", "entry_pending") and p.remaining_qty > 0
                              and (not celestial.enabled() or str(p.signal_id) in self._celestial_verified)]
        if not open_positions:
            return

        by_exchange: dict[str, list[str]] = {}
        for p in open_positions:
            by_exchange.setdefault(p.exchange, []).append(p.token)

        ltp_by_contract: dict[tuple[str,str], float] = {}
        for exch, tokens in by_exchange.items():
            ltp_by_contract.update({(exch,token):price for token,price in _poll_ltp(exch,tokens).items()})

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
            ltp = ltp_by_contract.get((pos.exchange,pos.token))
            if celestial.enabled() and ltp is not None:
                self._celestial_quotes[pos.exchange+':'+pos.token] = dict(ltp=ltp,ts=now.isoformat())
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
            if celestial.enabled():
                pos.effective_stop, active = celestial.trail_stop(pos.entry_price,pos.peak_price,
                    pos.effective_stop,structural,celestial.retain_fraction(pos.signal_id))
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
            elif getattr(pos,'kind',None)=='fast_momentum' and fast_momentum.timeout_reason(pos,now):
                reason = fast_momentum.timeout_reason(pos,now)
            elif ltp <= structural:
                reason = 'structural_stop_breached'
            elif (pos.trail_active or celestial.enabled()) and ltp <= pos.effective_stop:
                reason = 'dynamic_profit_trail_breached'
                if not celestial.enabled() and getattr(config, 'FORECAST_TRAIL_ENABLED', False):
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

            if not reason and celestial.enabled():
                from .celestial_exit import evaluate
                reason = evaluate(pos, ltp, now)

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
