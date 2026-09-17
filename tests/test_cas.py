"""CAS chronological detection and broker-boundary regressions; no live orders."""
from dataclasses import asdict
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from kadima import cas_engine as c, cas_orders as co, auto_executor as ae, storage
from kadima import execution_ledger as ledger, angel_orders as orders
from kadima.config import IST
from kadima.option_chain import OptionQuote, OptionChain

NOW = datetime(2026, 9, 17, 14, 0, tzinfo=IST)


class CAS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(storage, 'DATA_DIR', self.tmp.name),
                       patch.object(storage, 'DB_PATH', str(Path(self.tmp.name)/'test.db')),
                       patch.object(storage, '_local', threading.local()),
                       patch.object(c, 'live_blocks', return_value=[]),
                       patch.object(c.celestial, 'news_impact', return_value={'news_blocked':False}),
                       patch.object(ae.audit, 'log'), patch.object(ae.alerts, 'enqueue'),
                       patch.object(ae.SESSION, 'ensure', side_effect=AssertionError('Network forbidden'))]
        for p in self.patches: p.start()
        self.lane = c.Lane()
        self.q = OptionQuote('T', 'SENSEX17SEP2674400PE', 74400, 'PE', ltp=15,
                             bid=14.95, ask=15.05, volume=1000, option_vwap=14.2,
                             quote_at=NOW.isoformat(), bid_qty=1000, ask_qty=1000)
        master = NS(nearest_expiry=lambda index:'17SEP2026', strikes_around=lambda *a:[
            {'token':'T', 'symbol':self.q.symbol}])
        self.st = NS(name='SENSEX', spot=74414, is_live_tick=True, last_updated=NOW,
                     technical_at=NOW, snap=NS(vwap=74450, ema_fast=74440, rsi=48.5),
                     chain=NS(index=NS(strike_step=100, opt_exchange='BFO'), scrips=master, atm=74400))
        self.positions = {}

    def tearDown(self):
        con = getattr(storage._local, 'conn', None)
        if con: con.close()
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def sample(self, at, price, volume=1000, **changes):
        self.q.ltp, self.q.bid, self.q.ask = price, price-.05, price+.05
        self.q.quote_at, self.q.volume = at.isoformat(), volume
        for k,v in changes.items(): setattr(self.q,k,v)
        self.st.last_updated = self.st.technical_at = at
        self.st.chain._forecast_sample = dict(expiry='17SEP2026', mono=time.monotonic(), quotes=[asdict(self.q)])
        return self.lane.scan(self.st, self.positions, at)[1]

    def ready(self):
        self.sample(NOW-timedelta(seconds=60), 14.5, 1000)
        self.sample(NOW-timedelta(seconds=30), 14, 1100)
        return self.sample(NOW, 15, 1200)

    def position(self, row, qty=20, status='open'):
        sid = c.reserve(row, 10000, 2)
        p = ae.Position(sid,'cas','SENSEX','PE',self.q.symbol,'T','BFO',15.05,NOW,
                        1,20,qty,status=status,structural_stop=14.2975,effective_stop=14.2975,peak_price=15.05)
        self.positions[str(sid)] = p
        storage.upsert_position(p)
        return p

    def test_awakening_without_shield_score_oi_or_greeks(self):
        r = self.ready()
        self.assertEqual(r['stage'],'CAS ENTRY',r)
        self.assertTrue(r['eligible'],r)
        self.assertIsNone(self.q.oi)
        self.assertNotIn('score',r)
        self.assertLess(r['entry'],20)
        before = len(next(iter(self.lane.history.values())))
        self.lane.scan(self.st,self.positions,NOW)
        self.assertEqual(before,len(next(iter(self.lane.history.values()))))

    def test_symmetric_ce_awakening(self):
        self.q.opt_type='CE';self.q.symbol='SENSEX17SEP2674400CE'
        self.st.spot=74470
        self.ready()
        r=self.lane.scan(self.st,self.positions,NOW,False)[0]
        self.assertTrue(r['eligible'],r)

    def test_missing_stale_future_and_bad_quotes_never_executable(self):
        r=self.ready()
        for field,value in [('option_vwap',None),('quote_at',(NOW-timedelta(seconds=31)).isoformat()),
                ('quote_at',(NOW+timedelta(seconds=1)).isoformat()),('ask',float('nan')),
                ('bid',16),('ask_qty',0),('volume',None)]:
            with self.subTest(field=field,value=value):
                data=asdict(self.q);data[field]=value
                self.st.chain._forecast_sample['quotes']=[data]
                self.assertFalse(self.lane.scan(self.st,self.positions,NOW,False)[1]['eligible'])
        self.st.chain._forecast_sample['quotes']=[asdict(self.q)]
        self.st.chain._forecast_sample['expiry']='24SEP2026'
        self.assertIn('not_contract_expiry_day',self.lane.scan(self.st,self.positions,NOW,False)[1]['blocked_reasons'])

    def test_reclaim_and_underlying_support_required(self):
        self.ready()
        self.st.spot=74500
        self.assertIn('underlying_opposes',self.lane.scan(self.st,self.positions,NOW,False)[1]['blocked_reasons'])
        self.st.spot=74414
        self.q.option_vwap=16
        self.st.chain._forecast_sample['quotes']=[asdict(self.q)]
        self.assertFalse(self.lane.scan(self.st,self.positions,NOW,False)[1]['eligible'])

    def test_history_resets_on_gap_or_volume_reset(self):
        self.ready()
        self.assertFalse(self.sample(NOW+timedelta(seconds=120),16,1300)['eligible'])
        self.assertFalse(self.sample(NOW+timedelta(seconds=150),17,10)['eligible'])

    def test_large_move_locks_after_restart_and_cannot_hop_strike(self):
        self.ready()
        r=self.sample(NOW+timedelta(seconds=30),30,1300)
        self.assertEqual(r['stage'],'CAS LOCK',r)
        self.lane=c.Lane();self.q.token='NEW';self.q.strike=74500
        r=self.sample(NOW+timedelta(seconds=60),10,1400)
        self.assertEqual(r['stage'],'CAS LOCK',r)

    def test_add_waits_for_fill_and_expansion_and_only_once(self):
        r=self.ready();p=self.position(r,qty=10,status='entry_pending')
        self.sample(NOW+timedelta(seconds=30),17,1300)
        r=self.sample(NOW+timedelta(seconds=60),18,1400)
        self.assertIn('awaiting_broker_fill',r['blocked_reasons'])
        p.status='open'
        r=self.lane.scan(self.st,self.positions,NOW+timedelta(seconds=60),False)[1]
        self.assertEqual(r['stage'],'CAS ADD',r);self.assertTrue(r['eligible'],r)
        self.assertTrue(c.reserve_add(r['episode']));self.assertFalse(c.reserve_add(r['episode']))
        self.assertFalse(self.lane.scan(self.st,self.positions,NOW+timedelta(seconds=60),False)[1]['eligible'])

    def test_exit_then_lock_survives_restart(self):
        r=self.ready();p=self.position(r)
        p.exit_reason='manual_exit_dashboard';p.status='exit_pending'
        r=self.lane.scan(self.st,self.positions,NOW,False)[1]
        self.assertEqual(r['stage'],'CAS EXIT',r)
        self.positions.clear();self.lane=c.Lane()
        self.assertEqual(self.lane.scan(self.st,self.positions,NOW)[1]['stage'],'CAS LOCK')

    def test_protection_decays_trails_and_no_progress_without_entry_flag(self):
        r=self.ready();p=self.position(r)
        self.assertEqual(c.exit_reason(p,14,None,NOW),'cas_premium_stop_or_profit_trail')
        p.peak_price=30
        self.assertIsNone(c.exit_reason(p,30,None,NOW))
        self.assertIsNotNone(c.exit_reason(p,26,None,NOW))
        p.peak_price=15.05
        self.assertEqual(c.exit_reason(p,15,None,NOW+timedelta(minutes=2)),'cas_no_expansion_2min')
        self.assertEqual(c.exit_reason(p,0,None,NOW.replace(hour=15,minute=28)),'cas_expiry_decay_exit')
        p.kind='bull_shield';self.assertIsNone(c.exit_reason(p,1,None,NOW))

    def test_raw_full_sample_preserves_vwap_timestamp_and_depth(self):
        chain=object.__new__(OptionChain)
        chain.quotes={'T':self.q};chain.index=NS(name='SENSEX',opt_exchange='BFO')
        chain._expiry='17SEP2026';chain.velocity=NS(add=Mock(),mark_degraded=Mock())
        raw=dict(symbolToken='T',exchange='BFO',ltp=15,opnInterest=0,tradeVolume=1000,
                 avgPrice=14.2,exchFeedTime='17-Sep-2026 14:00:00',
                 depth={'buy':[{'price':14.95,'quantity':100}], 'sell':[{'price':15.05,'quantity':200}]})
        self.assertTrue(chain.accept_full_sample([raw]))
        q=OptionQuote(**chain._forecast_sample['quotes'][0])
        self.assertTrue(c.quote_valid(q,NOW));self.assertEqual(q.ask_qty,200)
        raw.pop('avgPrice');self.assertTrue(chain.accept_full_sample([raw]))
        self.assertFalse(c.quote_valid(chain.quotes['T'],NOW))

    def test_distinct_entry_enable_and_pause_kill_expiry_controls(self):
        self.patches[3].stop()
        with patch.dict(os.environ,{'KADIMA_CAS_LIVE':'1'}), \
             patch.object(c.celestial,'enabled',return_value=True), \
             patch.object(c.celestial,'read_config',return_value={'auto_trade_enabled':True,'kill_latched':False}) as cfg:
            self.assertEqual(c.live_blocks(NOW),[])
            cfg.return_value.update(auto_trade_enabled=False,kill_latched=True)
            self.assertEqual(set(c.live_blocks(NOW)),{'auto_paused','kill_latched'})
            self.assertIn('cas_entry_window_closed',c.live_blocks(NOW.replace(hour=15,minute=25)))


class Orders(CAS):
    def setUp(self):
        super().setUp()
        self.ex=ae.AutoExecutor();self.ex.state.positions=self.positions
        self.ex._reconcile_blocked=False;self.ex._positions_verified=True
        more=[patch.object(ae,'ORDER_PLACEMENT_ENABLED',True),patch.object(ae,'MAX_LOTS_PER_SIGNAL',2),
              patch.object(ae,'now_ist',return_value=NOW),patch.object(co,'now_ist',return_value=NOW),
              patch.object(ledger,'now_ist',return_value=NOW),patch.object(ae,'in_market_hours',return_value=True),
              patch.object(ae,'past_signal_cutoff',return_value=False),patch.object(ae.killswitch,'engaged',return_value=False),
              patch.object(ae.exit_manager,'realized_pnl_today',return_value=0),
              patch.object(ae.capital_guard,'get_capital_for_signal',return_value=10000),
              patch.object(ae.capital_guard,'enforce',return_value=(1,'ok')),
              patch.object(ae,'get_lot_size',return_value=20),
              patch.object(co,'fetch_quote',side_effect=lambda *a,**kw:self.q),
              patch.object(orders,'place_limit_order',return_value=orders.OrderResult(True,'O1','ok'))]
        self.patches+=more
        for p in more:p.start()
        self.place=orders.place_limit_order

    def fill(self,p,qty,avg,status='complete',oid='O1'):
        r=ledger.pending(p.signal_id)[0]
        ledger.apply_fill(p,r,orders.OrderStatus(oid,status,qty,avg,{}))

    def test_real_adapter_entry_partial_fill_add_cost_and_dedupe(self):
        r=self.ready();result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertTrue(result['ok'],result);p=next(iter(self.positions.values()))
        self.assertEqual(p.remaining_qty,0)
        self.fill(p,10,15.1,'open');self.assertEqual(p.remaining_qty,10)
        self.fill(p,10,15.1,'cancelled');self.assertEqual(p.status,'open')
        later=NOW+timedelta(seconds=60)
        self.sample(NOW+timedelta(seconds=30),17,1300)
        r=self.sample(later,18,1400)
        with patch.object(co,'now_ist',return_value=later):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertTrue(result['ok'],result)
        self.fill(p,5,18.1,'open','O2');self.fill(p,5,18.1,'open','O2')
        self.assertEqual(p.remaining_qty,15)
        self.fill(p,20,18.1,'complete','O2')
        self.assertEqual(p.remaining_qty,30)
        self.assertAlmostEqual(p.entry_price,(10*15.1+20*18.1)/30)
        self.assertEqual(self.place.call_count,2)
        with patch.object(co,'now_ist',return_value=later):
            self.assertFalse(self.ex.evaluate_cas(self.st,self.lane,r)['ok'])
        self.assertEqual(self.place.call_count,2)

    def test_blocked_admission_never_sends_broker_order(self):
        r=self.ready()
        for mode in ('paused','unreconciled','daily_loss','depth','price','funds','changed'):
            with self.subTest(mode=mode):
                with patch.object(co,'account_block',return_value=mode if mode in ('paused','unreconciled','daily_loss') else None), \
                     patch.object(ae.capital_guard,'enforce',return_value=(0,'no_cash') if mode=='funds' else (1,'ok')):
                    oldqty,oldask=self.q.ask_qty,self.q.ask
                    if mode=='depth':self.q.ask_qty=1
                    if mode=='price':self.q.ask=30
                    if mode=='changed':self.st.spot=74500
                    self.assertFalse(self.ex.evaluate_cas(self.st,self.lane,r)['ok'])
                    self.q.ask_qty,self.q.ask=oldqty,oldask;self.st.spot=74414
        self.place.assert_not_called()

    def test_rejected_add_preserves_existing_position_and_stop(self):
        r=self.ready();self.ex.evaluate_cas(self.st,self.lane,r)
        p=next(iter(self.positions.values()));self.fill(p,20,15.1)
        self.sample(NOW+timedelta(seconds=30),17,1300)
        later=NOW+timedelta(seconds=60);r=self.sample(later,18,1400)
        self.place.return_value=orders.OrderResult(False,None,'rejected')
        with patch.object(co,'now_ist',return_value=later):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertFalse(result['ok']);self.assertEqual(p.status,'open')
        self.assertEqual(p.remaining_qty,20);self.assertEqual(p.entry_price,15.1)
        self.assertGreater(p.effective_stop,0)

    def test_unknown_submission_blocks_replay_after_restart(self):
        r=self.ready();self.place.side_effect=TimeoutError()
        result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertEqual(result['reason'],'submission_unknown')
        self.lane=c.Lane()
        row=self.lane.scan(self.st,self.positions,NOW)[1]
        self.assertFalse(row['eligible'])
        self.assertEqual(len(ledger.pending()),1);self.assertEqual(self.place.call_count,1)

    def test_exit_monitor_submits_only_owned_quantity_and_keeps_pending_state(self):
        r=self.ready();p=self.position(r)
        p.peak_price=20
        self.q.ltp=18;self.q.bid=17.95;self.q.ask=18.05
        with patch.object(c.celestial,'enabled',return_value=False):
            self.ex.check_exits()
        args=self.place.call_args.kwargs
        self.assertEqual(args['transactiontype'],'SELL');self.assertEqual(args['quantity'],20)
        self.assertEqual(p.remaining_qty,20);self.assertEqual(p.status,'exit_pending')
        self.assertIsNotNone(p.exit_reason)
        self.ex.check_exits();self.assertEqual(self.place.call_count,1)

    def test_expiry_exit_latches_without_quote_and_executes_on_recovery(self):
        r=self.ready();p=self.position(r)
        later=NOW.replace(hour=15,minute=28)
        with patch.object(c.celestial,'enabled',return_value=False), \
             patch.object(ae,'now_ist',return_value=later),patch.object(co,'fetch_quote',return_value=None):
            self.ex.check_exits()
        self.assertEqual(p.exit_reason,'cas_expiry_decay_exit');self.place.assert_not_called()
        with patch.object(c.celestial,'enabled',return_value=False),patch.object(ae,'now_ist',return_value=later):
            self.ex.check_exits()
        self.assertEqual(self.place.call_args.kwargs['transactiontype'],'SELL')

    def test_pending_add_exit_waits_for_terminal_cancel_then_sells_total_fill(self):
        r=self.ready();self.ex.evaluate_cas(self.st,self.lane,r)
        p=next(iter(self.positions.values()));self.fill(p,20,15.1)
        self.sample(NOW+timedelta(seconds=30),17,1300)
        later=NOW+timedelta(seconds=60);r=self.sample(later,18,1400)
        with patch.object(co,'now_ist',return_value=later):self.ex.evaluate_cas(self.st,self.lane,r)
        self.fill(p,5,18.1,'open','O2')
        self.q.bid=14;self.q.ask=14.1
        with patch.object(c.celestial,'enabled',return_value=False),patch.object(orders,'cancel_order') as cancel:
            self.ex.check_exits()
            cancel.assert_called_once_with('O2')
            self.assertEqual(self.place.call_count,2)
            self.fill(p,7,18.1,'cancelled','O2')
            self.ex.check_exits()
        self.assertEqual(self.place.call_args.kwargs['transactiontype'],'SELL')
        self.assertEqual(self.place.call_args.kwargs['quantity'],27)

    def test_add_cannot_reallocate_budget_or_exceed_total_lot_cap(self):
        r=self.ready();p=self.position(r)
        self.sample(NOW+timedelta(seconds=30),17,1300)
        later=NOW+timedelta(seconds=60);r=self.sample(later,18,1400)
        saved=c.run(r['episode']);sid=saved.pop('signal_id');saved['allocation']=310
        db=c.connection()
        import json
        with db:db.execute('UPDATE cas_runs SET payload=? WHERE signal_id=?',(json.dumps(saved),sid))
        with patch.object(co,'now_ist',return_value=later):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertEqual(result['reason'],'cas_total_episode_budget_exceeded');self.place.assert_not_called()
        p.remaining_qty=40
        with patch.object(co,'now_ist',return_value=later):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertEqual(result['reason'],'cas_lot_cap_or_depth_insufficient');self.place.assert_not_called()

    def test_exit_quote_does_not_require_entry_spread_or_vwap(self):
        # Exercise the real FULL parser, bypassing only the network boundary.
        self.patches[-2].stop()
        raw=dict(symbolToken='T',exchange='BFO',ltp=14,tradeVolume=1000,
                 exchFeedTime=NOW.isoformat(),depth={'buy':[{'price':13,'quantity':1}],
                                                   'sell':[{'price':20,'quantity':100}]})
        with patch.object(ae.SESSION,'ensure'), \
             patch.object(ae.SESSION,'api',NS(getMarketData=lambda **kw: {'status':True,'data':{'fetched':[raw]}})), \
             patch.object(ae.QUOTE,'wait_and_allow',return_value=True):
            self.assertIsNone(co.fetch_quote('BFO','T',self.q.symbol,74400,'PE'))
            q=co.fetch_quote('BFO','T',self.q.symbol,74400,'PE',for_exit=True)
            self.assertEqual(q.bid,13);self.assertIsNone(q.option_vwap)
            raw['exchFeedTime']=(NOW-timedelta(seconds=31)).isoformat()
            self.assertIsNone(co.fetch_quote('BFO','T',self.q.symbol,74400,'PE',for_exit=True))

    def test_user_capital_cap_is_saved_and_lower_available_funds_preserved(self):
        r=self.ready()
        with patch.object(ae.capital_guard,'get_capital_for_signal',return_value=30000):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertTrue(result['ok'],result)
        self.assertEqual(c.run(r['episode'])['allocation'],15000)
        self.assertEqual(next(iter(self.positions.values())).allocated_capital,15000)

    def test_initial_allocation_does_not_force_spending_15000(self):
        r=self.ready()
        with patch.object(ae.capital_guard,'get_capital_for_signal',return_value=10000):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertTrue(result['ok'],result)
        self.assertEqual(c.run(r['episode'])['allocation'],10000)
        self.assertEqual(self.place.call_args.kwargs['quantity'],20)

    def test_restored_larger_allocation_cannot_add_beyond_15000_total(self):
        import json
        r=self.ready();p=self.position(r,qty=500);p.lot_size=500
        saved=c.run(r['episode']);sid=saved.pop('signal_id');saved['allocation']=30000
        db=c.connection()
        with db:db.execute('UPDATE cas_runs SET payload=? WHERE signal_id=?',(json.dumps(saved),sid))
        self.sample(NOW+timedelta(seconds=30),17,1300)
        later=NOW+timedelta(seconds=60);r=self.sample(later,18,1400)
        with patch.object(co,'now_ist',return_value=later),patch.object(ae,'get_lot_size',return_value=500):
            result=self.ex.evaluate_cas(self.st,self.lane,r)
        self.assertEqual(result['reason'],'cas_total_episode_budget_exceeded')
        self.place.assert_not_called()
