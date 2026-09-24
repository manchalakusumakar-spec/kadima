"""Offline, single-use support/resistance exception for a technical stop.

No forecast generation, calibration claims, feeds or broker calls. The operator
validation registry is separate from market input. A nominal stop remains
monotonic; an explicit, persisted exception temporarily suspends ONLY that stop.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import config

EVENT = 'option_recovery_before_exit'
REFERENCES = {'pivot', 'vwap', 'previous_high', 'previous_low', 'ema', 'fibonacci', 'oi_wall'}


def number(x, low=None, high=None):
    return (type(x) in (int, float) and math.isfinite(x)
            and (low is None or x >= low) and (high is None or x <= high))


def stamp(x):
    t = datetime.fromisoformat(x) if isinstance(x, str) else x
    if not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('aware_guard_timestamp_required')
    return t


def fresh(x, now, seconds):
    try:
        return 0 <= (now - stamp(x)).total_seconds() <= seconds
    except (ValueError, TypeError):
        return False


def hard_floor(state):
    initial = state.plan.hard_stop_premium
    return max(state.plan.initial_stop if initial is None else initial, state.protected_stop)


def policy(state):
    p = state.plan.params.get('structure_guard', {})
    if state.regime not in p.get('regimes', ()) or state.plan.asset not in p.get('assets', ()):
        return None
    return p


def depth(tick):
    bid, ask = tick.get('bid'), tick.get('ask')
    return number(bid, 0) and number(ask, 0) and 0 < bid <= ask


def spot_valid(state, market, now):
    return (market.get('asset') == state.plan.asset and number(market.get('spot'), 0)
            and market['spot'] > 0
            and fresh(market.get('ts'), now, config.STOP_ENGINE['max_input_age_seconds']))


def prepare(state, tick, market, now):
    """Select a qualified zone BEFORE a stop touch; freeze the selected boundary.

    Fresh signed Greeks map the premium stop to an approximate spot location.
    The projected premium at the buffered boundary must fit the reserved risk.
    An unrepresentable/wide stop is rejected, never clipped into the zone.
    """
    p = policy(state)
    g = state.structure_guard
    if (not p or g.get('used') or state.pending or not config.STRUCTURE_GUARD_VALIDATIONS
            or not depth(tick) or tick['bid'] <= state.active_stop
            or not spot_valid(state, market, now) or market.get('macro_event')):
        return
    if g.get('status') == 'ARMED' and g['technical_stop_premium'] == state.active_stop:
        return
    # A changed technical stop needs a newly qualified, pre-touch candidate.
    state.structure_guard = {}
    z, r = market.get('structure'), market.get('option_risk')
    if not isinstance(z, dict) or not isinstance(r, dict):
        return
    if (z.get('asset') != state.plan.asset or not isinstance(z.get('id'), str) or not z['id']
            or z.get('role') != ('support' if state.plan.direction == 'CE' else 'resistance')
            or z.get('bar_seconds') != p['structure_bar_seconds']
            or not fresh(z.get('ts'), now, p['structure_max_age_seconds'])
            or not fresh(z.get('reaction_at'), now, p['reaction_max_age_seconds'])
            or type(z.get('confirmed_reactions')) is not int or z['confirmed_reactions'] < 1
            or not isinstance(z.get('references'), list)):
        return
    refs = [x for x in z['references'] if isinstance(x, str) and x in REFERENCES
            and (x != 'oi_wall' or z.get('oi_valid') is True)]
    if not refs:
        return
    low, high, atr = z.get('low'), z.get('high'), market.get('atr_spot_5m')
    if not all(number(v, 0) and v > 0 for v in (low, high, atr)) or low > high:
        return
    if (str(r.get('token')) != state.plan.token or r.get('exchange') != state.plan.exchange
            or not fresh(r.get('ts'), now, p['structure_max_age_seconds'])):
        return
    delta, gamma, vega = r.get('delta'), r.get('gamma'), r.get('vega')
    iv_down, theta = r.get('iv_down_points'), r.get('theta_per_second')
    if (not number(delta, -1, 1) or delta == 0 or not number(gamma, 0)
            or not number(vega, 0) or not number(iv_down, 0) or not number(theta, high=0)
            or (delta > 0) != (state.plan.direction == 'CE')):
        return
    mid = (tick['bid'] + tick['ask']) / 2
    difference = state.active_stop - mid
    discriminant = delta * delta + 2 * gamma * difference
    if discriminant <= 0:
        return
    move = (difference / delta if gamma == 0 else
            2 * difference / (delta + math.copysign(math.sqrt(discriminant), delta)))
    spot_stop = market['spot'] + move
    distance = max(low - spot_stop, spot_stop - high, 0.)
    proximity = max(p['min_points'], p['proximity_atr'] * atr)
    buffer = max(p['min_points'], p['buffer_atr'] * atr)
    boundary = low - buffer if state.plan.direction == 'CE' else high + buffer
    ds = boundary - market['spot']
    if (distance > proximity or boundary <= 0
            or (state.plan.direction == 'CE' and not boundary < spot_stop < market['spot'])
            or (state.plan.direction == 'PE' and not boundary > spot_stop > market['spot'])
            or (delta + gamma * ds) * delta <= 0):
        return
    projected = mid + delta * ds + .5 * gamma * ds * ds - vega * iv_down + theta * p['grace_seconds']
    if not number(projected, hard_floor(state)) or projected >= state.active_stop:
        return
    state.structure_guard = dict(status='ARMED', used=False, armed_at=now.isoformat(),
        level_id=z['id'], low=low, high=high, references=refs, reaction_at=stamp(z['reaction_at']).isoformat(),
        technical_stop_premium=state.active_stop, technical_stop_spot=spot_stop,
        boundary_spot=boundary, projected_boundary_premium=projected,
        buffer_points=buffer, proximity_points=proximity)


def forecast_error(state, forecast, now, bid=None):
    """A confidence number supplied by a model cannot approve its own use."""
    p, g, f = policy(state), state.structure_guard, forecast
    if not p or not isinstance(f, dict):
        return 'forecast_missing'
    v = config.STRUCTURE_GUARD_VALIDATIONS.get(f.get('validation_id'))
    if not isinstance(v, dict) or v.get('status') != 'approved' or not v.get('report_id'):
        return 'forecast_unvalidated'
    if (not f.get('model_id') or v.get('model_id') != f['model_id']
            or v.get('event') != EVENT or f.get('event') != EVENT
            or v.get('asset') != state.plan.asset or f.get('asset') != state.plan.asset
            or v.get('regime') != state.regime or f.get('regime') != state.regime
            or f.get('direction') != state.plan.direction
            or f.get('signal_id') != state.signal_id or str(f.get('token')) != state.plan.token
            or f.get('exchange') != state.plan.exchange or f.get('level_id') != g.get('level_id')
            or f.get('boundary_spot') != g.get('boundary_spot')
            or f.get('technical_stop_premium') != g.get('technical_stop_premium')
            or f.get('hard_floor_premium') != hard_floor(state)):
        return 'forecast_context_mismatch'
    if not number(f.get('confidence_pct'), p['confidence_min_pct'], 100):
        return 'forecast_below_threshold_or_invalid'
    if g.get('status') == 'ACTIVE' and (f.get('model_id') != g['model_id']
            or f.get('validation_id') != g['validation_id']):
        return 'forecast_validation_changed'
    try:
        generated, inputs, deadline = (stamp(f[k]) for k in ('generated_at', 'market_data_at', 'outcome_deadline'))
        horizon = (deadline - generated).total_seconds()
        if (not stamp(v['valid_from']) <= now < stamp(v['valid_until'])
                or not number(v.get('max_horizon_seconds'), 1)
                or not 0 < horizon <= min(v['max_horizon_seconds'], p['grace_seconds'])
                or inputs > generated or deadline <= now
                or not fresh(generated, now, p['forecast_max_age_seconds'])
                or not fresh(inputs, now, p['forecast_max_age_seconds'])):
            return 'forecast_expired_or_stale'
        if g.get('status') == 'ACTIVE' and deadline != stamp(g['deadline']):
            return 'forecast_deadline_changed'
    except (ValueError, KeyError, TypeError):
        return 'forecast_timestamp_invalid'
    target, win, lose, costs = (f.get(k) for k in ('recovery_target_premium',
        'mean_success_premium', 'mean_failure_premium', 'remaining_costs_per_unit'))
    if (not all(number(x, 0) for x in (target, win, lose, costs))
            or target <= g['technical_stop_premium'] or win < target or lose >= target
            or costs < state.plan.costs):
        return 'forecast_payoff_invalid'
    if g.get('status') == 'ACTIVE' and target != g['recovery_target_premium']:
        return 'forecast_target_changed'
    if bid is not None:
        probability = f['confidence_pct'] / 100
        if probability * win + (1 - probability) * lose - bid - costs <= 0:
            return 'forecast_no_net_holding_advantage'
    return None


def finish(state, reason):
    g = state.structure_guard
    g.update(status='SPENT', used=True, end_reason=reason)


def confirmed_breach(state, market, now):
    p, g, bar = policy(state), state.structure_guard, market.get('spot_bar')
    if (p and isinstance(bar, dict) and bar.get('asset') == state.plan.asset
            and bar.get('closed') is True and bar.get('seconds') == p['confirmation_bar_seconds']
            and fresh(bar.get('ts'), now, p['confirmation_bar_seconds'])
            and stamp(bar['ts']) > stamp(g['armed_at']) and number(bar.get('close'), 0)):
        return (bar['close'] < g['boundary_spot'] if state.plan.direction == 'CE'
                else bar['close'] > g['boundary_spot'])
    return False


def watchdog(state, market, now):
    """Returns a latched exit reason even if no new option tick is available."""
    g = state.structure_guard
    if g.get('status') != 'ACTIVE':
        return None
    if now >= stamp(g['deadline']):
        finish(state, 'structure_grace_expired')
        return 'structure_grace_expired'
    if confirmed_breach(state, market, now):
        finish(state, 'structure_buffer_breached')
        return 'structure_buffer_breached'
    try:
        error = forecast_error(state, market.get('recovery_forecast'), now)
    except (KeyError, TypeError, ValueError, OverflowError):
        error = 'forecast_invalid'
    if error:
        finish(state, error)
        return 'structure_guard_ended:' + error
    return None


def evaluate(state, tick, market, now):
    """Return (HOLD/EXIT, reason), or None for ordinary stop processing.

    Call after mandatory regime actions and on a fresh matching option tick.
    A recovered price consumes the exception; it never creates a new allowance.
    """
    g, p = state.structure_guard, policy(state)
    if not p or g.get('status') not in ('ARMED', 'ACTIVE'):
        return None
    active = g['status'] == 'ACTIVE'
    quote_ok = depth(tick) and spot_valid(state, market, now)
    bid = tick['bid'] if depth(tick) else tick['ltp']
    if active and now >= stamp(g['deadline']):
        finish(state, 'structure_grace_expired')
        return 'EXIT', 'structure_grace_expired'
    if (active or tick['ltp'] <= state.active_stop) and confirmed_breach(state, market, now):
        finish(state, 'structure_buffer_breached')
        return 'EXIT', 'structure_buffer_breached'
    recovered = (quote_ok and bid > state.active_stop
        and (market['spot'] > g['technical_stop_spot'] if state.plan.direction == 'CE'
             else market['spot'] < g['technical_stop_spot']))
    if active and recovered:
        finish(state, 'structure_recovered')
        return 'HOLD', 'structure_recovered'
    if not active and tick['ltp'] > state.active_stop:
        return None
    if state.pending:
        if active:
            finish(state, 'pending_order')
        return None
    reason = None
    if not quote_ok or market.get('macro_event'):
        reason = 'structure_inputs_unavailable_or_macro_event'
    elif bid <= hard_floor(state):
        reason = 'hard_risk_floor_breached'
    elif state.active_stop != g['technical_stop_premium']:
        reason = 'technical_stop_changed'
    elif not fresh(g['reaction_at'], now, p['reaction_max_age_seconds']):
        reason = 'structure_reaction_expired'
    else:
        reason = forecast_error(state, market.get('recovery_forecast'), now, bid)
    if reason:
        if active:
            finish(state, reason)
            return 'EXIT', 'structure_guard_ended:' + reason
        return None
    if not active:
        if stamp(tick['ts']) <= stamp(g['armed_at']):
            return None
        f = market['recovery_forecast']
        deadline = min(now + timedelta(seconds=p['grace_seconds']), stamp(f['outcome_deadline']))
        g.update(status='ACTIVE', used=True, started_at=now.isoformat(), deadline=deadline.isoformat(),
            recovery_target_premium=f['recovery_target_premium'], validation_id=f['validation_id'],
            model_id=f['model_id'], last_confidence_pct=f['confidence_pct'])
    else:
        g['last_confidence_pct'] = market['recovery_forecast']['confidence_pct']
    return 'HOLD', 'structure_grace_active'


def validate_saved(state):
    """Corrupt/unknown guard state must not silently grant another exception."""
    g = state.structure_guard
    if not isinstance(g, dict):
        raise ValueError('invalid_guard_state')
    if not g:
        return
    status = g.get('status')
    if status not in ('ARMED', 'ACTIVE', 'SPENT') or type(g.get('used')) is not bool:
        raise ValueError('invalid_guard_state')
    if (status == 'ARMED') == g['used']:
        raise ValueError('invalid_guard_usage')
    if status == 'SPENT':
        return
    for name in ('low', 'high', 'technical_stop_premium', 'technical_stop_spot',
                 'boundary_spot', 'projected_boundary_premium', 'buffer_points', 'proximity_points'):
        if not number(g.get(name), 0) or g[name] <= 0:
            raise ValueError('invalid_guard_price')
    p = policy(state)
    if (not p or g['low'] > g['high'] or not isinstance(g.get('level_id'), str)
            or not g['level_id'] or g['technical_stop_premium'] > state.active_stop):
        raise ValueError('invalid_guard_context')
    if (state.plan.direction == 'CE' and not g['boundary_spot'] < g['low']
            or state.plan.direction == 'PE' and not g['boundary_spot'] > g['high']):
        raise ValueError('invalid_guard_boundary')
    armed = stamp(g['armed_at'])
    if armed < stamp(state.plan.entry_ts) or stamp(g['reaction_at']) > armed:
        raise ValueError('invalid_guard_timestamp')
    if status == 'ACTIVE':
        started, deadline = stamp(g['started_at']), stamp(g['deadline'])
        if (not armed < started < deadline or (deadline-started).total_seconds() > p['grace_seconds']
                or not number(g.get('recovery_target_premium'), g['technical_stop_premium'])
                or not g.get('validation_id') or not g.get('model_id')):
            raise ValueError('invalid_guard_deadline')
