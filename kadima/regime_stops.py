"""Deterministic draft stop decisions. No broker transport or live activation.

All timestamps are timezone-aware. Prices are rupees; IV/vega use one vol point,
delta is signed (negative for PE), ATRs use the regime's completed-bar timeframe.
See docs/dynamic_stops_v1.md for the normalized input contract and release gates.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR

from . import audit, config, structure_guard


@dataclass(frozen=True)
class Reject:
    reason: str


@dataclass(frozen=True)
class StopPlan:
    regime: str
    direction: str
    token: str
    exchange: str
    entry_price: float
    initial_stop: float
    r_value: float
    allowed_lots: int
    lot_size: int
    entry_spread: float
    invalidation_spot: float | None
    entry_ts: str
    hard_exit: str
    expiry: str
    params: dict
    session: dict
    score: float
    costs: float
    pre_entry: bool = False
    sniper: bool = False
    recovery: bool = False
    asset: str = ''
    hard_stop_premium: float | None = None
    source: str = ''
    manual_cas_start: str | None = None


@dataclass
class StopPosition:
    signal_id: int
    plan: StopPlan
    remaining_qty: int
    phase: str = 'INITIAL'
    peak_premium: float = 0.
    active_stop: float = 0.
    last_ts: str | None = None
    last_high_ts: str | None = None
    iv_peak: float | None = None
    graduated: bool = False
    seen: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)
    completed: list = field(default_factory=list)
    pending: dict | None = None
    exit_after_pending: str | None = None
    protected_stop: float = 0.
    structure_guard: dict = field(default_factory=dict)

    @property
    def regime(self):
        return self.plan.regime

    @property
    def r_value(self):
        return self.plan.r_value


@dataclass(frozen=True)
class StopDecision:
    action: str
    reason: str
    stop: float | None
    quantity: int = 0
    fraction: float = 0.


def _number(value, low=None, high=None):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (low is None or value >= low)
            and (high is None or value <= high))


def _stamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone_aware_timestamp_required')
    return result.astimezone(config.IST)


def _fresh(value, now, limit):
    try:
        return 0 <= (now - _stamp(value)).total_seconds() <= limit
    except (ValueError, TypeError):
        return False


def _clock(now, value):
    return datetime.combine(now.date(), time.fromisoformat(value), now.tzinfo)


def _event(name, state=None, **values):
    if state is not None:
        values = dict(signal_id=state.signal_id, regime=state.regime, phase=state.phase,
                      R=state.r_value, premium=state.peak_premium,
                      spot=None, stop=state.active_stop, **values)
    audit.log(name, **values)


def _reject(reason):
    _event('stop_reject', reason=reason, regime=None, phase=None, R=None,
           premium=None, spot=None)
    return Reject(reason)


MANUAL_SOURCES = ('manual', 'manual_adopted', 'MANUAL_ADOPTED')


def assign_regime(signal, market, modifiers=None):
    """Explicit source attribution; manual positions keep Intraday before CAS."""
    modifiers = modifiers or config.SESSION_MODIFIERS
    source = signal.get('source')
    if source in MANUAL_SOURCES:
        return 'INTRADAY'  # User policy overrides the pre-expiry Scalp modifier.
    if source in ('cas_blast',):
        regime = 'CAS'
    elif source in ('gamma_blast', 'gamma_pre_entry'):
        regime = 'GAMMA'
    elif source in ('scalp', 'index_scalp', 'fast_momentum'):
        regime = 'SCALP'
    elif source in ('bull_shield', 'bear_shield', 'sniper'):
        regime = 'INTRADAY'
    else:
        return Reject('explicit_supported_signal_regime_required')
    if market.get('expiry_minus_one') and signal.get('score', 0) < 90:
        if regime == 'GAMMA':
            choice = modifiers['EXPIRY_MINUS_1']['gamma_below90_regime']
            if choice not in ('GAMMA', 'SCALP'):
                return Reject('unresolved_preexpiry_gamma_regime')
            regime = choice
        elif regime == 'INTRADAY':
            regime = 'SCALP'
    return regime


def initial_stop(signal, chain_row, market, *, regimes=None, modifiers=None):
    """Return StopPlan or Reject without changing the selected contract.

    risk_budget_inr is supplied by the existing approved capital policy; this
    function never infers the user's live capital, increases it, or selects a
    cheaper strike. CAS additionally requires a resolved daily trade cap.
    """
    try:
        return _initial_stop(signal, chain_row, market, regimes or config.STOP_REGIMES,
                             modifiers or config.SESSION_MODIFIERS)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return _reject('invalid_stop_input:' + str(exc))


def _initial_stop(signal, row, market, regimes, modifiers):
    now = _stamp(market['now'])
    limit = config.STOP_ENGINE['max_input_age_seconds']
    if not _fresh(row['ts'], now, limit) or not _fresh(market['ts'], now, limit):
        return _reject('stale_stop_inputs')
    if market.get('macro_event'):
        return _reject('macro_event_entry_suppressed')
    if not _number(signal.get('score'), 0, 100):
        return _reject('invalid_signal_score')
    regime = assign_regime(signal, market, modifiers)
    if isinstance(regime, Reject):
        return _reject(regime.reason)
    p = copy.deepcopy(regimes[regime])
    p['structure_guard'] = copy.deepcopy(config.STRUCTURE_GUARD)
    side = signal['direction']
    if side not in ('CE', 'PE'):
        return _reject('invalid_option_direction')
    entry, bid, ask = row['entry'], row['bid'], row['ask']
    if not all(_number(v, 0) and v > 0 for v in (entry, bid, ask)) or ask < bid:
        return _reject('invalid_entry_or_depth')
    lot = row['lot_size']
    if type(lot) is not int or lot <= 0 or not row.get('token') or not row.get('exchange'):
        return _reject('invalid_contract')
    if not _number(signal.get('costs_per_unit'), 0):
        return _reject('cost_estimate_required')
    expiry = date.fromisoformat(str(market['expiry']))
    if expiry < now.date():
        return _reject('expired_contract')
    if market.get('expiry_minus_one') and expiry <= now.date():
        return _reject('invalid_expiry_session')
    session = dict(expiry_day=expiry == now.date(), expiry_minus_one=bool(market.get('expiry_minus_one')),
                   modifiers=copy.deepcopy(modifiers))
    if 'cutoff' in p and now >= _clock(now, p['cutoff']):
        return _reject('regime_entry_cutoff')
    # The caller must provide verified per-product timing, not a weekday guess.
    hard_exit = _stamp(market['session_close'] if regime == 'CAS' else market['hard_exit'])
    if hard_exit.date() != now.date() or now >= hard_exit:
        return _reject('invalid_or_elapsed_hard_exit')
    spread = ask - bid
    pre = signal.get('source') == 'gamma_pre_entry' and regime == 'GAMMA'
    if pre:
        p['max_sl_pct'] = p['preentry_max_sl_pct']
    invalidation = None
    if regime == 'CAS':
        cap = p['max_trades_per_day']
        budget = signal.get('cas_risk_budget_inr', p['risk_budget_inr'])
        if type(cap) is not int or cap <= 0 or not _number(budget, 0) or budget <= 0:
            return _reject('unresolved_cas_risk_sleeve')
        trades = signal.get('cas_trades_today')
        if type(trades) is not int or not 0 <= trades < cap:
            return _reject('cas_daily_trade_cap')
        if now < _clock(now, p['start']):
            return _reject('cas_window_not_open')
        broker = _stamp(market['broker_squareoff'])
        if broker.date() != now.date():
            return _reject('invalid_broker_squareoff_date')
        hard_exit = min(hard_exit, _clock(now, config.CAS_HARD_EXIT),
                        broker - timedelta(seconds=config.STOP_ENGINE['broker_buffer_seconds']))
        if now >= hard_exit:
            return _reject('cas_window_closed')
        if spread / entry > p['max_spread_pct']:
            return _reject('cas_spread_too_wide')
        stop = market['trigger_bar_low']
        if not _number(stop, 0) or not 0 < stop < entry:
            return _reject('invalid_cas_trigger_bar_low')
        risk_distance = entry  # Full premium at risk, not the technical stop.
    else:
        budget = signal['risk_budget_inr']
        atr = market['atr_premium']
        if not _number(atr, 0) or atr <= 0 or market['premium_bar_seconds'] != p['bar_seconds']:
            return _reject('valid_regime_atr_required')
        floor = max(p['noise_atr'] * atr, config.STOP_ENGINE['min_spread_multiple'] * spread)
        ceiling = p['max_sl_pct'] * entry
        if floor > ceiling:
            return _reject('stop_noise_floor_above_ceiling')
        if regime == 'SCALP':
            distance = max(floor, p['min_sl_pct'] * entry)
        else:
            spot, spot_atr = market['spot'], market['atr_spot_5m']
            if not _number(spot, 0) or spot <= 0 or not _number(spot_atr, 0) or spot_atr <= 0:
                return _reject('valid_spot_structure_required')
            if regime == 'GAMMA':
                level = market['oi_wall']
            else:
                vwap, ema = market['vwap'], market['ema20']
                if not all(_number(v, 0) and v > 0 for v in (vwap, ema)):
                    return _reject('valid_vwap_ema_required')
                level = min(vwap, ema) if side == 'CE' else max(vwap, ema)
                swing = market.get('swing')
                if _number(swing, 0):
                    if side == 'CE' and level < swing < spot:
                        level = swing
                    elif side == 'PE' and spot < swing < level:
                        level = swing
            if not _number(level, 0) or level <= 0:
                return _reject('valid_invalidation_level_required')
            invalidation = level + (-1 if side == 'CE' else 1) * p['spot_buffer'] * spot_atr
            ds = invalidation - spot
            delta, gamma, vega = row['delta'], row['gamma'], row['vega']
            crush = market['expected_iv_crush_points']
            if (not _number(delta, -1, 1) or not _number(gamma, 0)
                    or not _number(vega, 0) or not _number(crush, 0)
                    or (side == 'CE' and (delta <= 0 or ds >= 0))
                    or (side == 'PE' and (delta >= 0 or ds <= 0))):
                return _reject('invalid_signed_greeks_or_structure')
            structure = entry + delta * ds + .5 * gamma * ds * ds - vega * crush
            if not math.isfinite(structure) or structure >= entry:
                return _reject('invalid_taylor_stop_projection')
            distance = min(ceiling, max(floor, entry - structure))
        stop = entry - distance
        risk_distance = distance
    if not _number(budget, 0) or budget <= 0:
        return _reject('risk_budget_required')
    # Reserve any requested room BEFORE sizing. With no explicit reserve, the
    # initial stop remains the hard floor; forecasts cannot increase that risk.
    hard_stop = None
    if regime != 'CAS':
        hard_stop = signal.get('hard_stop_premium', stop)
        if not _number(hard_stop, entry * (1 - p['max_sl_pct']), stop):
            return _reject('hard_stop_outside_regime_limits')
        if 'hard_stop_premium' in signal:
            risk_distance = entry - hard_stop + signal['costs_per_unit']
    elif 'hard_stop_premium' in signal:
        return _reject('cas_hard_stop_override_unsupported')
    manual_start = None
    if signal.get('source') in MANUAL_SOURCES:
        # Persist the existing CAS boundary in IST, not in the caller's offset.
        local = now.astimezone(config.IST)
        p['manual_policy'] = copy.deepcopy(config.MANUAL_STOP_POLICY)
        manual_start = _clock(local, p['manual_policy']['cas_start'])
        # No-stop permission requires reserving full premium, even before CAS.
        # This planner never adopts or resizes an already-open broker position.
        risk_distance = entry + signal['costs_per_unit']
    max_lots, capital = signal['max_lots'], signal['available_capital_inr']
    if type(max_lots) is not int or max_lots <= 0 or not _number(capital, 0):
        return _reject('invalid_capital_or_lot_limit')
    lots = min(math.floor(budget / (risk_distance * lot)),
               math.floor(capital / (entry * lot)), max_lots, p.get('max_lots', max_lots))
    if lots < 1:
        return _reject('risk_budget_stop_too_wide')
    plan = StopPlan(regime, side, str(row['token']), str(row['exchange']), entry, stop,
                    entry - stop, lots, lot, spread, invalidation, now.isoformat(),
                    hard_exit.isoformat(), expiry.isoformat(), p, session, signal['score'],
                    signal['costs_per_unit'], pre, signal.get('source') == 'sniper',
                    bool(signal.get('recovery_session')), str(signal.get('asset', '')), hard_stop,
                    signal.get('source', ''), manual_start.isoformat() if manual_start else None)
    _event('stop_initial', regime=regime, phase='INITIAL', R=plan.r_value,
           premium=entry, spot=market.get('spot'), stop=stop, reason='draft_plan', lots=lots)
    return plan


def open_position(plan, signal_id, quantity=None):
    if not isinstance(plan, StopPlan):
        raise ValueError('valid_stop_plan_required')
    qty = plan.allowed_lots * plan.lot_size if quantity is None else quantity
    if type(qty) is not int or not 0 < qty <= plan.allowed_lots * plan.lot_size or qty % plan.lot_size:
        raise ValueError('invalid_confirmed_whole_lot_quantity')
    return StopPosition(signal_id, copy.deepcopy(plan), qty, peak_premium=plan.entry_price,
                        active_stop=plan.initial_stop, last_high_ts=plan.entry_ts)


def _decision(state, action, reason, fraction=0.):
    if action in ('EXIT', 'PARTIAL'):
        if action == 'EXIT' and state.structure_guard.get('status') in ('ARMED', 'ACTIVE'):
            structure_guard.finish(state, reason)
        if state.pending:
            if action == 'EXIT' and state.pending['action'] == 'PARTIAL':
                # Do not overwrite the fill cursor of an outstanding partial.
                # The execution adapter must cancel/reconcile it before the
                # queued full exit can sell the actual remaining quantity.
                state.exit_after_pending = state.exit_after_pending or reason
                return StopDecision('HOLD', 'full_exit_waiting_for_pending_resolution', state.active_stop)
            return _pending(state)
        qty = state.remaining_qty if action == 'EXIT' else min(state.remaining_qty,
            math.ceil(state.remaining_qty * fraction / state.plan.lot_size) * state.plan.lot_size)
        if qty == state.remaining_qty:
            action = 'EXIT'
        d = StopDecision(action, reason, state.active_stop, qty,
                         qty / state.remaining_qty if state.remaining_qty else 0.)
        state.pending = asdict(d)
        _event('stop_exit' if action == 'EXIT' else 'stop_partial', state, reason=reason, quantity=qty)
        return d
    return StopDecision(action, reason, state.active_stop)


def _pending(state):
    return StopDecision(**state.pending) if state.pending else None


def _ratchet(state, candidate, phase):
    previous = state.active_stop
    phases = ('INITIAL', 'BREAKEVEN', 'TRAIL', 'LOCK')
    if phases.index(phase) > phases.index(state.phase):
        state.phase = phase
        _event('stop_phase_change', state, reason=phase)
    state.active_stop = max(previous, candidate)
    if phase in ('BREAKEVEN', 'LOCK'):
        # Breakeven and earned profit floors remain hard even when an ATR
        # technical trail has a valid, temporary structure exception.
        state.protected_stop = max(state.protected_stop, candidate)
    if state.active_stop > previous:
        _event('stop_moved', state, reason=phase, previous_stop=previous)


def _once(state, name, ts):
    stamp = _stamp(ts)
    previous = state.seen.get(name)
    if previous and stamp <= _stamp(previous):
        return False
    state.seen[name] = stamp.isoformat()
    return True


def _time_factor(state):
    return (state.plan.session['modifiers']['EXPIRY']['time_factor']
            if state.plan.session['expiry_day'] else 1.)


def _trail_factor(state, now):
    s, p = state.plan.session, state.plan.params
    factor = s['modifiers']['EXPIRY']['trail_factor'] if s['expiry_day'] else 1.
    if state.regime == 'INTRADAY':
        if s['expiry_minus_one']:
            factor *= s['modifiers']['EXPIRY_MINUS_1']['intraday_trail_factor']
        theta = s['modifiers']['EXPIRY']['theta_start'] if s['expiry_day'] else p['theta_start']
        if now >= _clock(now, theta):
            factor *= p['theta_factor']
    return factor


def _partial(state, reason, fraction, stop=None):
    if reason in state.completed:
        return None
    if stop is not None:
        _ratchet(state, stop, 'BREAKEVEN')
    return _decision(state, 'PARTIAL', reason, fraction)


def _manual_cas_decision(state, now):
    if (state.plan.source not in MANUAL_SOURCES or state.plan.manual_cas_start is None
            or now < _stamp(state.plan.manual_cas_start)):
        return None
    if state.structure_guard.get('status') in ('ARMED', 'ACTIVE'):
        structure_guard.finish(state, 'manual_cas_no_stop')
    # Do not cancel or forget an order already in flight.
    pending = _pending(state)
    if pending:
        return pending
    return StopDecision('HOLD', 'manual_cas_no_stop', None)


def update_stop(state, tick, market):
    """Ratchet a draft state and return a decision; never submit an order.

    A latched exit cannot disappear on rebound. Ticks require contract identity;
    stale ATR/structure cannot raise a stop, but a fresh price can hit its lock.
    A hard deadline can exit even with no usable quote (execution must price it).
    """
    now = _stamp(market['now'])
    if state.remaining_qty <= 0:
        return _decision(state, 'HOLD', 'position_closed')
    if now >= _stamp(state.plan.hard_exit):
        return _decision(state, 'EXIT', 'cas_hard_exit' if state.regime == 'CAS' else 'session_hard_exit')
    if state.pending and state.pending['action'] == 'EXIT':
        return _pending(state)
    manual = _manual_cas_decision(state, now)
    if manual:
        return manual
    guard_active = state.structure_guard.get('status') == 'ACTIVE'
    if guard_active and now >= _stamp(state.structure_guard['deadline']):
        return _decision(state, 'EXIT', 'structure_grace_expired')
    if (str(tick.get('token')) != state.plan.token or tick.get('exchange') != state.plan.exchange
            or not _number(tick.get('ltp'), 0) or tick['ltp'] <= 0
            or not _fresh(tick.get('ts'), now, config.STOP_ENGINE['max_input_age_seconds'])):
        if guard_active:
            reason = structure_guard.watchdog(state, market, now)
            if reason:
                return _decision(state, 'EXIT', reason)
        return _pending(state) or _decision(state, 'HOLD', 'invalid_or_stale_tick')
    ts, price = _stamp(tick['ts']), tick['ltp']
    if state.last_ts and ts <= _stamp(state.last_ts):
        if guard_active:
            reason = structure_guard.watchdog(state, market, now)
            if reason:
                return _decision(state, 'EXIT', reason)
        return _pending(state) or _decision(state, 'HOLD', 'duplicate_or_out_of_order_tick')
    if ts < _stamp(state.plan.entry_ts):
        return _decision(state, 'HOLD', 'tick_before_entry')
    previous_tick = _stamp(state.last_ts) if state.last_ts else None
    state.last_ts = ts.isoformat()
    if price > state.peak_premium:
        state.peak_premium, state.last_high_ts = price, ts.isoformat()
    p, e, peak = state.plan.params, state.plan.entry_price, state.peak_premium
    age = (now - _stamp(state.plan.entry_ts)).total_seconds()
    if age < 0:
        return _decision(state, 'HOLD', 'tick_before_entry')
    fresh = _fresh(market.get('ts'), now, config.STOP_ENGINE['max_input_age_seconds'])
    atr = market.get('atr_premium')
    atr_ok = fresh and _number(atr, 0) and atr > 0 and market.get('premium_bar_seconds') == p['bar_seconds']
    before = state.active_stop
    partial = None
    if state.regime == 'INTRADAY':
        gain = (peak - e) / state.r_value
        if gain >= p['breakeven_r']:
            _ratchet(state, e + state.plan.costs, 'BREAKEVEN')
        if gain >= p['trail_r']:
            if atr_ok:
                multiple = p['sniper_trail_atr'] if state.plan.sniper else p['trail_atr']
                if gain >= p['tighten_r']:
                    multiple = p['sniper_tight_atr'] if state.plan.sniper else p['tight_atr']
                _ratchet(state, peak - multiple * atr * _trail_factor(state, now), 'TRAIL')
            giveback = p['sniper_giveback'] if state.plan.sniper else p['giveback']
            _ratchet(state, e + (peak - e) * (1 - giveback), 'LOCK')
        if gain >= p['partial_r'] and state.plan.score < p['partial_below_score']:
            partial = ('intraday_t1', p['partial_fraction'], e + state.plan.costs)
        if (state.phase == 'INITIAL' and age >= p['initial_timeout_seconds'] * _time_factor(state)
                and (price - e) / state.r_value < p['timeout_gain_r']):
            return _decision(state, 'EXIT', 'intraday_initial_timeout')
    elif state.regime == 'SCALP':
        if peak >= e * (1 + p['breakeven_pct']):
            _ratchet(state, e + state.plan.costs, 'BREAKEVEN')
        if peak >= e * (1 + p['t1_pct']):
            _ratchet(state, e * (1 + p['t1_lock_pct']), 'TRAIL')
            if atr_ok:
                _ratchet(state, peak - p['trail_atr'] * atr * _trail_factor(state, now), 'TRAIL')
            partial = ('scalp_t1', p['t1_fraction'], None)
        if peak >= e * (1 + p['t2_pct']):
            score = market.get('momentum_score') if fresh else None
            if not _number(score, p['hold_score'], 100):
                return _decision(state, 'EXIT', 'scalp_t2')
        if age >= p['timeout_seconds'] * _time_factor(state) and peak <= e:
            return _decision(state, 'EXIT', 'scalp_no_favourable_move')
        bid, ask = tick.get('bid'), tick.get('ask')
        if _number(bid, 0) and _number(ask, 0) and 0 < bid <= ask and state.plan.entry_spread > 0:
            wide = ask - bid > p['spread_multiple'] * state.plan.entry_spread
            consecutive = previous_tick and (ts - previous_tick).total_seconds() <= config.STOP_ENGINE['max_input_age_seconds']
            state.counters['spread'] = (state.counters.get('spread', 0) + 1 if consecutive else 1) if wide else 0
            if state.counters['spread'] >= p['spread_snapshots']:
                return _decision(state, 'EXIT', 'scalp_spread_widened')
    elif state.regime == 'GAMMA':
        if state.plan.pre_entry and not state.graduated:
            if fresh and market.get('gamma_confirmed') is True and age <= p['preentry_seconds'] * _time_factor(state):
                state.graduated = True
                # Confirmed ceiling may be looser, but an existing stop never falls.
                _event('stop_phase_change', state, reason='gamma_graduated_stop_preserved')
            elif age >= p['preentry_seconds'] * _time_factor(state):
                return _decision(state, 'EXIT', 'gamma_preentry_not_confirmed')
        m = peak / e
        rows = p['locks']
        if m >= rows[0][0]:
            lock = p['peak_lock'] * peak
            if m < rows[-1][0]:
                for (x0, y0), (x1, y1) in zip(rows, rows[1:]):
                    if x0 <= m < x1:
                        lock = e * (y0 + (m-x0) / (x1-x0) * (y1-y0))
                        break
            _ratchet(state, lock, 'BREAKEVEN' if m < p['chandelier_start'] else 'LOCK')
        if m >= p['chandelier_start'] and atr_ok:
            _ratchet(state, peak - p['trail_atr'] * atr * _trail_factor(state, now), 'LOCK')
    else:  # CAS: structure is checked on completed 15-second bars, not each tick.
        if peak >= e * p['free_ride_multiple']:
            giveback = p['tight_giveback'] if peak > e * p['tighten_multiple'] else p['giveback']
            _ratchet(state, peak * (1 - giveback), 'TRAIL')
            partial = ('cas_free_ride', p['partial_fraction'], None)
        if (now - _stamp(state.last_high_ts)).total_seconds() >= p['stall_seconds'] * _time_factor(state):
            return _decision(state, 'EXIT', 'cas_no_new_high')
        bar = market.get('premium_bar')
        if fresh and bar and bar.get('seconds') == p['bar_seconds'] and bar.get('closed') is True:
            if _fresh(bar.get('ts'), now, p['bar_seconds']) and _number(bar.get('close'), 0):
                if _once(state, 'cas_bar', bar['ts']) and bar['close'] < state.plan.initial_stop:
                    return _decision(state, 'EXIT', 'cas_trigger_bar_low_broken')
    if fresh and market.get('macro_event') and (price-e) / state.r_value >= state.plan.session['modifiers']['MACRO']['breakeven_r']:
        _ratchet(state, e + state.plan.costs, 'BREAKEVEN')
    # Only the supported structure policy changes the trigger path. CAS and
    # other unconfigured regimes preserve their existing bar/price semantics.
    if structure_guard.policy(state):
        executable = min(price, tick['bid']) if structure_guard.depth(tick) else price
        if executable <= structure_guard.hard_floor(state):
            return _decision(state, 'EXIT', 'hard_risk_floor_breached')
        previous_guard = copy.deepcopy(state.structure_guard)
        try:
            structure_guard.prepare(state, tick, market, now)
            guarded = structure_guard.evaluate(state, tick, market, now)
        except (KeyError, TypeError, ValueError, OverflowError):
            # Bad auxiliary data cannot turn a stop into permission to hold.
            was_active = previous_guard.get('status') == 'ACTIVE'
            state.structure_guard = previous_guard
            guarded = ('EXIT', 'invalid_structure_guard_input') if was_active else None
        if state.structure_guard != previous_guard:
            _event('structure_guard_changed', state, guard=copy.deepcopy(state.structure_guard))
        if guarded:
            return _decision(state, *guarded)
    # CAS INITIAL uses a closed-bar technical stop. Once trailed, its ratchet is tick-driven.
    if price <= state.active_stop and (state.regime != 'CAS' or state.phase != 'INITIAL'):
        return _decision(state, 'EXIT', 'regime_price_stop')
    if state.pending:
        return _pending(state)
    if partial:
        result = _partial(state, *partial)
        if result:
            return result
    return _decision(state, 'MOVE_STOP' if state.active_stop > before else 'HOLD', 'regime_ratchet')


def thesis_check(state, signals):
    """Only fresh, distinct completed bars/polls can advance confirmation counters.

    Signal booleans are explicit normalized evidence, never inferred from a
    missing field. Full thesis failures have priority over partial requests.
    """
    now = _stamp(signals['now'])
    if state.remaining_qty <= 0:
        return _decision(state, 'HOLD', 'position_closed')
    if now >= _stamp(state.plan.hard_exit):
        return _decision(state, 'EXIT', 'session_hard_exit')
    if state.pending and state.pending['action'] == 'EXIT':
        return _pending(state)
    manual = _manual_cas_decision(state, now)
    if manual:
        return manual
    if not _fresh(signals.get('ts'), now, config.STOP_ENGINE['thesis_max_age_seconds']):
        return _pending(state) or _decision(state, 'HOLD', 'stale_thesis')
    p, e = state.plan.params, state.plan.entry_price
    full, partial = None, None
    bar = signals.get('bar')
    if (bar and bar.get('closed') is True and bar.get('seconds') == p['bar_seconds']
            and _fresh(bar.get('ts'), now, p['bar_seconds'])
            and _once(state, 'thesis_bar', bar['ts'])):
        previous = state.seen.get('adverse_bar')
        consecutive = previous is not None and (_stamp(bar['ts']) - _stamp(previous)).total_seconds() == p['bar_seconds']
        state.seen['adverse_bar'] = _stamp(bar['ts']).isoformat()
        adverse = signals.get('vwap_ema_adverse') is True
        state.counters['adverse'] = (state.counters.get('adverse', 0) + 1 if consecutive else 1) if adverse else 0
        if state.regime == 'INTRADAY':
            if signals.get('shield_invalidated') is True or state.counters['adverse'] >= p['confirmation_bars']:
                full = 'shield_invalidated_confirmed'
            elif _number(signals.get('shield_score'), 0, 100) and signals['shield_score'] < p['shield_exit_below']:
                full = 'shield_score_below_floor'
            elif state.plan.recovery and signals.get('rsi_crossed_against') is True:
                full = 'recovery_rsi_crossed_against'
        elif state.regime == 'SCALP':
            age = (_stamp(bar['ts']) - _stamp(state.plan.entry_ts)).total_seconds()
            if state.counters['adverse'] >= p['confirmation_bars'] and age <= p['confirmation_bars'] * p['bar_seconds']:
                full = 'scalp_early_alignment_flip'
            elif signals.get('premium_lower_high') is True and signals.get('opposite_not_decaying') is True:
                full = 'scalp_lower_high_opposite_strength'
            elif _number(signals.get('momentum_score'), 0, 100) and signals['momentum_score'] < p['momentum_exit_below']:
                full = 'scalp_momentum_failed'
        elif state.regime == 'GAMMA':
            accel, move, atr = signals.get('acceleration'), signals.get('spot_move'), signals.get('atr_spot_5m')
            stalled = (_number(accel) and accel < 0 and _number(move) and _number(atr, 0)
                       and atr > 0 and abs(move) < p['spot_stall_atr'] * atr)
            state.counters['accel'] = (state.counters.get('accel', 0) + 1 if consecutive else 1) if stalled else 0
            if state.counters['accel'] >= p['acceleration_bars']:
                partial = ('gamma_acceleration_stalled', p['half_fraction'], e)
    previous_poll = state.seen.get('gamma_poll')
    if state.regime == 'GAMMA' and _once(state, 'gamma_poll', signals['ts']):
        if (_number(signals.get('wall_oi_growth'), p['wall_strength_pct'])
                and signals.get('wall_window_seconds') == p['wall_window_seconds']):
            full = 'gamma_wall_restrengthened'
        flipped = signals.get('oi_velocity_flipped') is True
        consecutive = previous_poll and (_stamp(signals['ts']) - _stamp(previous_poll)).total_seconds() <= config.STOP_ENGINE['thesis_max_age_seconds']
        state.counters['oi_flip'] = (state.counters.get('oi_flip', 0) + 1 if consecutive else 1) if flipped else 0
        if state.counters['oi_flip'] >= p['oi_flip_polls']:
            partial = ('gamma_oi_velocity_reversed', p['half_fraction'], e)
        iv = signals.get('iv_points')
        if _number(iv, 0) and iv > 0:
            state.iv_peak = max(state.iv_peak or iv, iv)
            if state.iv_peak - iv >= p['iv_crush_points']:
                partial = ('gamma_iv_crush', p['half_fraction'], None)
    if state.regime in ('INTRADAY', 'GAMMA'):
        if (_number(signals.get('opposing_wall_ratio'), p['opposing_wall_ratio'])
                and _number(signals.get('opposing_wall_distance'), 0, p['opposing_wall_points'])):
            partial = ('opposing_wall_built', p.get('opposing_fraction', p.get('partial_fraction')),
                       e + state.plan.costs if state.regime == 'INTRADAY' else None)
    if full:
        return _decision(state, 'EXIT', full)
    if state.pending:
        return _pending(state)
    if partial:
        result = _partial(state, *partial)
        if result:
            return result
    return _decision(state, 'HOLD', 'thesis_intact_or_incomplete')


def acknowledge_fill(state, cumulative_quantity, *, terminal):
    """Consume confirmed cumulative fills for the CURRENT decision only.

    Caller must associate its durable order intent with this pending decision.
    A cancelled unfilled remainder stays latched; no inferred fill or double sell.
    """
    if not state.pending:
        raise ValueError('no_pending_stop_decision')
    filled = state.counters.get('pending_filled', 0)
    target = state.pending['quantity']
    if type(cumulative_quantity) is not int or not filled <= cumulative_quantity <= target:
        raise ValueError('invalid_cumulative_fill')
    delta = cumulative_quantity - filled
    if delta > state.remaining_qty:
        raise ValueError('fill_exceeds_remaining')
    state.remaining_qty -= delta
    state.counters['pending_filled'] = cumulative_quantity
    if terminal:
        pending = state.pending
        state.pending = None
        state.counters.pop('pending_filled', None)
        if cumulative_quantity == target:
            state.completed.append(pending['reason'])
        if state.exit_after_pending and state.remaining_qty:
            reason, state.exit_after_pending = state.exit_after_pending, None
            _decision(state, 'EXIT', reason)
        elif cumulative_quantity < target:
            # A new, independently tagged retry would have its own zero-based
            # cumulative fill cursor. Never replay the old order's terminal fill.
            pending['quantity'] = target - cumulative_quantity
            pending['fraction'] = pending['quantity'] / state.remaining_qty
            state.pending = pending


def exit_limit(state, quote, now, attempt=0):
    """Pure LIMIT price proposal. Repricing scheduling/acknowledgement is external."""
    now = _stamp(now)
    if (quote.get('exchange') != state.plan.exchange or str(quote.get('token')) != state.plan.token
            or not _fresh(quote.get('ts'), now, config.STOP_ENGINE['max_input_age_seconds'])):
        return Reject('fresh_exit_depth_required')
    bid, ask, tick = quote.get('bid'), quote.get('ask'), quote.get('tick_size')
    if not all(_number(v, 0) and v > 0 for v in (bid, ask, tick)) or ask < bid:
        return Reject('valid_exit_depth_and_tick_required')
    if type(attempt) is not int or not 0 <= attempt <= config.STOP_ENGINE['max_reprices']:
        return Reject('invalid_reprice_attempt')
    n = 0 if attempt == config.STOP_ENGINE['max_reprices'] else state.plan.params['limit_ticks']
    step = Decimal(str(tick))
    price = ((Decimal(str(bid)) - n * step) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return float(price) if price > 0 else Reject('exit_bid_below_tick_floor')


def save_state(state, connection):
    connection.execute('CREATE TABLE IF NOT EXISTS regime_stop_states '
                       '(signal_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    payload = json.dumps(dict(version=config.STOP_ENGINE['version'], state=asdict(state)), allow_nan=False)
    with connection:
        connection.execute('INSERT OR REPLACE INTO regime_stop_states VALUES (?,?)', (state.signal_id, payload))


def load_state(signal_id, connection):
    connection.execute('CREATE TABLE IF NOT EXISTS regime_stop_states '
                       '(signal_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    row = connection.execute('SELECT payload FROM regime_stop_states WHERE signal_id=?', (signal_id,)).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    if payload['version'] not in (1, 2, config.STOP_ENGINE['version']):
        raise ValueError('unsupported_stop_state_version')
    data = payload['state']
    if payload['version'] == 1:
        # Older snapshots cannot prove unused permission or distinguish locks.
        data['protected_stop'] = data['active_stop']
        data['structure_guard'] = dict(status='SPENT', used=True, end_reason='legacy_state')
    data['plan'] = StopPlan(**data['plan'])
    state = StopPosition(**data)
    if state.plan.source in MANUAL_SOURCES:
        entry = _stamp(state.plan.entry_ts).astimezone(config.IST)
        start = _stamp(state.plan.manual_cas_start).astimezone(config.IST)
        policy = state.plan.params.get('manual_policy', {})
        if (policy.get('version') != 1 or policy.get('full_premium_risk') is not True
                or start != _clock(entry, policy.get('cas_start'))
                or state.plan.regime != 'INTRADAY'):
            raise ValueError('invalid_persisted_manual_policy')
    elif state.plan.manual_cas_start is not None:
        raise ValueError('manual_policy_requires_manual_source')
    if (state.signal_id != signal_id or state.phase not in ('INITIAL', 'BREAKEVEN', 'TRAIL', 'LOCK')
            or not _number(state.active_stop, state.plan.initial_stop)
            or not _number(state.peak_premium, state.plan.entry_price)
            or not _number(state.r_value, 0) or state.r_value <= 0
            or not _number(state.protected_stop, 0, state.active_stop)
            or type(state.remaining_qty) is not int or state.remaining_qty < 0):
        raise ValueError('invalid_persisted_stop_state')
    if state.plan.hard_stop_premium is not None and not _number(
            state.plan.hard_stop_premium,
            state.plan.entry_price * (1 - state.plan.params['max_sl_pct']), state.plan.initial_stop):
        raise ValueError('invalid_persisted_hard_stop')
    structure_guard.validate_saved(state)
    return state


def restore_missing_state(plan, signal_id, remaining_qty, persisted_stop=None):
    """Explicit fallback for already classified exposure; never guess its regime.

    The caller must reconcile quantity and persist this before enabling decisions.
    A known older stop remains binding even when the richer state is missing.
    CAS has full-premium risk; its supplied trigger-bar stop remains in force.
    """
    state = open_position(plan, signal_id, remaining_qty)
    ceiling = plan.entry_price * (1 - plan.params['max_sl_pct'])
    state.active_stop = max(ceiling, persisted_stop or 0, plan.initial_stop if plan.regime == 'CAS' else 0)
    # Preserve the tighter planned initial stop for restart validation/ratcheting.
    state.active_stop = max(state.active_stop, plan.initial_stop)
    state.protected_stop = state.active_stop
    state.structure_guard = dict(status='SPENT', used=True, end_reason='missing_state')
    _event('stop_initial', state, reason='missing_state_restored_protected')
    return state
