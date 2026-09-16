"""Offline control, radar, adoption and execution-boundary regression tests."""
import importlib
import json
import os
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
import test_execution_lifecycle as f

c = importlib.import_module(f.PKG+'.celestial')
news = importlib.import_module(f.PKG+'.news_catalysts')
radar = importlib.import_module(f.PKG+'.celestial_scanner')
universe = importlib.import_module(f.PKG+'.celestial_universe')
adoption = importlib.import_module(f.PKG+'.celestial_adoption')


def candidate(i=1, **kw):
    return dict(id='NFO:'+str(i),exchange='NFO',token=str(i),symbol='TESTCE',underlying='NIFTY',
        direction='CE',best_strike=100,option_ltp=110,premium_move_pct=10,gamma_score=80,
        priority=2,last_updated=f.NOW.isoformat(),spot_at=f.NOW.isoformat(),volume_spike=True,
        oi_support=True,spread_status='CLEAN',tradeable=True,direction_clear=True,**kw)


class Celestial(unittest.TestCase):
    def setUp(self):
        f.Lifecycle.setUp(self)
        patch.dict(os.environ,{'KADIMA_CELESTIAL_ENABLED':'1'}).start()
        c.health('news',dict(ok=True,covered_symbols=['NIFTY']))

    tearDown=f.Lifecycle.tearDown

    def arm(self):
        c.update_config({'auto_trade_enabled':True})

    def test_defaults_are_paused_and_65(self):
        self.assertEqual(c.read_config(),c.DEFAULTS)

    def test_thresholds_persist_and_revision_checks(self):
        for threshold in c.THRESHOLDS:
            current=c.read_config()
            new=c.update_config({'score_threshold':threshold},expected_revision=current['revision'])
            self.assertEqual(c.read_config()['score_threshold'],threshold)
            with self.assertRaises(ValueError):
                c.update_config({'score_threshold':65},expected_revision=current['revision'])
            self.assertEqual(new['revision'],current['revision']+1)

    def test_invalid_config_never_coerces_strings_or_bools(self):
        for payload in ({'score_threshold':True},{'score_threshold':66},{'score_threshold':65.0},
                        {'score_threshold':'65'},{'auto_trade_enabled':'false'},
                        {'kill_latched':False},{'dashboard_limit':200},{'unrelated':1}):
            with self.subTest(payload=payload),self.assertRaises(ValueError):c.update_config(payload)
        with self.assertRaises(ValueError):c.update_config({},expected_revision=True)

    def test_corrupt_config_fails_closed(self):
        db=c.connection()
        with db:db.execute('INSERT INTO celestial_config VALUES (1,?)',(json.dumps(c.DEFAULTS|{'auto_trade_enabled':'true'}),))
        with self.assertRaises(ValueError):c.read_config()
        with self.assertRaises(ValueError):c.update_config({'score_threshold':70})

    def test_kill_latch_survives_read_and_blocks_resume(self):
        self.arm();c.latch_kill()
        self.assertTrue(c.read_config()['kill_latched'])
        with self.assertRaises(ValueError):self.arm()
        c.update_config({'auto_trade_enabled':False},reset_kill=True)
        self.assertFalse(c.read_config()['auto_trade_enabled'])

    def test_rollout_flag_is_separate(self):
        with patch.dict(os.environ,{'KADIMA_CELESTIAL_ENABLED':'0'}):
            with self.assertRaises(ValueError):self.arm()

    def test_top20_and_manual_share_ranking_but_pause_only_blocks_auto(self):
        c.publish('test',[candidate()])
        self.assertFalse(c.top20()[0]['eligible'])
        self.assertTrue(c.top20(manual=True)[0]['eligible'])
        self.arm();self.assertTrue(c.top20()[0]['eligible'])
        c.latch_kill();self.assertFalse(c.top20(manual=True)[0]['eligible'])

    def test_stale_future_and_underfive_are_not_displayed(self):
        for stamp,move in [(f.NOW-timedelta(seconds=31),10),(f.NOW+timedelta(seconds=1),10),(f.NOW,4.99)]:
            row=candidate();row.update(last_updated=stamp.isoformat(),premium_move_pct=move)
            c.publish('test',[row]);self.assertEqual(c.top20(),[])

    def test_all_candidate_gates_fail_closed(self):
        self.arm()
        for key,value in [('volume_spike',False),('oi_support',False),('spread_status','UNKNOWN'),
                          ('tradeable',False),('direction_clear',False),('spot_at',None),('gamma_score',64.9)]:
            row=candidate();row[key]=value;c.publish('test',[row])
            self.assertFalse(c.top20()[0]['eligible'],key)

    def test_top20_is_bounded_expiry_first_and_deduplicated(self):
        rows=[candidate(i)|{'underlying':'STOCK'+str(i),'priority':3} for i in range(1,31)]
        rows+=[candidate(50)|{'gamma_score':70,'priority':1}]
        rows+=[candidate(51)|{'gamma_score':69,'priority':1}]
        c.publish('test',rows)
        result=c.top20()
        self.assertEqual(len(result),20)
        self.assertEqual(result[0]['id'],'NFO:50')
        self.assertNotIn('NFO:51',[r['id'] for r in result])
        self.assertEqual([r['rank'] for r in result],list(range(1,21)))

    def test_missing_news_is_not_neutral_coverage(self):
        self.arm();c.publish('test',[candidate()])
        c.health('news',dict(ok=True,covered_symbols=['OTHER']))
        self.assertIn('news_coverage_unavailable',c.top20()[0]['blocked_reasons'])

    def event(self, **kw):
        return dict(id='event-1',symbols=['NIFTY'],score=20,block=False,url='https://example.test/announcement',
                    published_at=(f.NOW-timedelta(minutes=5)).isoformat(),
                    expires_at=(f.NOW+timedelta(hours=1)).isoformat(),**kw)

    def ingest(self,events):
        return news.ingest(events,source='fixture',observed_at=f.NOW.isoformat(),covered_symbols=['NIFTY'])

    def test_news_direction_block_and_dedup(self):
        self.ingest([self.event()]);self.ingest([self.event()])
        self.assertEqual(c.news_impact('NIFTY','CE')['news_score'],20)
        self.assertEqual(c.news_impact('NIFTY','PE')['news_score'],-20)
        event=self.event();event['block']=True;self.ingest([event])
        self.arm();c.publish('test',[candidate()])
        self.assertIn('news_risk_block',c.top20()[0]['blocked_reasons'])
        self.assertEqual(c.top20()[0]['final_score'],100)

    def test_news_batch_validation_is_atomic(self):
        event=self.event();event['score']=100
        with self.assertRaises(ValueError):self.ingest([self.event(),event])
        self.assertEqual(c.connection().execute('SELECT count(*) FROM celestial_news').fetchone()[0],0)

    def test_news_empty_checked_batch_vs_missing_and_expired(self):
        self.ingest([]);self.assertTrue(c.news_impact('NIFTY','CE')['news_available'])
        self.assertFalse(c.news_impact('NIFTY','CE',f.NOW+timedelta(minutes=6))['news_available'])
        event=self.event();event['published_at']=(f.NOW+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):self.ingest([event])

    def test_durable_signal_id_prevents_restart_duplicate(self):
        row=candidate()|{'tier':'ACTIVE','final_score':80}
        self.assertEqual(c.signal_for(row),c.signal_for(row))

    def test_trail_at_five_tightens_and_has_no_cap(self):
        stop,active=c.trail_stop(100,105,95,95)
        self.assertTrue(active);self.assertAlmostEqual(stop,102.5)
        stop2,_=c.trail_stop(100,200,stop,95)
        stop3,_=c.trail_stop(100,1000,stop2,95)
        self.assertGreater(stop3,stop2)
        self.assertEqual(c.trail_stop(100,105,120,95)[0],120)

    def test_trail_setting_only_tightens(self):
        c.set_retain(1,.8);c.set_retain(1,.6)
        self.assertEqual(c.retain_fraction(1),.8)
        for bad in (.1,1,float('nan'),True):
            with self.assertRaises(ValueError):c.set_retain(1,bad)

    def test_five_minute_volume_window_and_session_reset(self):
        w=radar.PremiumWindow();result=None
        for seconds in range(0,301,30):
            volume=1000+seconds*2 if seconds<=240 else 1480+(seconds-240)*8
            result=w.add(f.NOW+timedelta(seconds=seconds),100+seconds/30,volume,1000+seconds)
        self.assertAlmostEqual(result['premium_move_pct'],10)
        self.assertTrue(result['volume_spike'])
        self.assertAlmostEqual(result['volume_ratio'],4)
        self.assertIsNone(w.add(f.NOW+timedelta(days=1),110,10,1000))

    def test_missing_windows_never_create_premium_blast(self):
        w=radar.PremiumWindow()
        self.assertIsNone(w.add(f.NOW,100,100,100))
        self.assertIsNone(w.add(f.NOW+timedelta(minutes=10),200,10000,100))
        self.assertEqual(len(w.samples),1)
        self.assertIsNone(w.add(f.NOW+timedelta(minutes=10,seconds=30),float('nan'),10001,100))
        self.assertEqual(len(w.samples),0)

    def contract(self):
        return dict(exch_seg='NFO',token='X',symbol='TESTCE',name='STOCK',instrumenttype='OPTSTK',
                    expiry=f.NOW.strftime('%d%b%Y'),lotsize='10',strike='10000')

    def broker(self, **kw):
        return dict(exchange='NFO',symboltoken='X',tradingsymbol='TESTCE',producttype='INTRADAY',
                    netqty='10',buyavgprice='100',**kw)

    def setup_adoption(self):
        self.ex.state.positions.clear()
        self.ex.celestial_master=SimpleNamespace(rows=[self.contract()])
        self.ex._restored=True
        return patch.object(f.executor,'fetch_broker_positions',return_value=[self.broker()])

    def test_adoption_idempotent_and_initial_stop_is_persisted(self):
        with self.setup_adoption():
            self.ex.reconcile_on_startup();self.ex.reconcile_on_startup()
        self.assertFalse(self.ex._reconcile_blocked)
        self.assertEqual(len(self.ex.state.positions),1)
        pos=next(iter(self.ex.state.positions.values()))
        self.assertEqual(pos.kind,'MANUAL_ADOPTED');self.assertEqual(pos.effective_stop,95)
        self.assertEqual(pos.remaining_qty,10)
        self.place.assert_not_called()

    def test_unsupported_short_remains_visible_and_blocks_entries(self):
        self.setup_adoption()
        row=self.broker();row['netqty']='-10'
        with patch.object(f.executor,'fetch_broker_positions',return_value=[row]):self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked)
        self.assertFalse(c.health('reconciliation')['positions'][0]['managed'])
        self.assertEqual(self.ex.state.positions,{})

    def test_external_quantity_change_is_not_an_assumed_fill(self):
        with self.setup_adoption():self.ex.reconcile_on_startup()
        row=self.broker();row['netqty']='5'
        with patch.object(f.executor,'fetch_broker_positions',return_value=[row]):self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked);self.assertFalse(self.ex._positions_verified)
        self.assertEqual(next(iter(self.ex.state.positions.values())).remaining_qty,10)
        self.place.assert_not_called()

    def test_unreadable_book_removes_stale_verification(self):
        with self.setup_adoption():self.ex.reconcile_on_startup()
        with patch.object(f.executor,'fetch_broker_positions',return_value=None):self.ex.reconcile_on_startup()
        self.assertFalse(self.ex._positions_verified);self.assertEqual(self.ex._celestial_verified,set())
        self.assertFalse(c.health('reconciliation')['ok'])

    def external_fill(self, qty, side, price=110, oid='EXT1'):
        return f.orders.OrderStatus(oid,'complete',qty,price,dict(exchange='NFO',symboltoken='X',
            producttype='INTRADAY',transactiontype=side,tradingsymbol='TESTCE'))

    def test_verified_manual_exit_is_accounted_once_without_reselling(self):
        with self.setup_adoption():self.ex.reconcile_on_startup()
        pos=next(iter(self.ex.state.positions.values()))
        self.book.return_value=[self.external_fill(10,'SELL')]
        with patch.object(f.executor,'fetch_broker_positions',return_value=[]):
            self.ex.reconcile_on_startup();self.ex.reconcile_on_startup()
        self.assertEqual(pos.remaining_qty,0)
        self.assertEqual(pos.status,'closed')
        module=importlib.import_module(f.PKG+'.external_reconciliation')
        self.assertEqual(module.realized_today(),100)
        self.assertFalse(self.ex._reconcile_blocked)
        self.place.assert_not_called()

    def test_verified_manual_scale_in_updates_basis_and_tightens_protection(self):
        with self.setup_adoption():self.ex.reconcile_on_startup()
        pos=next(iter(self.ex.state.positions.values()))
        self.book.return_value=[self.external_fill(10,'BUY',120)]
        row=self.broker();row.update(netqty='20',buyavgprice='110')
        with patch.object(f.executor,'fetch_broker_positions',return_value=[row]):self.ex.reconcile_on_startup()
        self.assertEqual((pos.remaining_qty,pos.entry_price),(20,110))
        self.assertGreaterEqual(pos.effective_stop,104.5)
        self.assertFalse(self.ex._reconcile_blocked)
        self.place.assert_not_called()

    def test_mixed_manual_fills_with_same_net_do_not_silently_reconcile(self):
        with self.setup_adoption():self.ex.reconcile_on_startup()
        self.book.return_value=[self.external_fill(10,'SELL'),self.external_fill(10,'BUY',120,'EXT2')]
        with patch.object(f.executor,'fetch_broker_positions',return_value=[self.broker()]):self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked)
        self.assertFalse(self.ex._positions_verified)
        self.place.assert_not_called()

    def test_revised_external_fill_book_is_not_applied_twice(self):
        with self.setup_adoption():self.ex.reconcile_on_startup()
        self.book.return_value=[self.external_fill(10,'BUY',120)]
        row=self.broker();row.update(netqty='20',buyavgprice='110')
        with patch.object(f.executor,'fetch_broker_positions',return_value=[row]):
            self.ex.reconcile_on_startup()
            self.book.return_value=[self.external_fill(10,'BUY',130)]
            self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked)
        self.assertEqual(next(iter(self.ex.state.positions.values())).entry_price,110)

    def test_external_pending_order_prevents_adoption(self):
        self.book.return_value=[f.orders.OrderStatus('external','open',0,None,{})]
        with self.setup_adoption():self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked);self.assertEqual(self.ex.state.positions,{})

    def test_pause_does_not_disable_exit_monitor(self):
        self.ex._celestial_verified={'1'}
        self.pos.structural_stop=95;self.pos.effective_stop=98
        with patch.object(f.executor,'_poll_ltp',return_value={'T':97}):self.ex.check_exits()
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.kwargs['transactiontype'],'SELL')

    def test_quotes_are_keyed_by_exchange_and_token(self):
        other=f.executor.Position(2,'MANUAL_ADOPTED','SENSEX','CE','OTHERCE','T','BFO',100,f.NOW,1,10,10,
            peak_price=100,structural_stop=95,effective_stop=95)
        self.ex.state.positions['2']=other
        self.pos.structural_stop=95;self.pos.effective_stop=95
        self.ex._celestial_verified={'1','2'}
        with patch.object(f.executor,'_poll_ltp',side_effect=lambda exchange,tokens:{'T':90 if exchange=='NFO' else 110}):
            self.ex.check_exits()
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.kwargs['exchange'],'NFO')
        self.assertEqual(other.status,'open')

    def test_selected_candidate_is_required_for_new_mode(self):
        result=self.ex.evaluate_and_enter('NIFTY','CE',SimpleNamespace(),100,None,1,'gamma_blast',2,'CONFIRMED',90)
        self.assertEqual(result['reason'],'legacy_entry_disabled_in_celestial_mode')
        self.place.assert_not_called()

    def test_manual_buy_does_not_accept_arbitrary_token(self):
        self.assertFalse(self.ex.buy_candidate('NFO:INVENTED')['ok'])
        self.place.assert_not_called()

    def entry_fixture(self, score=65):
        self.ex.state.positions.clear()
        self.pos.status='closed';f.storage.upsert_position(self.pos)
        self.arm()
        row=candidate('T');row['gamma_score']=score;c.publish('entry-fixture',[row])
        q=SimpleNamespace(symbol='TESTCE',token='T',strike=10000,opt_type='CE',ltp=100,
            bid=99.5,ask=100.5,delta=.5,iv=80,volume=1000,oi=2000,spread_pct=1)
        other=SimpleNamespace(**(q.__dict__|{'token':'U','symbol':'OTHERCE'}))
        f.config.STRIKES_EACH_SIDE=5
        chain=SimpleNamespace(quotes={'T':q,'U':other},index=SimpleNamespace(opt_exchange='NFO'),
            atm=10000,_last_poll=time.monotonic(),_spot_received_at=time.monotonic(),
            _alignment_at=time.monotonic(),scrips=SimpleNamespace(strikes_around=lambda *a:[{'token':'T'},{'token':'U'}]))
        snap=SimpleNamespace(vwap=9990,ema_fast=10000,ema_slow=9990,rsi=60)
        patch.object(f.pkg.strike_selector,'rank',lambda *a:[SimpleNamespace(rejected=False,quote=x) for x in (other,q)],create=True).start()
        patch.object(f.pkg.risk_budget,'select_within_budget',lambda rows,*a,**kw:SimpleNamespace(candidate=rows[0],lots=1),create=True).start()
        patch.object(self.ex,'_structure',return_value=(9980,1)).start()
        patch.object(f.executor.trade_policy,'premium_stop',return_value=90).start()
        return dict(index_name='NIFTY',direction='CE',chain=chain,spot=10000,snap=snap,
            days_to_expiry=1,kind='gamma_blast',source_signal_id=40,tier='CONFIRMED',score=score,candidate_id='NFO:T')

    def test_65_enters_exact_top20_strike_not_better_legacy_rank(self):
        args=self.entry_fixture()
        result=self.ex.evaluate_and_enter(**args)
        self.assertTrue(result['ok'],result)
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.kwargs['symboltoken'],'T')
        self.assertEqual(self.place.call_args.kwargs['transactiontype'],'BUY')

    def test_threshold_changed_before_send_prevents_order(self):
        args=self.entry_fixture()
        def fresh_quote(*a):
            c.update_config({'score_threshold':90})
            return {'T':100}
        with patch.object(f.executor,'_poll_ltp',side_effect=fresh_quote):
            result=self.ex.evaluate_and_enter(**args)
        self.assertEqual(result['reason'],'celestial_candidate_changed_before_submission')
        self.place.assert_not_called()

    def test_pause_before_send_prevents_auto_order(self):
        args=self.entry_fixture()
        def fresh_quote(*a):
            c.update_config({'auto_trade_enabled':False})
            return {'T':100}
        with patch.object(f.executor,'_poll_ltp',side_effect=fresh_quote):
            result=self.ex.evaluate_and_enter(**args)
        self.assertEqual(result['reason'],'celestial_candidate_changed_before_submission')
        self.place.assert_not_called()

    def test_selected_strike_removed_has_no_fallback_order(self):
        args=self.entry_fixture()
        def fresh_quote(*a):
            c.publish('entry-fixture',[])
            return {'T':100}
        with patch.object(f.executor,'_poll_ltp',side_effect=fresh_quote):
            result=self.ex.evaluate_and_enter(**args)
        self.assertEqual(result['reason'],'celestial_candidate_changed_before_submission')
        self.place.assert_not_called()

    def test_duplicate_candidate_does_not_send_twice(self):
        args=self.entry_fixture()
        self.ex.evaluate_and_enter(**args);self.ex.evaluate_and_enter(**args)
        self.place.assert_called_once()

    def test_master_expires_from_dates_not_weekday(self):
        today=self.contract();old=self.contract()|{'token':'Y','expiry':(f.NOW-timedelta(days=1)).strftime('%d%b%Y')}
        self.assertEqual(set(universe.active_contracts([today,old],f.NOW.date())),{('NFO','X')})


if __name__=='__main__':unittest.main()
