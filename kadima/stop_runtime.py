"""Executor/ledger adapter for explicitly bound, verified stop plans.

No broker calls or invented market inputs. New bindings are rollout-gated;
existing bindings continue protection if the rollout flag is switched off.
"""
import copy
import json
from dataclasses import replace
from zoneinfo import ZoneInfo

from . import config, storage, regime_stops as stops


def enabled():
    return getattr(config, 'REGIME_STOPS_ENABLED', False) is True


def manual_cas(pos, now, plan=None):
    if plan is not None:
        return (plan.source in stops.MANUAL_SOURCES and plan.manual_cas_start is not None
                and now >= stops._stamp(plan.manual_cas_start))
    if not enabled() or pos.kind not in stops.MANUAL_SOURCES or pos.exchange not in ('NFO','BFO'):
        return False
    local = now.astimezone(ZoneInfo('Asia/Kolkata'))
    clock = getattr(config, 'MANUAL_STOP_POLICY', {}).get('cas_start')
    return bool(clock and local >= stops._clock(local, clock))


def bound(signal_id=None, connection=None):
    c = connection if connection is not None else storage.conn()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='regime_stop_states'").fetchone():
        return False
    if signal_id is None:
        return c.execute('SELECT 1 FROM regime_stop_states LIMIT 1').fetchone() is not None
    return c.execute('SELECT 1 FROM regime_stop_states WHERE signal_id=?', (signal_id,)).fetchone() is not None


def _write(state, c):
    c.execute('INSERT OR REPLACE INTO regime_stop_states VALUES (?,?)',
        (state.signal_id,json.dumps(dict(version=config.STOP_ENGINE['version'],
         state=stops.asdict(state)),allow_nan=False)))


def bind(pos, plan, *, now, tick_size):
    """Only final confirmed fills with known ownership may receive a plan.

    Caller is the serialized executor after broker reconciliation. Binding is
    immutable; subsequent market contexts cannot replace source or initial risk.
    """
    from . import execution_ledger
    if not enabled():
        raise ValueError('regime_rollout_disabled')
    if (not isinstance(plan, stops.StopPlan) or pos.status != 'open'
            or pos.remaining_qty <= 0 or execution_ledger.pending(pos.signal_id)
            or pos.exit_reason):
        raise ValueError('confirmed_unencumbered_position_required')
    if (plan.source != pos.kind or plan.token != str(pos.token) or plan.exchange != pos.exchange
            or plan.asset != pos.index or plan.direction != pos.direction
            or abs(plan.entry_price-pos.entry_price) > 1e-8 or plan.lot_size != pos.lot_size):
        raise ValueError('stop_plan_position_mismatch')
    if (not stops._number(tick_size,0) or tick_size <= 0 or pos.entry_ts is None
            or stops._stamp(plan.entry_ts) != pos.entry_ts
            or stops._stamp(plan.hard_exit) <= now):
        raise ValueError('verified_tick_size_entry_time_and_deadline_required')
    plan=replace(plan,params=dict(plan.params,execution_tick_size=tick_size))
    c=storage.conn()
    if bound(pos.signal_id,c):
        raise ValueError('stop_plan_already_bound')
    state=stops.open_position(plan,pos.signal_id,pos.remaining_qty)
    # Migrating a position must never discard protection already earned.
    state.active_stop=max(state.active_stop,pos.effective_stop or 0,pos.structural_stop or 0)
    state.protected_stop=state.active_stop
    state.peak_premium=max(plan.entry_price,pos.peak_price or plan.entry_price)
    stops.save_state(state,c)
    return state


def load(pos, c=None):
    c=c if c is not None else storage.conn()
    state=stops.load_state(pos.signal_id,c)
    if (state is None or state.plan.token != str(pos.token) or state.plan.exchange != pos.exchange
            or state.plan.source != pos.kind or state.plan.direction != pos.direction
            or state.plan.asset != pos.index or state.remaining_qty != pos.remaining_qty
            or abs(state.plan.entry_price-pos.entry_price)>1e-8):
        raise ValueError('persisted_stop_position_mismatch')
    return state


def link_order(c, intent, pos, state, quantity):
    if (state.signal_id != pos.signal_id or state.remaining_qty != pos.remaining_qty
            or not state.pending or state.pending['quantity'] != quantity):
        raise ValueError('stop_order_identity_mismatch')
    c.execute('CREATE TABLE IF NOT EXISTS regime_order_links '
              '(intent TEXT PRIMARY KEY, signal_id INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)')
    _write(state,c)
    c.execute('INSERT INTO regime_order_links(intent,signal_id) VALUES (?,?)',(intent,pos.signal_id))


def confirm_fill(c, row, cumulative, terminal, remaining_qty):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='regime_order_links'").fetchone():
        return
    link=c.execute('SELECT signal_id,consumed FROM regime_order_links WHERE intent=?',(row['intent'],)).fetchone()
    if link is None or link['consumed']:
        return
    if row['side']!='SELL' or link['signal_id']!=row['signal_id']:
        raise ValueError('stop_fill_identity_mismatch')
    state=stops.load_state(row['signal_id'],c)
    stops.acknowledge_fill(state,cumulative,terminal=terminal)
    if state.remaining_qty != remaining_qty:
        raise ValueError('stop_fill_quantity_mismatch')
    _write(state,c)
    if terminal:
        c.execute('UPDATE regime_order_links SET consumed=1 WHERE intent=?',(row['intent'],))


def decide(pos, tick, context, now, emergency=None):
    state=load(pos)
    market=copy.deepcopy(context or {})
    market['now']=now
    if emergency:
        decision=stops._decision(state,'EXIT',emergency)
    else:
        thesis=market.pop('thesis',None)
        if thesis is not None:
            thesis['now']=now
            stops.thesis_check(state,thesis)
        decision=stops.update_stop(state,tick,market)
    stops.save_state(state,storage.conn())
    return state,decision


def cancel_partial_for_full(state, rows, now):
    """Request cancellation once; broker confirmation alone releases quantity.

    An ambiguous cancellation remains pending and is surfaced by ledger refresh.
    Do not issue a replacement while its actual final filled quantity is unknown.
    """
    if not state.exit_after_pending or len(rows) != 1:
        return
    row=rows[0]
    if row['side'] != 'SELL' or not row['order_id']:
        return
    c=storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS regime_cancel_requests (intent TEXT PRIMARY KEY, ts TEXT NOT NULL)')
    with c:
        cursor=c.execute('INSERT OR IGNORE INTO regime_cancel_requests VALUES (?,?)',
                         (row['intent'],now.isoformat()))
    if cursor.rowcount:
        from . import angel_orders, audit
        result=angel_orders.cancel_order(row['order_id'])
        audit.log('regime_partial_cancel_requested',signal_id=state.signal_id,
                  intent=row['intent'],reason=result.reason)
