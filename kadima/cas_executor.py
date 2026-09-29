"""Default-off CAS execution using the shared serialized, durable order ledger.

CAS keeps its own detection/no-entry-stop policy, not a separate account budget.
Reserve full premium plus costs; preserve the account gate, pause/kill, permits,
exact contract identity and confirmed-fill accounting. A quote is not a fill.
"""
import json
import threading
import time
from datetime import datetime

from . import (alerts, angel_orders, audit, capital_guard, celestial, execution_ledger,
               exit_manager, killswitch, market_session_gate, storage, cas_safety as safety)
from .angel_orders import CAS_KIND
from .config import (CAS_ENTRY_MAX_STEPS, CAS_ENTRY_PRICE_CAP_MULT, CAS_ENTRY_STEP_SEC,
                     CAS_EXECUTION_ENABLED, CAS_EXIT_WATCH_SEC, CAS_MAX_CAPITAL_INR,
                     CAS_MAX_ORDER_QTY, CAS_MAX_SPREAD_PCT, CAS_MAX_TRADES_PER_DAY,
                     CAS_PRODUCT_TYPE, CAS_TRAIL_ARM_PCT, CAS_TRAIL_GIVEBACK_PCT,
                     DAILY_LOSS_LIMIT_INR, MARKET_CLOSE)
from .instrument_master import get_lot_size
from .session import SESSION
from .utils import now_ist

MAX_SIGNAL_AGE_SEC = 20
_inflight = threading.Lock()
_watcher_lock = threading.Lock()


def _executor():
    from .auto_executor import EXECUTOR
    return EXECUTOR


def fetch_quote(exchange, token, *, for_exit=False):
    try:
        SESSION.ensure()  # Existing SDK has the shared cross-process quote budget.
        try:
            response = SESSION.api.getMarketData(mode='FULL', exchangeTokens={exchange:[token]})
        except TypeError:
            response = SESSION.api.getMarketData('FULL', {exchange:[token]})
        return safety.parse_quote(response, exchange, token, now_ist(), for_exit=for_exit)
    except Exception as exc:
        audit.log('cas_quote_unavailable', exchange=exchange, token=token, error_type=type(exc).__name__)
        return None


def cas_trades_today():
    day = now_ist().astimezone(safety.IST).date().isoformat()
    row = storage.conn().execute(
        "SELECT COUNT(*) FROM positions WHERE kind=? AND entry_ts LIKE ? "
        "AND (entry_price > 0 OR status != 'closed')", (CAS_KIND, day+'%')).fetchone()
    return int(row[0])


def size_order(index, budget, cap_price, lot_size, *, bid_qty=None, ask_qty=None):
    freeze = CAS_MAX_ORDER_QTY.get(index)
    return safety.affordable(budget, cap_price, lot_size, freeze, bid_qty, ask_qty)


def price_ladder(quote, cap):
    # First price never rounds ABOVE the bid. Transport currently uses a 0.1 grid.
    raw = [safety.grid(quote['bid']), quote['ltp'], quote['ask'], quote['ask']*1.02]
    out = []
    for index, p in enumerate(raw):
        price = min(safety.grid(p, up=index > 0), safety.grid(cap))
        if price > 0 and (not out or price > out[-1]):
            out.append(price)
    return out[:CAS_ENTRY_MAX_STEPS]


def controls():
    """Missing rollout/permit/pause evidence is not permission to trade."""
    import os
    if not CAS_EXECUTION_ENABLED or not angel_orders.ORDER_PLACEMENT_ENABLED:
        return 'cas_execution_disabled', None
    if CAS_PRODUCT_TYPE != 'CARRYFORWARD':
        return 'cas_product_policy_changed', None
    if killswitch.engaged():
        return 'killswitch_engaged', None
    if not celestial.enabled() or os.environ.get('KADIMA_SESSION_AUTOTRADE') != '1':
        return 'cas_control_or_session_gate_disabled', None
    try:
        cfg = celestial.read_config()
    except Exception:
        return 'cas_controls_unavailable', None
    if cfg.get('kill_latched') is not False:
        return 'kill_latched', None
    if cfg.get('auto_trade_enabled') is not True:
        return 'auto_paused', None
    return None, cfg['revision']


def gate(candidate, executor, now, *, own_signal=None, revision=None):
    from .cas_blast_scanner import in_cas_window
    block, current_revision = controls()
    if block:
        return block
    if revision is not None and revision != current_revision:
        return 'cas_controls_changed'
    if not in_cas_window(now):
        return 'outside_cas_window'
    if (candidate.exchange not in ('NFO','BFO') or candidate.option_type not in ('CE','PE')
            or candidate.expiry != now.astimezone(safety.IST).date().isoformat()
            or not safety.fresh(candidate.detected_at, now, MAX_SIGNAL_AGE_SEC)
            or not safety.fresh(candidate.quote_at, now)
            or not safety.fresh(candidate.spot_at, now, 30)):
        return 'cas_signal_or_contract_stale'
    if executor._reconcile_blocked or not executor._positions_verified:
        return 'startup_reconcile_not_complete'
    if exit_manager.realized_pnl_today() <= -DAILY_LOSS_LIMIT_INR:
        return 'daily_loss_limit_breached'
    if own_signal is None and cas_trades_today() >= CAS_MAX_TRADES_PER_DAY:
        return 'cas_daily_trade_limit'
    for pos in executor.state.positions.values():
        if str(pos.signal_id) == str(own_signal):
            if pos.exit_reason:
                return 'cas_exit_already_requested'
            continue
        if pos.status != 'closed' or pos.remaining_qty:
            return 'account_position_already_open'
    pending = execution_ledger.pending()
    if any(str(r['signal_id']) != str(own_signal) for r in pending):
        return 'portfolio_order_pending_wait'
    if (getattr(executor, '_manual_carryforward_underlyings', set())
            or getattr(executor, '_external_contracts', set())):
        return 'cas_external_account_exposure'
    # Explicitly retain the installed calendar/lease. Never extend it for CAS here.
    return market_session_gate.entry_block(candidate.exchange, now=now)


def current_candidate_reason(candidate, chain, now):
    if chain is None:
        return 'cas_live_chain_required'
    quote = chain.quotes.get(candidate.token)
    spec = safety.contract(chain, quote, now) if quote is not None else None
    if (spec is None or spec['expiry'] != candidate.expiry or spec['lot_size'] != candidate.lot_size
            or quote.symbol != candidate.symbol or quote.opt_type != candidate.option_type
            or quote.strike != candidate.strike or chain.index.name != candidate.index
            or chain.index.opt_exchange != candidate.exchange):
        return 'cas_contract_changed'
    if (not safety.fresh(getattr(chain,'_cas_spot_at',None), now, 30)
            or not 0 <= time.monotonic()-getattr(chain,'_last_poll',0) <= 30):
        return 'cas_underlying_or_chain_stale'
    spot = getattr(chain,'_cas_spot',None)
    if not safety.number(spot, positive=True):
        return 'cas_underlying_unavailable'
    if (spot <= candidate.base_spot if candidate.option_type == 'CE' else spot >= candidate.base_spot):
        return 'cas_direction_invalidated'
    return None


def _reject(reason, candidate, **extra):
    audit.log('cas_entry_rejected', reason=reason, index=candidate.index,
              symbol=candidate.symbol, token=candidate.token, exchange=candidate.exchange, **extra)
    return {'ok':False, 'reason':reason}


def submit(candidate, chain):
    block, _ = controls()
    if block:
        return _reject(block, candidate)
    if not _inflight.acquire(blocking=False):
        return _reject('entry_already_in_progress', candidate)
    def run():
        try:
            enter(candidate, chain)
        except Exception as exc:
            audit.log('cas_entry_exception', symbol=candidate.symbol, error_type=type(exc).__name__)
        finally:
            _inflight.release()
    try:
        threading.Thread(target=run, daemon=True, name='cas-entry').start()
    except Exception:
        _inflight.release()
        raise
    return {'ok':True, 'reason':'cas_evaluation_scheduled_not_order_confirmation'}


def enter(candidate, chain=None):
    from .auto_executor import Position
    executor = _executor()
    cap = safety.grid(candidate.ltp*CAS_ENTRY_PRICE_CAP_MULT)
    with executor._execution_lock:
        block, revision = controls()
        if block:
            return _reject(block,candidate)
        # A fresh account reconciliation precedes a new CAS reservation.
        executor.reconcile_on_startup()
        block = gate(candidate,executor,now_ist(),revision=revision) or current_candidate_reason(candidate,chain,now_ist())
        if block:
            return _reject(block,candidate)
        lot = get_lot_size(candidate.symbol,candidate.token)
        if type(lot) is not int or lot != candidate.lot_size:
            return _reject('lot_size_unresolved_or_changed',candidate)
        funds = capital_guard.broker_available_funds()
        q = fetch_quote(candidate.exchange,candidate.token)
        now = now_ist()
        block = (gate(candidate,executor,now,revision=revision) or current_candidate_reason(candidate,chain,now)
                 or safety.entry_book_reason(q,now,lot,CAS_MAX_SPREAD_PCT))
        if block:
            return _reject(block,candidate)
        if q['symbol'] != candidate.symbol or q['ltp'] > cap or q['ask'] > cap:
            return _reject('cas_quote_identity_or_price_cap',candidate)
        budget = safety.budget(funds,CAS_MAX_CAPITAL_INR,DAILY_LOSS_LIMIT_INR,
                               exit_manager.realized_pnl_today(),capital_guard.CAPITAL_SAFETY_BUFFER_PCT)
        lots,qty = size_order(candidate.index,budget,cap,lot,bid_qty=q['bid_qty'],ask_qty=q['ask_qty'])
        if not qty:
            return _reject('cas_cash_risk_or_depth_below_one_lot',candidate)
        ladder = price_ladder(q,cap)
        if not ladder:
            return _reject('no_valid_entry_price',candidate)
        payload = dict(source='CAS_BLAST',model_version=safety.VERSION,product='CARRYFORWARD',
            exchange=candidate.exchange,token=candidate.token,symbol=candidate.symbol,expiry=candidate.expiry,
            strike=candidate.strike,direction=candidate.option_type,quote_at=q['quote_at'],spot_at=candidate.spot_at,
            detected_at=candidate.detected_at.isoformat(),cap=cap,budget=budget,
            full_premium_risk_reserved=qty*cap*(1+safety.COST_RESERVE),lots=lots,control_revision=revision)
        sid = storage.insert_signal(candidate.index,'CAS',candidate.premium_change_pct,
                                    candidate.option_type,json.dumps(payload),kind=CAS_KIND)
        pos = Position(signal_id=sid,kind=CAS_KIND,index=candidate.index,direction=candidate.option_type,
            symbol=candidate.symbol,token=candidate.token,exchange=candidate.exchange,entry_price=0.0,
            entry_ts=now,lots=lots,lot_size=lot,remaining_qty=0,status='entry_pending',
            allocated_capital=qty*cap*(1+safety.COST_RESERVE))
        executor.state.positions[str(sid)] = pos
        # Recheck after signal persistence, immediately before the ledger write/send.
        block = (gate(candidate,executor,now_ist(),own_signal=sid,revision=revision)
                 or current_candidate_reason(candidate,chain,now_ist()))
        if block or not safety.fresh(q['quote_at'],now_ist()):
            pos.status='closed'
            storage.upsert_position(pos)
            executor.state.positions.pop(str(sid),None)
            return _reject(block or 'cas_quote_expired_before_submission',candidate)
        result = execution_ledger.submit(pos,'BUY',qty,ladder[0])
        executor._last_reconcile = 0.0
        if pos.status == 'closed':
            executor.state.positions.pop(str(sid),None)
        audit.log('cas_entry_submission_result', signal_id=sid, token=pos.token, exchange=pos.exchange,
                  price=ladder[0], cap=cap, quantity=qty, **result)
    # UNKNOWN may already have a real fill. Protection/reconciliation starts too.
    if result.get('ok') or result.get('pending'):
        start_exit_watcher(executor)
    if result.get('ok') and result.get('pending') and result.get('order_id'):
        _step_entry(pos,result['order_id'],qty,ladder[0],cap,candidate,chain,revision)
    return result


def _step_entry(pos, order_id, qty, start, cap, candidate, chain, revision):
    executor = _executor()
    current = start
    targets = (lambda q:q['ltp'], lambda q:q['ask'],lambda q:q['ask']*1.02)[:max(0,CAS_ENTRY_MAX_STEPS-1)]
    for step,target in enumerate(targets,2):
        if executor._stop.wait(CAS_ENTRY_STEP_SEC):
            break
        with executor._execution_lock:
            executor.refresh_orders()
            pending = execution_ledger.pending(pos.signal_id)
            if not pending or pos.status != 'entry_pending':
                return
            if len(pending)!=1 or pending[0]['order_id']!=order_id or pending[0]['side']!='BUY':
                return  # Never guess which order owns an ambiguous response.
            if pos.remaining_qty > 0:
                break  # Never chase the remainder once any quantity is filled.
            block = gate(candidate,executor,now_ist(),own_signal=pos.signal_id,revision=revision)
            if block or current_candidate_reason(candidate,chain,now_ist()):
                break
        # Do not hold the shared writer lock while waiting for a quote: the
        # monitor must be able to reconcile/protect a fill arriving meanwhile.
        q = fetch_quote(pos.exchange,pos.token)
        with executor._execution_lock:
            executor.refresh_orders()
            pending = execution_ledger.pending(pos.signal_id)
            if not pending or pos.status != 'entry_pending':
                return
            if len(pending)!=1 or pending[0]['order_id']!=order_id or pending[0]['side']!='BUY':
                return
            if pos.remaining_qty>0:
                break
            now = now_ist()
            if (gate(candidate,executor,now,own_signal=pos.signal_id,revision=revision)
                    or current_candidate_reason(candidate,chain,now)
                    or safety.entry_book_reason(q,now,pos.lot_size,CAS_MAX_SPREAD_PCT)
                    or q['symbol']!=pos.symbol or q['ltp']>cap or q['ask']>cap
                    or min(q['bid_qty'],q['ask_qty'])<qty):
                break
            price = min(safety.grid(target(q),up=True),safety.grid(cap))
            if price <= current:
                continue
            # Serialized with fills, cancellation, manual exits and the CAS monitor.
            result = angel_orders.modify_order(order_id,price=price,quantity=qty,
                contract=dict(tradingsymbol=pos.symbol,symboltoken=pos.token,
                              exchange=pos.exchange,producttype=angel_orders.product_for(pos)))
            audit.log('cas_entry_reprice_result',signal_id=pos.signal_id,order_id=order_id,
                      step=step,price=price,accepted=result.ok,uncertain=result.uncertain)
            if not result.ok or result.uncertain:
                break  # Unknown modify is not permission to increase again.
            current=price
    else:
        executor._stop.wait(CAS_ENTRY_STEP_SEC)
    with executor._execution_lock:
        pending = execution_ledger.pending(pos.signal_id)
        if len(pending)==1 and pending[0]['order_id']==order_id and pending[0]['side']=='BUY':
            result = angel_orders.cancel_order(order_id)
            audit.log('cas_entry_cancel_requested',signal_id=pos.signal_id,order_id=order_id,
                      accepted=result.ok,uncertain=result.uncertain)
            # The pending intent remains until terminal broker evidence is reconciled.


def exit_reason(pos, ltp, daily_loss_breached, force_close):
    if daily_loss_breached:
        return 'daily_loss_limit_breached'
    if force_close:
        return 'eod_force_close'
    if pos.exit_reason:
        return pos.exit_reason
    if not safety.number(pos.entry_price,positive=True) or not safety.number(ltp,positive=True):
        return None
    if pos.status not in ('open','entry_pending'):
        return None
    peak=max(pos.peak_price or pos.entry_price,ltp)
    previous=pos.effective_stop if safety.number(pos.effective_stop,positive=True) else 0.0
    active=pos.trail_active or peak+1e-10 >= pos.entry_price*(1+CAS_TRAIL_ARM_PCT/100)
    if active:
        pos.trail_active=True
        pos.effective_stop=max(previous,pos.entry_price,round(peak*(1-CAS_TRAIL_GIVEBACK_PCT/100),2))
        storage.upsert_position(pos)
    # An explicit operator-tightened stop remains binding even before activation.
    stop=pos.effective_stop or 0.0
    return 'cas_peak_trail' if stop>0 and ltp<=stop else None


def exit_bid(pos):
    q=fetch_quote(pos.exchange,pos.token,for_exit=True)
    return q['bid'] if q and q['symbol']==pos.symbol else None


def check_exits(executor, *, force_signal=None, reason=None):
    """Shared lock required. Entries may be paused; exits never require a permit."""
    positions=[p for p in list(executor.state.positions.values()) if p.kind==CAS_KIND and p.status!='closed']
    if not positions:
        return
    executor.refresh_orders()
    if time.monotonic()-executor._last_reconcile >= 15:
        executor.reconcile_on_startup()
    now=now_ist()
    hard_kill=killswitch.engaged()
    if celestial.enabled():
        try:
            hard_kill=hard_kill or celestial.read_config()['kill_latched'] is True
        except Exception:
            audit.log('cas_exit_controls_unavailable')  # Never suppress existing exits.
    from .auto_executor import EOD_FORCE_CLOSE_HHMM
    for pos in positions:
        if pos.status=='closed':
            continue
        late=pos.entry_ts is not None and pos.entry_ts.astimezone(safety.IST).date()<now.astimezone(safety.IST).date()
        due=late or (now.hour,now.minute)>=EOD_FORCE_CLOSE_HHMM
        loss=exit_manager.realized_pnl_today()<=-DAILY_LOSS_LIMIT_INR
        urgent=('daily_loss_limit_breached' if loss else 'eod_force_close' if due else
                'cas_kill_exit' if hard_kill else
                reason if force_signal==pos.signal_id else pos.exit_reason)
        if urgent:
            pos.exit_reason=urgent
            storage.upsert_position(pos)
            if pos.status=='entry_pending':
                execution_ledger.request_entry_exit(pos,urgent)
                continue
        if pos.status not in ('open','entry_pending') or pos.remaining_qty<=0:
            continue
        verified=(str(pos.signal_id) in executor._celestial_verified if celestial.enabled() else
                  executor._positions_verified and str(pos.signal_id) not in executor._exit_paused)
        if not verified:
            audit.log('cas_exit_ownership_unverified',signal_id=pos.signal_id)
            continue
        q=fetch_quote(pos.exchange,pos.token,for_exit=True)
        if q is None or q['symbol']!=pos.symbol or not safety.fresh(q.get('quote_at'),now_ist()):
            audit.log('cas_exit_quote_unavailable',signal_id=pos.signal_id,exit_requested=bool(pos.exit_reason))
            alerts.enqueue(f'CAS exit quote unavailable: {pos.symbol}; check owned exposure.',
                           key=f'cas-exit-quote:{pos.signal_id}')
            continue  # No entry/peak/stop-price fallback masquerading as a live bid.
        bid=q['bid']
        pos.peak_price=max(pos.peak_price or pos.entry_price,bid)
        storage.upsert_position(pos)
        why=exit_reason(pos,bid,loss,due)
        if why:
            executor._close_position(pos,bid,why)


def request_exit(executor,pos,reason):
    pos.exit_reason=pos.exit_reason or reason
    storage.upsert_position(pos)
    check_exits(executor,force_signal=pos.signal_id,reason=reason)
    return dict(ok=True,reason='cas_exit_requested_not_fill_confirmation',signal_id=pos.signal_id,
                pending=pos.status!='closed')


def start_exit_watcher(executor=None):
    executor=executor or _executor()
    with _watcher_lock:
        worker=getattr(executor,'_cas_watcher',None)
        if worker is not None and worker.is_alive():
            return
        def loop():
            while not executor._stop.is_set():
                started=time.monotonic()
                try:
                    with executor._execution_lock:
                        check_exits(executor)
                except Exception as exc:
                    audit.log('cas_exit_watch_error',error_type=type(exc).__name__)
                executor._stop.wait(max(.1,CAS_EXIT_WATCH_SEC-(time.monotonic()-started)))
        worker=threading.Thread(target=loop,daemon=True,name='cas-exit-watch')
        executor._cas_watcher=worker
        worker.start()
