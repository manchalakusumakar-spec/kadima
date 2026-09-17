import copy
import os
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import Mock, patch

from kadima import fast_momentum as fm
from kadima.config import IST
from kadima.option_chain import OptionQuote
import test_execution_lifecycle as f
import test_celestial as cf

NOW = datetime(2026,9,17,10,0,tzinfo=IST)


def candles(direction='CE'):
    one=[]
    values=[(1000,1001,999,1000)]*5+[(1001,1003,1000.8,1002),(1002,1003,1001,1002.5)]
    for i,(o,h,l,c) in enumerate(values):
        one.append(dict(start_ts=(NOW-timedelta(minutes=7-i)).isoformat(),open=o,high=h,low=l,close=c,closed=1))
    five=[dict(start_ts=(NOW-timedelta(minutes=15-5*i)).isoformat(),open=998+i,high=1001+i,
               low=996+i,close=998+i,closed=1) for i in range(3)]
    if direction=='PE':
        for r in one+five:
            r.update(open=2000-r['open'],high=2000-r['low'],low=2000-r['high'],close=2000-r['close'])
    return one,five


class Detection(unittest.TestCase):
    def test_live_requires_explicit_flag_threshold_news_and_controls(self):
        cfg=dict(auto_trade_enabled=True,kill_latched=False,score_threshold=65)
        health=dict(ok=True,ts=NOW.isoformat())
        impact=dict(news_available=True,news_blocked=False)
        with patch.dict(os.environ,{'KADIMA_FAST_MOMENTUM_LIVE':'0'}), \
             patch.object(fm.celestial,'read_config',return_value=cfg), \
             patch.object(fm.celestial,'enabled',return_value=True) as enabled, \
             patch.object(fm.celestial,'health',return_value=health), \
             patch.object(fm.celestial,'news_impact',return_value=impact):
            self.assertEqual(fm.live_blocks('NIFTY','CE',NOW,80),['fast_observation_only'])
            with patch.dict(os.environ,{'KADIMA_FAST_MOMENTUM_LIVE':'1'}):
                self.assertEqual(fm.live_blocks('NIFTY','CE',NOW,80),[])
                cfg.update(auto_trade_enabled=False,kill_latched=True,score_threshold=90)
                health['ts']=(NOW-timedelta(minutes=6)).isoformat()
                impact['news_blocked']=True;enabled.return_value=False
                self.assertEqual(set(fm.live_blocks('NIFTY','CE',NOW,80)),
                    {'rollout_disabled','auto_paused','kill_latched','below_threshold',
                     'fresh_news_coverage_required','news_risk_block'})

    def test_symmetric_breakout_and_hold_without_oi_direction(self):
        for d,spot in [('CE',1002.5),('PE',997.5)]:
            one,five=candles(d)
            r=fm.setup(one,five,spot,NOW)
            self.assertEqual(r['direction'],d)
            self.assertEqual(r['spot_stop'],1000.8 if d=='CE' else 999.2)

    def test_partial_future_stale_gap_and_failed_hold_rejected(self):
        for mode in ('partial','future','stale','gap','failed','opposing','extended'):
            one,five=candles();now=NOW;spot=1002.5
            if mode=='partial':one[-1]['closed']=0
            if mode=='future':now-=timedelta(seconds=1)
            if mode=='stale':now+=timedelta(minutes=3)
            if mode=='gap':one.pop(2)
            if mode=='failed':one[-1]['low']=998
            if mode=='opposing':five[0]['close']=1001
            if mode=='extended':spot=1010
            with self.subTest(mode=mode),self.assertRaises(ValueError):fm.setup(one,five,spot,now)

    def test_scenarios_use_real_short_horizons_and_expiry_cutoff(self):
        one,_=candles()
        q=NS(iv=25,strike=1000,ltp=12,opt_type='CE')
        r=fm.scenarios(q,1002.5,one,5,NOW)
        self.assertEqual([x['minutes'] for x in r],[5,10,15])
        self.assertTrue(all(x['low']<=x['central']<=x['high'] for x in r))
        self.assertEqual(fm.scenarios(q,1002.5,one,5,NOW.replace(hour=15,minute=29)),[])
        self.assertEqual(fm.scenarios(q,1002.5,one,.001,NOW),[])

    def test_volume_is_incremental_and_resets_after_gap(self):
        h=fm.PremiumHistory();q=NS(token='T',symbol='TEST',ltp=10,bid=9.99,ask=10.01,volume=1000)
        chain=NS(_expiry='22SEP2026',_last_poll=1,quotes={'T':q})
        for sec in range(0,181,30):
            chain._last_poll+=1
            q.volume=1000+sec if sec<=120 else 1120+3*(sec-120)
            q.ltp=10 if sec<=120 else 10+(sec-120)/100
            q.bid=q.ltp-.01;q.ask=q.ltp+.01
            h.record(chain,NOW+timedelta(seconds=sec))
        m=h.metrics(chain,q,NOW+timedelta(seconds=180))
        self.assertAlmostEqual(m['volume_ratio'],3)
        self.assertAlmostEqual(m['premium_move_pct'],6)
        before=len(next(iter(h.windows.values())))
        h.record(chain,NOW+timedelta(seconds=180))
        self.assertEqual(len(next(iter(h.windows.values()))),before)
        chain._last_poll+=1;h.record(chain,NOW+timedelta(seconds=300))
        self.assertIsNone(h.metrics(chain,q,NOW+timedelta(seconds=300)))

    def test_lane_exports_plan_without_mutating_broker_quotes(self):
        one,five=candles()
        q=OptionQuote('T','TESTCE',1000,'CE',ltp=12,bid=11.99,ask=12.01,
                      volume=1000000,oi=1000000,delta=.5,iv=25)
        chain=NS(_expiry='22SEP2026',_last_poll=time.monotonic(),index=NS(token='U',opt_exchange='NFO'),
                 quotes={'T':q},walls=lambda:{'CE':[],'PE':[]})
        st=NS(name='NIFTY',spot=1002.5,last_updated=NOW,chain=chain)
        lane=fm.Lane();original=copy.copy(q)
        with patch.object(fm.storage,'recent_candles',side_effect=lambda token,interval,limit:one if interval==1 else five), \
             patch.object(fm.PremiumHistory,'metrics',return_value=dict(premium_move_pct=3,volume_ratio=2)), \
             patch.object(fm,'live_blocks',return_value=['fast_observation_only']), \
             patch.object(fm,'scenarios',return_value=[dict(minutes=15,central=30,low=8,high=35)]):
            r=lane.scan(st,NOW)
        self.assertEqual(r['stage'],'SETUP_READY',r)
        self.assertFalse(r['eligible'])
        self.assertIn('fast_observation_only',r['blocked_reasons'])
        self.assertGreater(r['t2'],r['t1']);self.assertLess(r['stop'],r['entry'])
        self.assertEqual(q,original)

    def test_no_progress_exit_not_profit_cap_or_global_exit(self):
        p=NS(kind='fast_momentum',entry_ts=NOW-timedelta(minutes=5),entry_price=100,peak_price=104)
        self.assertEqual(fm.timeout_reason(p,NOW),'fast_momentum_no_progress_5min')
        p.peak_price=106;self.assertIsNone(fm.timeout_reason(p,NOW))
        p.kind='gamma_blast';p.peak_price=100;self.assertIsNone(fm.timeout_reason(p,NOW))


class FastDispatch(unittest.TestCase):
    setUp=cf.Celestial.setUp
    tearDown=cf.Celestial.tearDown
    entry_fixture=cf.Celestial.entry_fixture
    arm=cf.Celestial.arm

    def args(self):
        args=self.entry_fixture()
        self.row=dict(strategy='fast_momentum',eligible=True,ts=f.NOW.isoformat(),underlying=args['index_name'],
            direction='CE',token='T',symbol=args['chain'].quotes['T'].symbol,exchange='NFO',spot_stop=9900,
            days=2,episode='one',entry=100)
        args.update(kind='fast_momentum',tier='EXPERIMENTAL',score=0,fast_context=lambda:self.row,candidate_id=None)
        patch.object(f.executor.fast_momentum,'live_blocks',return_value=[]).start()
        return args

    def test_fast_entry_uses_same_ledger_no_assumed_fill_no_duplicate(self):
        args=self.args();r=self.ex.evaluate_and_enter(**args)
        self.assertTrue(r['ok'],r);self.place.assert_called_once()
        self.assertEqual(self.ex.state.positions['40'].remaining_qty,0)
        self.assertEqual(self.ex.state.positions['40'].kind,'fast_momentum')
        self.ex.evaluate_and_enter(**args);self.place.assert_called_once()

    def test_observation_reconciliation_and_dry_run_block_orders(self):
        args=self.args()
        with patch.object(f.executor.fast_momentum,'live_blocks',return_value=['fast_observation_only']):
            self.assertFalse(self.ex.evaluate_and_enter(**args)['ok'])
        self.ex._reconcile_blocked=True
        self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'],'startup_reconcile_not_complete')
        self.ex._reconcile_blocked=False
        with patch.object(f.executor,'ORDER_PLACEMENT_ENABLED',False):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'],'broker_orders_disabled')
        self.place.assert_not_called()

    def test_setup_and_price_rechecked_before_order(self):
        args=self.args()
        with patch.object(f.executor,'_poll_ltp',return_value={'T':104}):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'],'fast_entry_price_ran_away')
        def quote(*a):
            self.row['eligible']=False
            return {'T':100}
        with patch.object(f.executor,'_poll_ltp',side_effect=quote):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'],'fast_invalidated_before_submission')
        self.place.assert_not_called()

    def test_episode_identity_persisted_once(self):
        row=dict(episode='nifty:ce:time',ts=f.NOW.isoformat(),underlying='NIFTY',direction='CE')
        a=f.executor.fast_momentum.signal_id(row)
        self.assertEqual(a,f.executor.fast_momentum.signal_id(row))

    def test_stop_time_out_uses_owned_quantity(self):
        args=self.args();self.ex.evaluate_and_enter(**args)
        pos=self.ex.state.positions['40'];pos.kind='fast_momentum'
        pos.entry_ts=f.NOW-timedelta(minutes=6);pos.entry_price=100;pos.remaining_qty=10
        pos.status='open';pos.peak_price=100;pos.structural_stop=90
        f.ledger.connection().execute("UPDATE execution_orders SET state='terminal'")
        self.ex._celestial_verified.add('40')
        self.ex.check_exits()
        self.assertEqual(pos.exit_reason,'fast_momentum_no_progress_5min')
        self.assertEqual(self.place.call_args.kwargs['transactiontype'],'SELL')


class EngineIsolation(unittest.TestCase):
    def test_observation_never_dispatches_and_scan_failure_is_isolated(self):
        from test_shield_confirmation import load_engine
        module=load_engine();engine=module.Engine.__new__(module.Engine)
        engine.fast_lane=NS(scan=Mock(return_value=dict(eligible=False,stage='SETUP_READY',
            blocked_reasons=['fast_observation_only'],underlying='NIFTY')))
        engine._fast_reported={}
        ex=NS(fast_rows={},evaluate_and_enter=Mock())
        with patch.object(module.auto_executor,'EXECUTOR',ex),patch.object(module.audit,'log'):
            engine._scan_fast(NS(name='NIFTY'))
            ex.evaluate_and_enter.assert_not_called()
            engine.fast_lane.scan.side_effect=RuntimeError('test failure')
            engine._scan_fast(NS(name='NIFTY'))
            self.assertEqual(ex.fast_rows['NIFTY']['blocked_reasons'],['fast_lane_error'])
            ex.evaluate_and_enter.assert_not_called()
