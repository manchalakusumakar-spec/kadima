#!/usr/bin/env python3
"""Deterministic tests on real modules/SQLite with inert broker and transport.

No credentials, live session, server, service operation or network is permitted.
This is regression evidence, not a profitability backtest or broker acceptance.
"""
import sys, os, tempfile, types, unittest, copy, threading
from pathlib import Path
from datetime import datetime,timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
TMP=tempfile.TemporaryDirectory(prefix='kadima-cas-offline-')
os.environ['KADIMA_DATA_DIR']=TMP.name
os.environ['KADIMA_CAS_EXECUTION']='0'
for k in list(os.environ):
    if k.startswith(('ANGEL_', 'TELEGRAM_')) or k=='KADIMA_API_TOKEN':
        os.environ.pop(k,None)

def deny(*a,**k):
    raise AssertionError('Network or live session forbidden in CAS tests')
def audit_guard(event,args):
    if event.startswith('socket.') or event in ('subprocess.Popen','os.system'):
        deny()
sys.addaudithook(audit_guard)
stub=types.ModuleType('kadima.session');stub.SESSION=NS(ensure=deny,api=NS())
sys.modules['kadima.session']=stub
stub_alerts=types.ModuleType('kadima.alerts');stub_alerts.enqueue=Mock();stub_alerts.send=Mock()
sys.modules['kadima.alerts']=stub_alerts
from kadima import (cas_safety as s,cas_executor as ce,cas_blast_scanner as scan,
                    auto_executor as ae,angel_orders as ao,execution_ledger as el,
                    storage,celestial,config,exit_manager,portfolio_capacity,
                    external_reconciliation,cas_watchdog,eod_watchdog,market_session_gate)
from kadima.option_chain import OptionQuote,OptionChain
from kadima.scrip_master import ScripMaster

REAL_STEP=ce._step_entry
NOW=datetime(2026,9,29,15,22,tzinfo=s.IST)
SYMBOL='NIFTY29SEP2622700CE'

def row(now=NOW,**kw):
    return dict(exchange='NFO',symbolToken='T',tradingSymbol=SYMBOL,ltp=7.5,
        exchFeedTime=now.isoformat(),exchTradeTime=now.isoformat(),tradeVolume=10000,
        depth={'buy':[{'price':7.4,'quantity':650}], 'sell':[{'price':7.6,'quantity':650}]},**kw)

def response(r=None):
    return {'status':True,'data':{'fetched':[row() if r is None else r]}}

def master_row():
    return dict(token='T',symbol=SYMBOL,name='NIFTY',instrumenttype='OPTIDX',exch_seg='NFO',
                expiry='29SEP2026',lotsize='65',strike='2270000',tick_size='5.000000')

def chain(now=NOW,**kw):
    import time
    q=OptionQuote('T',SYMBOL,22700,'CE',ltp=7.5,volume=10000,oi=200000,bid=7.4,ask=7.6,
                  quote_at=now.isoformat(),trade_at=now.isoformat(),bid_qty=650,ask_qty=650)
    return NS(index=NS(name='NIFTY',opt_exchange='NFO',strike_step=50,opt_symbol_root='NIFTY'),
              scrips=ScripMaster([master_row()]),quotes={'T':q},_last_poll=time.monotonic(),
              _cas_spot_at=now,_cas_spot=22700.,**kw)

def candidate(now=NOW):
    return scan.CasCandidate('NIFTY',SYMBOL,'T','NFO',22700.,'CE',7.5,7.4,7.6,50.,
        5000,1000.,22700.,20.,now,'2026-09-29',65,now.isoformat(),now.isoformat(),22680.,650,650)

class Pure(unittest.TestCase):
    def test_default_execution_off(self):self.assertFalse(config.CAS_EXECUTION_ENABLED)
    def test_hard_network_deny(self):
        import socket
        with self.assertRaises(AssertionError): socket.create_connection(('example.test',443))
    def test_ist_documented_time(self):self.assertEqual(s.stamp('29-Sep-2026 15:22:00'),NOW)
    def test_timezone_naive_iso_refused(self):self.assertIsNone(s.stamp('2026-09-29T15:22:00'))
    def test_quote_valid(self):self.assertEqual(s.parse_quote(response(),'NFO','T',NOW)['bid'],7.4)
    def test_exit_needs_bid_not_entry_spread(self):
        r=row();r['depth']['sell']=[];r['ltp']=None;r['exchTradeTime']=None
        self.assertIsNotNone(s.parse_quote(response(r),'NFO','T',NOW,for_exit=True))
        self.assertIsNone(s.parse_quote(response(r),'NFO','T',NOW))
    def test_cas_cap_counts_costs(self):
        lots,qty=s.affordable(10000,6.5,65,1800,10000,10000)
        self.assertLessEqual(qty*6.5*1.01,10000);self.assertEqual(qty%65,0)
    def test_loss_headroom(self):self.assertEqual(s.budget(50000,10000,10000,-9000,10),1000)
    def test_profit_never_expands_daily_headroom(self):self.assertEqual(s.budget(50000,20000,10000,9000,10),10000)
    def test_reserve_below_one_lot(self):self.assertEqual(s.affordable(650,10.,65,1000,650,650),(0,0))
    def test_depth_sizes_down(self):self.assertEqual(s.affordable(10000,6.5,65,1800,65,650),(1,65))
    def test_bad_size_inputs(self):
        for x in [True,None,float('nan'),-1]:
            self.assertEqual(s.affordable(x,6.5,65,1800,650,650),(0,0))
    def test_unknown_freeze_refused(self):self.assertEqual(s.affordable(10000,6.5,65,None,650,650),(0,0))
    def test_compatible_tick(self):
        self.assertTrue(s.compatible_tick('5'));self.assertFalse(s.compatible_tick('25'))
    def test_first_limit_not_above_bid(self):
        self.assertEqual(ce.price_ladder(dict(bid=5.05,ltp=5.2,ask=5.4),6.5)[0],5.)
    def test_price_cap_monotone(self):
        ps=ce.price_ladder(dict(bid=6.3,ltp=6.4,ask=7.),6.5)
        self.assertLessEqual(max(ps),6.5);self.assertEqual(ps,sorted(set(ps)))
    def test_expiry_mapping(self):
        ch=chain();self.assertEqual(s.contract(ch,ch.quotes['T'],NOW)['lot_size'],65)
        ch.scrips.rows[0]['expiry']='06OCT2026';self.assertIsNone(s.contract(ch,ch.quotes['T'],NOW))
    def test_session_window_converts_utc(self):
        from datetime import timezone
        self.assertTrue(scan.in_cas_window(NOW.astimezone(timezone.utc)))
        self.assertFalse(scan.in_cas_window(NOW.replace(hour=15,minute=35)))
    def test_real_calendar_cutoff_preserved(self):
        p=dict(schema=1,valid_from='2026-09-28',valid_through='2026-12-31',holidays=[],sources=['fixture'],
               arm_at='09:00',entries_at='09:15',stop_entries_at='15:15')
        self.assertEqual(market_session_gate.calendar_reason(p,NOW,entries=True),'outside_scheduled_window')
    def test_product_cannot_drift_on_exit(self):
        with patch.object(ao,'CAS_PRODUCT_TYPE','INTRADAY'):
            self.assertEqual(ao.product_for(NS(kind='cas_blast')),'CARRYFORWARD')
    def test_regular_product_unchanged(self):self.assertEqual(ao.product_for(NS(kind='gamma_blast')),'INTRADAY')

# Each malformed quote is a separately counted regression, not a hidden subtest.
for name,change in {
 'stale_feed':lambda r:r.update(exchFeedTime=(NOW-timedelta(seconds=16)).isoformat()),
 'future_feed':lambda r:r.update(exchFeedTime=(NOW+timedelta(seconds=1)).isoformat()),
 'missing_feed':lambda r:r.pop('exchFeedTime'),
 'stale_trade':lambda r:r.update(exchTradeTime=(NOW-timedelta(seconds=16)).isoformat()),
 'wrong_exchange':lambda r:r.update(exchange='BFO'),
 'wrong_token':lambda r:r.update(symbolToken='OTHER'),
 'missing_bid':lambda r:r['depth'].update(buy=[]),
 'missing_ask':lambda r:r['depth'].update(sell=[]),
 'crossed_book':lambda r:r['depth']['buy'][0].update(price=8.),
 'boolean_price':lambda r:r.update(ltp=True),
 'nan_price':lambda r:r.update(ltp=float('nan')),
 'zero_depth':lambda r:r['depth']['buy'][0].update(quantity=0),
 'fractional_depth':lambda r:r['depth']['buy'][0].update(quantity=1.5),
}.items():
 def case(self,fn=change):
    r=row();fn(r);self.assertIsNone(s.parse_quote(response(r),'NFO','T',NOW))
 setattr(Pure,'test_quote_rejects_'+name,case)

class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=TMP.name)
        if getattr(storage._local,'conn',None):storage._local.conn.close()
        storage._local.conn=None
        self.now=NOW
        self.patches=[]
        def patcher(*args,**kw):
            p=patch(*args,**kw);self.patches.append(p);return p.start()
        patcher('kadima.storage.DB_PATH',self.tmp.name+'/kadima.db')
        self.env=patch.dict(os.environ,{'KADIMA_CELESTIAL_ENABLED':'1','KADIMA_SESSION_AUTOTRADE':'1'});self.env.start()
        for m in [ce,ae,el,celestial,exit_manager,external_reconciliation]:
            p=patch.object(m,'now_ist',lambda:self.now);self.patches.append(p);p.start()
        patcher('kadima.cas_executor.CAS_EXECUTION_ENABLED',True)
        patcher('kadima.angel_orders.ORDER_PLACEMENT_ENABLED',True)
        patcher('kadima.killswitch.engaged',return_value=False)
        patcher('kadima.exit_manager.realized_pnl_today',return_value=0.)
        patcher('kadima.market_session_gate.entry_block',return_value=None)
        celestial.update_config({'auto_trade_enabled':True})
        self.ex=ae.AutoExecutor();self.ex.celestial_master=ScripMaster([master_row()])
        patcher('kadima.cas_executor._executor',return_value=self.ex)
        def reconciled():
            import time
            self.ex._reconcile_blocked=False;self.ex._positions_verified=True
            self.ex._celestial_verified={str(p.signal_id) for p in self.ex.state.positions.values()}
            self.ex._last_reconcile=time.monotonic()
        self.recon=reconciled
        self.ex.reconcile_on_startup=Mock(side_effect=reconciled)
        self.recon()
        patcher('kadima.cas_executor.get_lot_size',return_value=65)
        patcher('kadima.capital_guard.broker_available_funds',return_value=20000.)
        self.quote=s.parse_quote(response(),'NFO','T',NOW)
        self.fq=patcher('kadima.cas_executor.fetch_quote',side_effect=lambda *a,**kw:copy.deepcopy(self.quote))
        self.watcher=patcher('kadima.cas_executor.start_exit_watcher')
        self.step=patcher('kadima.cas_executor._step_entry')
        self.sent=[];self.book={};self.cancels=[];self.modifies=[]
        def place(**kw):
            oid='O'+str(len(self.sent)+1);self.sent.append(kw)
            raw=dict(symboltoken=kw['symboltoken'],exchange=kw['exchange'],tradingsymbol=kw['tradingsymbol'],
                transactiontype=kw['transactiontype'],producttype=kw['producttype'],ordertag=kw['ordertag'],price=kw['price'])
            self.book[oid]=ao.OrderStatus(oid,'open',0,None,raw)
            return ao.OrderResult(True,oid,'ok')
        patcher('kadima.angel_orders.place_limit_order',side_effect=place)
        patcher('kadima.angel_orders.fetch_order_book',side_effect=lambda:list(self.book.values()))
        patcher('kadima.angel_orders.cancel_order',side_effect=lambda oid:self.cancels.append(oid) or ao.OrderResult(True,oid,'ok'))
        patcher('kadima.angel_orders.modify_order',side_effect=lambda oid,**kw:self.modifies.append((oid,kw)) or ao.OrderResult(True,oid,'ok'))
        self.ch=chain();self.cand=candidate()
    def tearDown(self):
        self.ex._stop.set()
        for p in reversed(self.patches):p.stop()
        self.env.stop()
        if getattr(storage._local,'conn',None):storage._local.conn.close();storage._local.conn=None
        self.tmp.cleanup()
    def enter(self):return ce.enter(self.cand,self.ch)
    def pos(self):return next(iter(self.ex.state.positions.values()))
    def fill(self,oid='O1',qty=None,price=7.4,terminal=True):
        status=self.book[oid];status.filled_qty=self.sent[int(oid[1:])-1]['quantity'] if qty is None else qty
        status.avg_price=price;status.status='complete' if terminal else 'open'
        self.ex.refresh_orders();self.recon()
    def test_acceptance_not_fill(self):
        r=self.enter();self.assertTrue(r['pending']);self.assertEqual(self.pos().remaining_qty,0)
        self.assertEqual(self.pos().status,'entry_pending');self.assertEqual(self.sent[0]['producttype'],'CARRYFORWARD')
    def test_watcher_before_steps(self):
        order=[];self.watcher.side_effect=lambda *a:order.append('watch');self.step.side_effect=lambda *a:order.append('step')
        self.enter();self.assertEqual(order,['watch','step'])
    def test_unknown_submission_still_starts_watcher(self):
        ao.place_limit_order.side_effect=lambda **k:ao.OrderResult(False,None,'timeout',uncertain=True)
        r=self.enter();self.assertTrue(r['pending']);self.watcher.assert_called_once();self.step.assert_not_called()
        self.assertFalse(self.enter()['ok'])
    def test_explicit_rejection_not_consumed_trade(self):
        ao.place_limit_order.side_effect=lambda **k:ao.OrderResult(False,None,'rate_limited')
        self.assertFalse(self.enter()['ok']);self.assertEqual(ce.cas_trades_today(),0)
    def test_paused_has_no_broker_reads(self):
        celestial.update_config({'auto_trade_enabled':False})
        self.assertEqual(self.enter()['reason'],'auto_paused');self.fq.assert_not_called()
    def test_disable_direct_enter_cannot_bypass(self):
        ce.CAS_EXECUTION_ENABLED=False
        self.assertEqual(self.enter()['reason'],'cas_execution_disabled');self.assertFalse(self.sent)
    def test_pause_during_quote_blocks_send(self):
        def quote(*a,**kw):celestial.update_config({'auto_trade_enabled':False});return self.quote
        self.fq.side_effect=quote
        self.assertFalse(self.enter()['ok']);self.assertFalse(self.sent)
    def test_kill_during_quote_blocks_send(self):
        def quote(*a,**kw):ce.killswitch.engaged.return_value=True;return self.quote
        self.fq.side_effect=quote
        self.assertFalse(self.enter()['ok']);self.assertFalse(self.sent)
    def test_expiry_during_quote_blocks_send(self):
        def quote(*a,**kw):self.now+=timedelta(seconds=21);return self.quote
        self.fq.side_effect=quote
        self.assertFalse(self.enter()['ok']);self.assertFalse(self.sent)
    def test_direction_change_during_quote_blocks_send(self):
        def quote(*a,**kw):self.ch._cas_spot=22670;return self.quote
        self.fq.side_effect=quote
        self.assertFalse(self.enter()['ok']);self.assertFalse(self.sent)
    def test_revision_change_blocks_send(self):
        def quote(*a,**kw):celestial.update_config({'score_threshold':70});return self.quote
        self.fq.side_effect=quote
        self.assertEqual(self.enter()['reason'],'cas_controls_changed');self.assertFalse(self.sent)
    def test_global_existing_position_blocks(self):
        self.ex.state.positions['999']=NS(signal_id=999,kind='gamma_blast',status='open',remaining_qty=65)
        self.assertFalse(self.enter()['ok']);self.assertFalse(self.sent)
    def test_global_unresolved_order_blocks(self):
        c=el.connection();c.execute("INSERT INTO execution_orders(intent,signal_id,side,quantity,state,created_at) VALUES('U',999,'BUY',65,'unknown',?)",(NOW.isoformat(),));c.commit()
        self.assertEqual(self.enter()['reason'],'portfolio_order_pending_wait')
    def test_manual_exposure_blocks(self):
        self.ex._manual_carryforward_underlyings={'BANKNIFTY'}
        self.assertEqual(self.enter()['reason'],'cas_external_account_exposure')
    def test_remaining_daily_risk_can_reject(self):
        exit_manager.realized_pnl_today.return_value=-9900.
        self.assertFalse(self.enter()['ok']);self.assertFalse(self.sent)
    def test_remaining_daily_risk_sizes_down(self):
        exit_manager.realized_pnl_today.return_value=-9000.
        self.enter();self.assertLessEqual(self.pos().allocated_capital,1000)
    def test_spread_and_depth_gate(self):
        self.quote['bid']=2.
        self.assertEqual(self.enter()['reason'],'spread_too_wide')
    def test_missing_depth_rejected(self):
        self.quote['ask_qty']=0
        self.assertEqual(self.enter()['reason'],'cas_depth_below_one_lot')
    def test_lot_size_drift_rejected(self):
        ce.get_lot_size.return_value=75
        self.assertEqual(self.enter()['reason'],'lot_size_unresolved_or_changed')
    def test_second_filled_trade_not_allowed(self):
        self.enter();self.fill();self.assertEqual(self.enter()['reason'],'cas_daily_trade_limit')
    def test_duplicate_broker_fill_applied_once(self):
        self.enter();self.fill();q=self.pos().remaining_qty;self.fill()
        self.assertEqual(self.pos().remaining_qty,q)
        self.assertEqual(el.connection().execute('select count(*) from execution_fills').fetchone()[0],1)
    def test_missing_product_never_applies_fill(self):
        self.enter();self.book['O1'].raw.pop('producttype');self.book['O1'].filled_qty=65;self.book['O1'].avg_price=7.4
        self.assertFalse(el.refresh(self.ex.state.positions));self.assertEqual(self.pos().remaining_qty,0)
    def test_wrong_product_never_applies_fill(self):
        self.enter();self.book['O1'].raw['producttype']='INTRADAY';self.book['O1'].filled_qty=65;self.book['O1'].avg_price=7.4
        self.assertFalse(el.refresh(self.ex.state.positions));self.assertEqual(self.pos().remaining_qty,0)
    def test_full_fill_has_no_initial_stop(self):
        self.enter();self.fill();p=self.pos();self.assertIsNone(p.effective_stop)
        self.assertIsNone(ce.exit_reason(p,1.,False,False))
    def test_trail_boundary_breakeven_and_ratchet(self):
        self.enter();self.fill();p=self.pos();p.peak_price=p.entry_price*1.3
        ce.exit_reason(p,p.peak_price,False,False);self.assertGreaterEqual(p.effective_stop,p.entry_price)
        p.effective_stop=20.;ce.exit_reason(p,10.,False,False);self.assertEqual(p.effective_stop,20.)
    def test_partial_fill_trail_latches_and_cancels(self):
        self.enter();self.fill(qty=65,terminal=False);p=self.pos()
        self.quote['bid']=15.;ce.check_exits(self.ex)
        self.quote['bid']=10.;ce.check_exits(self.ex)
        self.assertEqual(p.exit_reason,'cas_peak_trail');self.assertTrue(self.cancels)
        self.assertEqual(len(self.sent),1)  # No SELL before terminal BUY evidence.
    def test_late_buy_fill_then_one_sell(self):
        self.enter();self.fill(qty=65,terminal=False);p=self.pos();p.exit_reason='cas_peak_trail'
        self.fill();self.quote['bid']=7.;ce.check_exits(self.ex);ce.check_exits(self.ex)
        self.assertEqual(len(self.sent),2);self.assertEqual(self.sent[1]['quantity'],self.sent[0]['quantity'])
        self.assertEqual(self.sent[1]['producttype'],'CARRYFORWARD')
    def test_pending_eod_zero_fill_is_cancelled(self):
        self.enter();p=self.pos();self.now=NOW.replace(minute=38)
        ce.check_exits(self.ex);self.assertEqual(p.exit_reason,'eod_force_close');self.assertTrue(self.cancels)
        self.assertEqual(len(self.sent),1)
    def test_eod_missing_quote_no_guessed_sell(self):
        self.enter();self.fill();p=self.pos();self.now=NOW.replace(minute=38);self.fq.side_effect=lambda *a,**kw:None
        ce.check_exits(self.ex);self.assertEqual(p.exit_reason,'eod_force_close');self.assertEqual(len(self.sent),1)
    def test_durable_kill_still_exits_after_restart(self):
        self.enter();self.fill();celestial.latch_kill();ce.check_exits(self.ex)
        self.assertEqual(self.sent[-1]['transactiontype'],'SELL')
        self.assertEqual(self.pos().exit_reason,'cas_kill_exit')
    def test_paused_control_does_not_disable_exit(self):
        self.enter();self.fill();p=self.pos();p.exit_reason='operator_exit';celestial.update_config({'auto_trade_enabled':False})
        ce.check_exits(self.ex);self.assertEqual(self.sent[-1]['transactiontype'],'SELL')
    def test_no_generic_structure_stop_for_cas(self):
        self.enter();self.fill();p=self.pos()
        with patch.object(self.ex,'_structure',return_value=(22600,1.)),patch('kadima.trade_policy.premium_stop',return_value=5.):
            self.ex.update_structure('NIFTY',self.ch,22700.)
        self.assertIsNone(p.structural_stop)
    def test_cas_blocks_other_strategy_capacity(self):
        self.enter();self.fill();self.assertEqual(portfolio_capacity.entry_block(self.ex.state.positions,[]),'cas_owns_account_slot')
    def test_watchdog_ownership_uses_live_fills(self):
        self.enter();self.fill();p=self.pos()
        self.assertEqual(cas_watchdog.owned_positions()[('NFO','T')]['quantity'],p.remaining_qty)
    def test_watchdog_excludes_paper(self):
        self.enter();self.fill();c=el.connection();c.execute('update execution_fills set dry_run=1');c.commit()
        self.assertEqual(cas_watchdog.owned_positions(),{})
    def test_manual_same_contract_never_absorbed(self):
        self.enter();self.fill();p=self.pos();q=p.remaining_qty
        broker=[dict(exchange='NFO',symboltoken='T',producttype='CARRYFORWARD',netqty=q+65)]
        unresolved=external_reconciliation.reconcile(self.ex,broker,list(self.book.values()))
        self.assertIn(str(p.signal_id),unresolved);self.assertEqual(p.remaining_qty,q)
    def test_restart_restores_kind_stop_and_pending_intent(self):
        self.enter();self.fill();p=self.pos();p.trail_active=True;p.effective_stop=8.;p.exit_reason='cas_peak_trail';storage.upsert_position(p)
        rows=storage.load_open_positions();self.assertEqual(rows[0]['kind'],'cas_blast');self.assertEqual(rows[0]['effective_stop'],8.)
        self.assertEqual(rows[0]['exit_reason'],'cas_peak_trail')
    def test_manual_cas_exit_uses_fresh_bid_path(self):
        self.enter();self.fill();r=self.ex.manual_exit(self.pos().signal_id)
        self.assertEqual(r['reason'],'cas_exit_requested_not_fill_confirmation');self.assertEqual(len(self.sent),2)

class Scanner(unittest.TestCase):
    def run_scanner(self,*,gap=False,reset=False,duplicate=False,wide=False,twice=False):
        fired=[];scanner=scan.CasBlastScanner(executor_hook=lambda c,ch:fired.append(c))
        ch=chain();clock=NOW-timedelta(seconds=70);volume=0
        with patch.object(scan,'now_ist',side_effect=lambda:clock):
            for i in range(6):
                clock+=timedelta(seconds=10);volume+=1000
                q=ch.quotes['T'];q.ltp=5.;q.volume=volume;q.quote_at=q.trade_at=clock.isoformat()
                ch._cas_spot_at=clock;ch._cas_spot=22660.;scanner.on_chain('NIFTY',ch,22660.)
            clock+=timedelta(seconds=120 if gap else 10)
            if reset:volume=10
            elif not duplicate:volume+=10000
            q=ch.quotes['T'];q.ltp=7.5;q.volume=volume
            q.bid=2. if wide else 7.4;q.ask=7.6
            if not duplicate:q.quote_at=q.trade_at=clock.isoformat()
            ch._cas_spot_at=clock;ch._cas_spot=22690.;scanner.on_chain('NIFTY',ch,22690.)
            if twice:
                clock+=timedelta(seconds=10);q.quote_at=q.trade_at=clock.isoformat();q.volume+=20000;q.ltp=9.;q.bid=8.9;q.ask=9.1
                ch._cas_spot_at=clock;ch._cas_spot=22700.;scanner.on_chain('NIFTY',ch,22700.)
        return fired
    def test_real_continuous_trigger(self):self.assertEqual(len(self.run_scanner()),1)
    def test_gap_does_not_trigger(self):self.assertEqual(self.run_scanner(gap=True),[])
    def test_volume_reset_does_not_trigger(self):self.assertEqual(self.run_scanner(reset=True),[])
    def test_duplicate_source_does_not_trigger(self):self.assertEqual(self.run_scanner(duplicate=True),[])
    def test_wide_book_not_recommended(self):self.assertEqual(self.run_scanner(wide=True),[])
    def test_unfilled_or_blocked_dispatch_can_retry_fresh_sample(self):self.assertEqual(len(self.run_scanner(twice=True)),2)

class Stepping(unittest.TestCase):
    setUp=Runtime.setUp
    tearDown=Runtime.tearDown
    enter=Runtime.enter
    pos=Runtime.pos
    fill=Runtime.fill
    def steps(self):
        p=self.pos();revision=celestial.read_config()['revision']
        with patch.object(self.ex._stop,'wait',return_value=False):
            REAL_STEP(p,'O1',self.sent[0]['quantity'],self.sent[0]['price'],9.7,self.cand,self.ch,revision)
    def test_known_order_reprices_then_retains_pending_cancel(self):
        self.enter();self.steps()
        self.assertTrue(self.modifies);self.assertTrue(self.cancels)
        self.assertTrue(el.pending());self.assertEqual(self.pos().remaining_qty,0)
        self.assertTrue(all(k['price']<=9.7 and k['contract']['producttype']=='CARRYFORWARD' for _,k in self.modifies))
    def test_partial_fill_stops_price_increases(self):
        self.enter();self.fill(qty=65,terminal=False);self.steps()
        self.assertFalse(self.modifies);self.assertTrue(self.cancels)
        self.assertEqual(self.pos().remaining_qty,65)
    def test_pause_before_reprice_cancels_only(self):
        self.enter();celestial.update_config({'auto_trade_enabled':False});self.steps()
        self.assertFalse(self.modifies);self.assertTrue(self.cancels)
    def test_kill_between_quote_and_modify_cancels_only(self):
        self.enter()
        def quote(*a,**kw):ce.killswitch.engaged.return_value=True;return self.quote
        self.fq.side_effect=quote;self.steps()
        self.assertFalse(self.modifies);self.assertTrue(self.cancels)
    def test_unknown_order_book_cannot_reprice(self):
        self.enter();ao.fetch_order_book.side_effect=lambda:None;self.steps()
        self.assertFalse(self.modifies);self.assertTrue(el.pending())
    def test_uncertain_modify_cannot_continue_price_increases(self):
        self.enter();ao.modify_order.side_effect=lambda *a,**k:ao.OrderResult(False,'O1','unknown',uncertain=True)
        self.steps();self.assertEqual(ao.modify_order.call_count,1);self.assertTrue(el.pending())
    def test_rejected_cancel_does_not_free_account_slot(self):
        self.enter();ao.cancel_order.side_effect=lambda *a:ao.OrderResult(False,'O1','unknown',uncertain=True)
        self.steps();self.assertTrue(el.pending());self.assertFalse(self.enter()['ok']);self.assertEqual(len(self.sent),1)
    def test_cancel_race_late_fill_is_booked_once(self):
        self.enter()
        def cancel(oid):
            self.book[oid].filled_qty=self.sent[0]['quantity'];self.book[oid].avg_price=7.6;self.book[oid].status='complete'
            return ao.OrderResult(True,oid,'accepted')
        ao.cancel_order.side_effect=cancel;self.steps();self.ex.refresh_orders();q=self.pos().remaining_qty
        self.ex.refresh_orders();self.assertEqual(self.pos().remaining_qty,q);self.assertGreater(q,0)
    def test_expired_cas_window_never_reprices(self):
        self.enter();self.now=NOW.replace(minute=35);self.steps()
        self.assertFalse(self.modifies);self.assertTrue(self.cancels)
    def test_filled_order_not_modified_or_cancelled_again(self):
        self.enter();self.fill();self.steps();self.assertFalse(self.modifies)
    def test_stale_exit_quote_never_repriced_from_ltp_fallback(self):
        self.enter();self.fill();p=self.pos();p.exit_reason='manual_exit';ce.check_exits(self.ex)
        self.now+=timedelta(seconds=4);self.fq.side_effect=lambda *a,**k:None
        with patch.object(ae,'_fresh_ltp',return_value=999.):self.ex.refresh_orders()
        self.assertFalse(self.modifies)
    def test_exit_bid_reprices_same_id_and_keeps_product(self):
        self.enter();self.fill();p=self.pos();p.exit_reason='manual_exit';ce.check_exits(self.ex)
        self.now+=timedelta(seconds=4);self.quote['bid']=6.0;self.quote['quote_at']=self.now.isoformat()
        self.ex.refresh_orders()
        self.assertEqual(self.modifies[-1][0],'O2');self.assertEqual(self.modifies[-1][1]['price'],6.)
        self.assertEqual(self.modifies[-1][1]['contract']['producttype'],'CARRYFORWARD')
    def test_five_paise_bid_is_not_zero_limit(self):
        self.enter();self.fill();p=self.pos();p.exit_reason='manual_exit';self.quote['bid']=.05
        ce.check_exits(self.ex);self.assertEqual(self.sent[-1]['price'],.05)

class Ownership(unittest.TestCase):
    setUp=Runtime.setUp
    tearDown=Runtime.tearDown
    enter=Runtime.enter
    pos=Runtime.pos
    fill=Runtime.fill
    def broker_position(self,p,qty=None):
        return dict(exchange=p.exchange,symboltoken=p.token,producttype='CARRYFORWARD',netqty=p.remaining_qty if qty is None else qty,
                    tradingsymbol=p.symbol,buyavgprice=p.entry_price)
    def test_real_restart_recovers_protection(self):
        self.enter();self.fill();p=self.pos();p.trail_active=True;p.effective_stop=8.;p.exit_reason='cas_peak_trail';storage.upsert_position(p)
        restarted=ae.AutoExecutor();restarted.celestial_master=self.ex.celestial_master
        with patch.object(ae,'fetch_broker_positions',return_value=[self.broker_position(p)]):
            restarted.reconcile_on_startup()
        restored=restarted.state.positions[str(p.signal_id)]
        self.assertEqual(restored.effective_stop,8.);self.assertEqual(ao.product_for(restored),'CARRYFORWARD')
        self.assertIn(str(p.signal_id),restarted._celestial_verified)
    def test_unrelated_pending_order_keeps_verified_exit(self):
        self.enter();self.fill();p=self.pos()
        self.book['MAN']=ao.OrderStatus('MAN','open',0,None,dict(exchange='NFO',symboltoken='OTHER',ordertag='manual'))
        with patch.object(ae,'fetch_broker_positions',return_value=[self.broker_position(p)]):
            ae.AutoExecutor.reconcile_on_startup(self.ex)
        self.assertTrue(self.ex._reconcile_blocked);self.assertIn(str(p.signal_id),self.ex._celestial_verified)
    def test_same_contract_pending_order_revokes_exit_ownership(self):
        self.enter();self.fill();p=self.pos()
        self.book['MAN']=ao.OrderStatus('MAN','open',0,None,dict(exchange='NFO',symboltoken='T',ordertag='manual'))
        with patch.object(ae,'fetch_broker_positions',return_value=[self.broker_position(p)]):
            ae.AutoExecutor.reconcile_on_startup(self.ex)
        self.assertNotIn(str(p.signal_id),self.ex._celestial_verified)
    def test_unknown_pending_identity_revokes_exit_ownership(self):
        self.enter();self.fill();p=self.pos();self.book['MAN']=ao.OrderStatus('MAN','open',0,None,{})
        with patch.object(ae,'fetch_broker_positions',return_value=[self.broker_position(p)]):
            ae.AutoExecutor.reconcile_on_startup(self.ex)
        self.assertNotIn(str(p.signal_id),self.ex._celestial_verified)
    def test_watchdog_never_direct_sells_cas(self):
        self.enter();self.fill();p=self.pos()
        with patch.object(eod_watchdog,'_fetch_positions',return_value=[self.broker_position(p)]),\
             patch.object(eod_watchdog,'now_ist',return_value=NOW.replace(minute=39)),\
             patch.object(eod_watchdog,'_close_position') as direct,\
             patch.object(cas_watchdog,'request_exit',return_value=True) as request:
            self.assertEqual(eod_watchdog.main(),1);direct.assert_not_called();request.assert_called_once_with(p.signal_id)
    def test_watchdog_mixed_manual_quantity_is_not_sold(self):
        self.enter();self.fill();p=self.pos()
        with patch.object(eod_watchdog,'_fetch_positions',return_value=[self.broker_position(p,p.remaining_qty+65)]),\
             patch.object(eod_watchdog,'now_ist',return_value=NOW.replace(minute=39)),\
             patch.object(eod_watchdog,'_close_position') as direct,\
             patch.object(cas_watchdog,'request_exit') as request:
            self.assertEqual(eod_watchdog.main(),1);direct.assert_not_called();request.assert_not_called()
    def test_watchdog_manual_carryforward_untouched(self):
        r=dict(exchange='NFO',symboltoken='MAN',producttype='CARRYFORWARD',netqty=65,tradingsymbol='MANUAL')
        with patch.object(eod_watchdog,'_fetch_positions',return_value=[r]),\
             patch.object(eod_watchdog,'now_ist',return_value=NOW.replace(minute=39)),\
             patch.object(eod_watchdog,'_close_position') as direct,\
             patch.object(cas_watchdog,'request_exit') as request:
            eod_watchdog.main();direct.assert_not_called();request.assert_not_called()
    def test_two_concurrent_cas_entries_share_one_slot(self):
        results=[]
        def attempt():results.append(self.enter())
        threads=[threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(results),2);self.assertEqual(len(self.sent),1)
        self.assertEqual(sum(bool(r['ok']) for r in results),1)
    def test_startup_registers_cas_watcher_while_execution_disabled(self):
        ce.CAS_EXECUTION_ENABLED=False
        with patch.object(ae.threading,'Thread') as factory:
            self.ex.start_monitor_thread()
            self.watcher.assert_called_once_with(self.ex)
            self.assertEqual(factory.call_args.kwargs['name'],'auto-executor-monitor')

if __name__=='__main__':
    unittest.main(verbosity=2)
