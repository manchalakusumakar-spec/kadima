"""CAS order adapter. Called under AutoExecutor's execution lock.

Shares account controls, durable intents, reconciliation and owned-quantity exits.
Never calls the legacy Shield eligibility/score/ranking path for a CAS entry.
"""
from types import SimpleNamespace

from . import cas_engine as cas, celestial, config, execution_ledger, storage, trade_policy
from .cas_config import tick_price
from .utils import now_ist


def fetch_quote(exchange, token, symbol, strike, direction, *, for_exit=False):
    from .auto_executor import SESSION, QUOTE
    from .option_chain import OptionQuote, _f, _i
    if not QUOTE.wait_and_allow(timeout_sec=1.0): return None
    try:
        SESSION.ensure()
        try:
            response = SESSION.api.getMarketData(mode='FULL', exchangeTokens={exchange: [token]})
        except TypeError:
            response = SESSION.api.getMarketData('FULL', {exchange: [token]})
        if not response or response.get('status') is not True: return None
        rows = [r for r in (response.get('data') or {}).get('fetched', [])
                if str(r.get('symbolToken')) == token and r.get('exchange') == exchange]
        if len(rows) != 1: return None
        r = rows[0]
        if r.get('tradingSymbol') not in (None, symbol): return None
        depth = r.get('depth') or {}
        buy = (depth.get('buy') or [{}])[0]
        sell = (depth.get('sell') or [{}])[0]
        q = OptionQuote(token, symbol, strike, direction, ltp=_f(r.get('ltp')),
                        bid=_f(buy.get('price')), ask=_f(sell.get('price')),
                        volume=_i(r.get('tradeVolume')), option_vwap=_f(r.get('avgPrice')),
                        quote_at=r.get('exchFeedTime'), bid_qty=_i(buy.get('quantity')),
                        ask_qty=_i(sell.get('quantity')))
        if for_exit:
            # A deteriorating spread or missing VWAP must not trap an owned long.
            return q if cas.positive(q.bid, q.bid_qty) and cas.fresh(q.quote_at, now_ist()) else None
        return q if cas.quote_valid(q, now_ist()) else None
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return None
    except Exception:
        return None  # transport failure is a blocked entry; never retry an order here


def account_block(ex, now, exchange=None, underlying=None):
    from . import auto_executor as ae
    if underlying in getattr(ex, '_manual_carryforward_underlyings', set()):
        return 'manual_carryforward_underlying'
    if exchange is not None:
        from .market_session_gate import entry_block
        reason = entry_block(exchange, now)
        if reason: return reason
    if not ae.ORDER_PLACEMENT_ENABLED: return 'broker_orders_disabled'
    blocks = cas.live_blocks(now)
    if blocks: return blocks[0]
    if ex._reconcile_blocked or not ex._positions_verified: return 'startup_reconcile_not_complete'
    if ae.killswitch.engaged(): return 'killswitch_engaged'
    if not ae.in_market_hours() or ae.past_signal_cutoff(): return 'outside_entry_session'
    if ae.exit_manager.realized_pnl_today() <= -ae.DAILY_LOSS_LIMIT_INR:
        return 'daily_loss_limit_breached'
    return None


def dispatch(ex, st, lane, row):
    from . import auto_executor as ae
    def reject(reason): return ex._reject(reason, st.name, row.get('direction'))
    now = now_ist()
    blocked = account_block(ex, now, row.get('exchange'), st.name)
    if blocked: return reject(blocked)
    if not row.get('eligible') or row.get('stage') not in ('CAS ENTRY', 'CAS ADD'):
        return reject('cas_setup_not_executable')
    latest = next((r for r in lane.scan(st, ex.state.positions, now, record=False)
                   if r.get('episode') == row.get('episode')), {})
    keys = ('episode', 'token', 'symbol', 'exchange', 'direction', 'stage', 'quote_at')
    if not latest.get('eligible') or any(latest.get(k) != row.get(k) for k in keys):
        return reject('cas_setup_changed')
    saved = cas.run(row['episode'])
    pos = ex.state.positions.get(str(saved['signal_id'])) if saved else None
    adding = row['stage'] == 'CAS ADD'
    if adding:
        if (not pos or pos.kind != 'cas' or pos.status != 'open' or pos.exit_reason
                or pos.remaining_qty <= 0 or saved['attempts'] != 1):
            return reject('cas_add_position_not_ready')
        if (pos.token, pos.symbol, pos.exchange, pos.direction) != tuple(row[k] for k in ('token','symbol','exchange','direction')):
            return reject('cas_add_contract_mismatch')
    elif saved or ex.state.positions:
        return reject('cas_session_used_or_position_slot_busy')
    if execution_ledger.pending(): return reject('account_order_pending')
    if adding and any(p.signal_id != pos.signal_id and p.status != 'closed'
                      for p in ex.state.positions.values()):
        return reject('cas_add_other_exposure_exists')
    try:
        contracts = st.chain.scrips.strikes_around(st.chain.index, st.chain.atm, config.STRIKES_EACH_SIDE)
        if (st.chain.scrips.nearest_expiry(st.chain.index) != now.strftime('%d%b%Y').upper()
                or not any(str(c['token']) == row['token'] and c['symbol'] == row['symbol'] for c in contracts)):
            return reject('cas_contract_expiry_unverifiable')
    except (AttributeError, TypeError, KeyError):
        return reject('cas_contract_expiry_unverifiable')
    lot = ae.get_lot_size(row['symbol'], row['token'])
    if not cas.positive(lot) or int(lot) != lot: return reject('lot_size_unresolvable_at_order_time')
    q = fetch_quote(row['exchange'], row['token'], row['symbol'], row['strike'], row['direction'])
    if q is None: return reject('cas_fresh_execution_quote_unavailable')
    # Preserve the CAS tick grid without rounding a BUY above its cap.
    reference = row['ask']
    cap = tick_price(reference*1.01)
    price = tick_price(q.ask, up=True)
    if price > cap or q.ltp < row['ltp'] or q.ltp <= q.option_vwap:
        return reject('cas_entry_ran_away_or_reversed')
    if not adding and price > cas.POLICY.max_premium: return reject('cas_not_cheap_at_execution')
    if adding and price > saved['anchor']*cas.POLICY.max_add_multiple:
        return reject('cas_add_price_extended')
    allocation_source = saved['allocation'] if adding else ae.capital_guard.get_capital_for_signal('bull_shield', 'CONFIRMED')
    # User-approved total buying capital, shared by entry and add. Reapply the
    # ceiling to restored runs as well; never treat it as a permitted loss.
    allocation = min(allocation_source, cas.POLICY.max_capital) if cas.positive(allocation_source) else 0
    max_lots = saved['max_lots'] if adding else max(0, min(2, ae.MAX_LOTS_PER_SIGNAL))
    stop = max(pos.effective_stop or 0, pos.structural_stop or 0) if adding else price*(1-cas.POLICY.stop_fraction)
    if not cas.positive(allocation, stop) or q.bid <= stop: return reject('cas_invalid_allocation_or_stop')
    # One lot per stage. Total exposure retains the ORIGINAL episode budget.
    owned = pos.remaining_qty if adding else 0
    if owned+lot > max_lots*lot or q.ask_qty < lot or q.bid_qty < owned+lot:
        return reject('cas_lot_cap_or_depth_insufficient')
    cost = owned*pos.entry_price if adding else 0
    existing_risk = max(0, cost-owned*stop*(1-trade_policy.EXIT_BUFFER)) + cost*trade_policy.COST_RESERVE
    added_risk = trade_policy.risk_per_unit(price, stop)
    daily_room = max(0.0, ae.DAILY_LOSS_LIMIT_INR + min(0.0, ae.exit_manager.realized_pnl_today()))
    if (added_risk is None or existing_risk+lot*added_risk > min(daily_room, allocation*trade_policy.RISK_FRACTION)
            or (cost+price*lot)*(1+trade_policy.COST_RESERVE) > allocation):
        return reject('cas_total_episode_budget_exceeded')
    allowed, reason = ae.capital_guard.enforce(ex, SimpleNamespace(quote=SimpleNamespace(
        ltp=price*(1+trade_policy.COST_RESERVE), symbol=q.symbol, token=q.token)), row['direction'], 1)
    if allowed < 1: return reject('capital_guard_'+reason)
    now = now_ist()
    blocked = account_block(ex, now, row.get('exchange'), st.name)
    latest = next((r for r in lane.scan(st, ex.state.positions, now, record=False)
                   if r.get('episode') == row['episode']), {})
    if (blocked or not cas.fresh(q.quote_at, now) or not latest.get('eligible')
            or any(latest.get(k) != row.get(k) for k in keys)):
        return reject(blocked or 'cas_invalidated_before_submission')
    if adding:
        if not cas.reserve_add(row['episode']): return reject('cas_add_already_reserved')
    else:
        reserve_row = dict(row, entry=price)
        sid = cas.reserve(reserve_row, allocation, max_lots)
        if sid is None: return reject('cas_episode_already_reserved')
        pos = ae.Position(sid, 'cas', st.name, row['direction'], q.symbol, q.token,
                          row['exchange'], 0.0, now, 1, int(lot), 0, status='entry_pending',
                          structural_stop=stop, effective_stop=stop, allocated_capital=allocation)
        ex.state.positions[str(sid)] = pos
    result = execution_ledger.submit(pos, 'BUY', int(lot), price)
    ex._last_reconcile = 0.0
    if pos.status == 'closed': ex.state.positions.pop(str(pos.signal_id), None)
    ex.entry_diagnostics.append(dict(ts=now.isoformat(), index=st.name, direction=row['direction'],
                                    stage=row['stage'], signal_id=pos.signal_id, **result))
    ae.audit.log('CAS_ORDER_RESULT', episode=row['episode'], stage=row['stage'], **result)
    return result

