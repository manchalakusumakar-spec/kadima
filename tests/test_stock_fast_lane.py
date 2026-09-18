import ast
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import kadima
from kadima import stock_fast as sf

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

@dataclass
class Quote:
    token: str
    symbol: str
    strike: float
    opt_type: str
    ltp: float = 10.5
    oi: int = 10000
    oi_change: int = 0
    volume: int = 10000
    iv: float = 25
    delta: float = .5
    gamma: float = .01
    theta: float = -.01
    vega: float = .1
    bid: float = 10.48
    ask: float = 10.52

@dataclass
class Index:
    name: str = 'UNOMINDA'
    exchange: str = 'NSE'
    token: str = 'SPOT'
    opt_exchange: str = 'NFO'
    opt_symbol_root: str = 'UNOMINDA'
    strike_step: int = 20
    weight: float = 0


def make_chain():
    quotes = {str(i): Quote(str(i), f'UNOMINDA29SEP26{1240 + i*20}CE', 1240+i*20, 'CE') for i in range(6)}
    return types.SimpleNamespace(index=Index(), quotes=quotes, _expiry='29Sep2026', _last_poll=1.,
                                 velocity=types.SimpleNamespace(add=Mock(), mark_degraded=Mock()))

class Base(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.modules = {}
        def install(name, **values):
            module = types.ModuleType('kadima.'+name)
            module.__dict__.update(values)
            key = 'kadima.'+name
            previous = sys.modules.get(key)
            sys.modules[key] = module
            def restore():
                if previous is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = previous
            self.stack.callback(restore)
            self.stack.enter_context(patch.object(kadima, name, module, create=True))
            self.modules[name] = module
            return module
        self.install = install
        self.install('storage', conn=lambda: self.db, insert_oi_snapshot=Mock())
        self.install('audit', log=Mock())
        self.celestial = self.install('celestial', enabled=lambda: True,
            read_config=lambda: {'kill_latched': False, 'auto_trade_enabled': True}, health=Mock(return_value={}))
        self.fast = self.install('fast_momentum', live_requested=lambda: True,
            live_blocks=lambda *a: [], signal_id=lambda row: 7)
        self.ledger = self.install('execution_ledger', seen=lambda sid: False)
        self.install('option_chain', OptionQuote=Quote)
        self.install('config', MARKET_CLOSE=(15, 40), ORDER_PLACEMENT_ENABLED=True)
        self.stack.enter_context(patch.object(sf, '_now', return_value=NOW))

    def data(self, chain=None):
        chain = chain or make_chain()
        recorder = sf.Recorder()
        recorder.publish(chain, 1284, NOW.isoformat(), 1270)
        return sf.observations()[0]

class WindowTests(unittest.TestCase):
    def filled(self):
        window = sf.PremiumWindow()
        metric = None
        for i in range(7):
            price = 10 if i < 5 else 10+(i-4)*.3
            volume = 1000+100*min(i,4)+300*max(0,i-4)
            metric = window.add(NOW+timedelta(seconds=30*i), price, price-.02, price+.02, volume)
        return window, metric
    def test_acceleration_uses_incremental_volume(self):
        _, metric = self.filled()
        self.assertAlmostEqual(metric['volume_ratio'], 3)
        self.assertAlmostEqual(metric['premium_move_pct'], 6)
        self.assertTrue(sf.accelerated(metric))
    def test_duplicate_does_not_count_as_another_sample(self):
        window, _ = self.filled(); n = len(window.samples)
        self.assertIsNone(window.add(NOW+timedelta(seconds=180), 11, 10.99, 11.01, 2200))
        self.assertEqual(n, len(window.samples))
    def test_volume_reset_clears_history(self):
        window, _ = self.filled()
        self.assertIsNone(window.add(NOW+timedelta(seconds=210), 11, 10.99, 11.01, 1))
        self.assertEqual(1, len(window.samples))
    def test_long_gap_clears_history(self):
        window, _ = self.filled()
        self.assertIsNone(window.add(NOW+timedelta(seconds=280), 11, 10.99, 11.01, 2500))
        self.assertEqual(1, len(window.samples))
    def test_new_day_clears_history(self):
        window, _ = self.filled()
        self.assertIsNone(window.add(NOW+timedelta(days=1), 11, 10.99, 11.01, 2500))
    def test_invalid_and_crossed_books_clear(self):
        for values in [(10, 11, 10, 100), (float('nan'), 9, 11, 100), (True, 9, 11, 100)]:
            window, _ = self.filled()
            self.assertIsNone(window.add(NOW+timedelta(seconds=210), *values))
            self.assertFalse(window.samples)
    def test_premium_only_move_without_bid_confirmation_rejected(self):
        _, metric = self.filled(); metric['bid_move_pct'] = 0
        self.assertFalse(sf.accelerated(metric))
    def test_out_of_order_sample_cannot_keep_old_window(self):
        window, _ = self.filled()
        self.assertIsNone(window.add(NOW, 10, 9.99, 10.01, 1000))
        self.assertEqual(1, len(window.samples))

class ControlAndPersistenceTests(Base):
    def test_defaults_do_not_arm_live(self):
        self.assertEqual(sf.controls(), sf.DEFAULTS)
        self.assertFalse(sf.controls()['live_enabled'])
    def test_request_and_pause_are_explicit_revisioned_writes(self):
        self.assertTrue(sf.update_controls({'live_enabled': True}, 0)['live_enabled'])
        with self.assertRaises(ValueError): sf.update_controls({'holding_minutes': 5}, 0)
        self.assertFalse(sf.update_controls({'live_enabled': False}, 1)['live_enabled'])
    def test_controls_do_not_coerce_strings_bools_or_floats(self):
        for patch_ in [{'live_enabled':'true'}, {'holding_minutes':True}, {'holding_minutes':5.0}, {'holding_minutes':30}, {'extra':1}]:
            with self.assertRaises(ValueError): sf.update_controls(patch_, 0)
            self.assertEqual(sf.controls()['revision'], 0)
    def test_global_read_occurs_before_transaction(self):
        def global_read():
            self.assertFalse(self.db.in_transaction)
            return {'kill_latched':False}
        self.celestial.read_config = global_read
        sf.update_controls({'live_enabled':True}, 0)
    def test_rollout_and_kill_are_not_bypassed(self):
        self.fast.live_requested = lambda: False
        with self.assertRaises(ValueError): sf.update_controls({'live_enabled':True}, 0)
        self.fast.live_requested = lambda: True
        self.celestial.read_config = lambda: {'kill_latched':True}
        with self.assertRaises(ValueError): sf.update_controls({'live_enabled':True}, 0)
    def test_snapshot_independent_of_gamma(self):
        data = self.data()
        self.assertNotIn('gamma_score', data)
        self.assertTrue(sf.still_current(data))
    def test_stale_and_replaced_snapshots_not_reused(self):
        data = self.data()
        with patch.object(sf, '_now', return_value=NOW+timedelta(seconds=31)):
            self.assertFalse(sf.still_current(data)); self.assertEqual(sf.observations(), [])
    def test_invalidation_removes_candidate_but_retains_genuine_common_history(self):
        recorder = sf.Recorder(); chain = make_chain()
        recorder.publish(chain,1284,NOW.isoformat(),1270)
        recorder.invalidate('UNOMINDA')
        self.assertEqual(sf.observations(), [])
        self.assertIn('UNOMINDA', recorder.windows)
    def test_future_timestamp_not_fresh(self):
        self.assertFalse(sf._fresh((NOW+timedelta(seconds=1)).isoformat(), NOW))
    def test_policy_survives_pause_and_horizon_changes(self):
        row = dict(episode='stock:E', holding_minutes=5, session_exit=sf.session_exit(NOW).isoformat())
        self.assertEqual(sf.bind_policy(row), 7)
        self.ledger.seen = lambda sid: True
        sf.bind_policy(dict(row, holding_minutes=15))
        pos = types.SimpleNamespace(signal_id=7,entry_ts=NOW)
        self.assertEqual(sf.deadline_reason(pos,NOW+timedelta(minutes=5)), 'stock_fast_holding_deadline')
    def test_unsubmitted_policy_can_follow_new_operator_horizon(self):
        row = dict(episode='stock:E', holding_minutes=15, session_exit=sf.session_exit(NOW).isoformat())
        sf.bind_policy(row); sf.bind_policy(dict(row, holding_minutes=5))
        pos = types.SimpleNamespace(signal_id=7,entry_ts=NOW)
        self.assertEqual(sf.deadline_reason(pos,NOW+timedelta(minutes=5)), 'stock_fast_holding_deadline')
    def test_other_positions_have_no_stock_deadline(self):
        self.assertIsNone(sf.deadline_reason(types.SimpleNamespace(signal_id=99,entry_ts=NOW)))
    def test_no_entry_past_selected_horizon(self):
        self.assertFalse(sf.entry_time_allowed(NOW.replace(hour=15,minute=0),15))
        self.assertTrue(sf.entry_time_allowed(NOW.replace(hour=15,minute=0),5))
        self.assertFalse(sf.entry_time_allowed(NOW.replace(hour=9,minute=0),5))
        self.assertFalse(sf.entry_time_allowed(NOW+timedelta(days=1),5))

class PlanFixture(Base):
    def setUp(self):
        super().setUp()
        self.chain=make_chain(); self.payload=self.data(self.chain)
        metric=dict(premium_move_pct=4,bid_move_pct=4,ask_move_pct=4,volume_ratio=2,
                    observed_at=NOW.isoformat(),baseline_at=(NOW-timedelta(seconds=60)).isoformat())
        self.payload['fast_metrics']={str(i):metric.copy() for i in range(6)}
        self.bars={1:[dict(volume=100) for _ in range(5)]+[dict(volume=250),dict(volume=120)],5:[]}
        self.fast.setup=lambda *args: dict(direction='CE',spot_stop=1280,level=1282,
            breakout_at=(NOW-timedelta(minutes=2)).isoformat(),bars=self.bars[1])
        self.fast.scenarios=lambda *args:[dict(minutes=m,central=13,low=11,high=15) for m in (5,10,15)]
        self.install('strike_selector',rank=lambda *args:[types.SimpleNamespace(quote=q,rejected=None) for q in self.chain.quotes.values()])
        self.install('trade_policy',premium_stop=lambda *args:10,risk_per_unit=lambda entry,stop:entry-stop,
                     EXIT_BUFFER=.01,COST_RESERVE=.01)
        self.cfg=dict(sf.DEFAULTS,live_enabled=True)
class PlanTests(PlanFixture):
    def test_shared_executor_strategy_identity_and_stock_scope(self):
        row=sf.plan(self.payload,self.chain,self.bars,self.cfg)
        self.assertTrue(row['eligible'],row)
        self.assertEqual(row['strategy'],'fast_momentum')
        self.assertEqual(row['asset_scope'],'STOCK')
        self.assertTrue(row['episode'].startswith('stock:NFO:UNOMINDA:'))
    def test_stock_off_does_not_become_live_due_to_master_switch(self):
        row=sf.plan(self.payload,self.chain,self.bars,sf.DEFAULTS)
        self.assertFalse(row['eligible']); self.assertIn('stock_fast_live_disabled',row['blocked_reasons'])
    def test_existing_shared_blocker_is_preserved(self):
        self.fast.live_blocks=lambda *args:['fresh_news_coverage_required']
        row=sf.plan(self.payload,self.chain,self.bars,self.cfg)
        self.assertIn('fresh_news_coverage_required',row['blocked_reasons'])
    def test_no_bid_confirmation_no_entry(self):
        for metric in self.payload['fast_metrics'].values(): metric['bid_move_pct']=0
        self.assertFalse(sf.plan(self.payload,self.chain,self.bars,self.cfg)['eligible'])
    def test_underlying_volume_confirmation_required(self):
        self.bars[1][-2]['volume']=100
        row=sf.plan(self.payload,self.chain,self.bars,self.cfg)
        self.assertIn('stock_fast_underlying_volume_unconfirmed',row['blocked_reasons'])
    def test_stale_quote_rejected_even_with_new_decision_timestamp(self):
        self.payload['chain_at']=(NOW-timedelta(seconds=31)).isoformat()
        row=sf.plan(self.payload,self.chain,self.bars,self.cfg)
        self.assertIn('stock_fast_stale_snapshot',row['blocked_reasons'])
    def test_selected_five_minute_horizon_not_fifteen_is_tested(self):
        self.fast.scenarios=lambda *a:[dict(minutes=5,central=10.5),dict(minutes=15,central=20)]
        row=sf.plan(self.payload,self.chain,self.bars,dict(self.cfg,holding_minutes=5))
        self.assertIn('scenario_net_reward_risk_below_1_5',row['blocked_reasons'])
    def test_invalid_history_does_not_turn_into_a_pass(self):
        def missing(*args): raise ValueError('candle_gap')
        self.fast.setup=missing
        self.assertIn('candle_gap',sf.plan(self.payload,self.chain,self.bars,self.cfg)['blocked_reasons'])
    def test_expired_contract_rejected(self):
        self.chain._expiry='17Sep2026'
        self.assertIn('expired_contract',sf.plan(self.payload,self.chain,self.bars,self.cfg)['blocked_reasons'])

    def test_missing_greeks_are_not_a_pass(self):
        for q in self.chain.quotes.values(): q.delta = None
        self.assertFalse(sf.plan(self.payload,self.chain,self.bars,self.cfg)['eligible'])
    def test_nan_scenario_rejected(self):
        self.fast.scenarios=lambda *a:[dict(minutes=15,central=float('nan'))]
        self.assertFalse(sf.plan(self.payload,self.chain,self.bars,self.cfg)['eligible'])

class WorkerTests(PlanFixture):
    def setUp(self):
        super().setUp()
        self.executor=types.SimpleNamespace(fast_rows={}, _lock=threading.RLock(),
            state=types.SimpleNamespace(positions={}), manual_exit=Mock(), evaluate_and_enter=Mock())
        self.install('stock_execution',StockExecutionWorker=lambda *a:
            types.SimpleNamespace(restore_chain=lambda d: self.chain))
        self.install('indicators',Snapshot=lambda:types.SimpleNamespace(vwap=None))
        self.worker=sf.Worker(self.executor,None)
        self.worker.history=lambda i:self.bars
        sf.update_controls({'live_enabled':True},0)
    def test_live_candidate_reaches_shared_executor_with_revalidation(self):
        def execute(*args,**kwargs):
            self.assertEqual(args[6],'fast_momentum')
            self.assertEqual(kwargs['fast_context']()['strategy'],'fast_momentum')
            self.assertTrue(self.chain._entry_guard())
            return dict(ok=True,reason='acknowledged_not_filled')
        self.executor.evaluate_and_enter.side_effect=execute
        row=self.worker.process(self.payload)
        self.executor.evaluate_and_enter.assert_called_once()
        self.assertEqual(row['dispatch']['reason'],'acknowledged_not_filled')
        self.assertEqual(self.executor.state.positions,{})
        self.assertIsNotNone(sf._connection().execute('SELECT * FROM stock_fast_policies').fetchone())
    def test_stock_pause_prevents_dispatch(self):
        sf.update_controls({'live_enabled':False},1)
        row=self.worker.process(self.payload)
        self.assertFalse(row['eligible'])
        self.executor.evaluate_and_enter.assert_not_called()
    def test_no_new_dispatch_after_worker_stop(self):
        self.worker.stop_event.set()
        self.assertFalse(self.worker.process(self.payload)['eligible'])
        self.executor.evaluate_and_enter.assert_not_called()
    def test_source_change_during_preparation_cannot_enter(self):
        def history(_):
            sf.Recorder().invalidate(self.chain.index.name)
            return self.bars
        self.worker.history=history
        with self.assertRaisesRegex(ValueError,'snapshot_changed'):
            self.worker.process(self.payload)
        self.executor.evaluate_and_enter.assert_not_called()
    def test_revalidation_checks_pause_after_candidate(self):
        def execute(*args,**kwargs):
            sf.update_controls({'live_enabled':False},1)
            self.assertIsNone(kwargs['fast_context']())
            self.assertFalse(self.chain._entry_guard())
            return dict(ok=False,reason='fast_invalidated_before_submission')
        self.executor.evaluate_and_enter.side_effect=execute
        self.assertFalse(self.worker.process(self.payload)['dispatch']['ok'])
    def test_revalidation_checks_replaced_snapshot(self):
        def execute(*args,**kwargs):
            sf.Recorder().invalidate(self.chain.index.name)
            self.assertIsNone(kwargs['fast_context']())
            return dict(ok=False,reason='stock_signal_changed_before_submission')
        self.executor.evaluate_and_enter.side_effect=execute
        self.assertFalse(self.worker.process(self.payload)['dispatch']['ok'])
    def test_revalidation_keeps_shared_news_block(self):
        def execute(*args,**kwargs):
            self.fast.live_blocks=lambda *a:['fresh_news_coverage_required']
            self.assertFalse(kwargs['fast_context']()['eligible'])
            return dict(ok=False,reason='fast_context_or_live_gate_blocked')
        self.executor.evaluate_and_enter.side_effect=execute
        self.assertFalse(self.worker.process(self.payload)['dispatch']['ok'])
    def test_owned_partial_position_protection_survives_entry_pause(self):
        sf.bind_policy(dict(episode='E',holding_minutes=5,session_exit=sf.session_exit(NOW).isoformat()))
        sf.update_controls({'live_enabled':False},1)
        pos=types.SimpleNamespace(signal_id=7,entry_ts=NOW-timedelta(minutes=6),
            kind='fast_momentum',status='entry_pending',remaining_qty=20)
        self.executor.state.positions={'7':pos}
        self.worker.protect()
        self.executor.manual_exit.assert_called_once_with(7,'stock_fast_holding_deadline')
    def test_session_exit_handles_pending_unfilled_order(self):
        sf.bind_policy(dict(episode='E',holding_minutes=15,session_exit=sf.session_exit(NOW).isoformat()))
        pos=types.SimpleNamespace(signal_id=7,entry_ts=None,kind='fast_momentum',status='entry_pending',remaining_qty=0)
        self.executor.state.positions={'7':pos}
        with patch.object(sf,'_now',return_value=NOW.replace(hour=15,minute=15)):
            self.worker.protect()
        self.executor.manual_exit.assert_called_once_with(7,'stock_fast_session_exit')
    def test_unrelated_fast_position_not_owned_by_stock_policy(self):
        self.executor.state.positions={'99':types.SimpleNamespace(signal_id=99,entry_ts=NOW-timedelta(hours=1),
            kind='fast_momentum',status='open',remaining_qty=20)}
        self.worker.protect()
        self.executor.manual_exit.assert_not_called()

class ApiTests(Base):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from kadima import stock_fast_api
        def auth(token):
            if token!='Bearer test': raise HTTPException(401,'invalid_token')
        executor=types.SimpleNamespace(fast_rows={},_lock=threading.RLock(),state=types.SimpleNamespace(positions={}))
        self.worker=types.SimpleNamespace(executor=executor,thread=None)
        control=types.SimpleNamespace(app=FastAPI(),_check_auth=auth)
        stock_fast_api.register(control,self.worker)
        self.client=TestClient(control.app)
        self.headers={'Authorization':'Bearer test'}
    def test_every_route_requires_auth(self):
        self.assertEqual(self.client.get('/api/kadima/stock-fast').status_code,401)
        self.assertEqual(self.client.post('/api/kadima/stock-fast/config',json={}).status_code,401)
    def test_get_is_read_only_and_not_a_fill(self):
        result=self.client.get('/api/kadima/stock-fast',headers=self.headers).json()
        self.assertFalse(result['controls']['live_enabled']); self.assertEqual(result['positions'],[])
    def test_write_does_not_place_order_and_revision_conflicts(self):
        payload={'patch':{'live_enabled':True},'expected_revision':0}
        response=self.client.post('/api/kadima/stock-fast/config',headers=self.headers,json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.post('/api/kadima/stock-fast/config',headers=self.headers,json=payload).status_code,409)
    def test_partial_fill_and_pending_are_not_reported_as_full_fill(self):
        sf.bind_policy(dict(episode='E',holding_minutes=5,session_exit=sf.session_exit(NOW).isoformat()))
        pos=types.SimpleNamespace(signal_id=7,index='UNOMINDA',symbol='CE',status='entry_pending',
                                 remaining_qty=0,entry_price=0,effective_stop=9)
        self.worker.executor.state.positions={'7':pos}
        data=self.client.get('/api/kadima/stock-fast',headers=self.headers).json()
        self.assertEqual(data['positions'][0]['execution_stage'],'PENDING_NOT_FILLED')
        pos.remaining_qty=20
        data=self.client.get('/api/kadima/stock-fast',headers=self.headers).json()
        self.assertEqual(data['positions'][0]['execution_stage'],'PARTIAL_FILL')

    def test_paused_controls_invalidate_cached_ready_display(self):
        self.worker.executor.fast_rows={'UNOMINDA':dict(underlying='UNOMINDA',asset_scope='STOCK',
            quote_at=NOW.isoformat(),eligible=True,blocked_reasons=[])}
        data=self.client.get('/api/kadima/stock-fast',headers=self.headers).json()
        self.assertFalse(data['rows'][0]['eligible'])
        self.assertIn('stock_fast_live_disabled',data['rows'][0]['blocked_reasons'])

class UpgradeTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'stock_fast_update'/'upgrade_dashboard.py'
        spec=importlib.util.spec_from_file_location('sf_upgrade',path)
        self.mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.mod)
    def test_dashboard_patch_is_additive_and_idempotent(self):
        text='<html><body>Existing controls</body></html>'
        result=self.mod.patch_dashboard(text,'console.log("ready");')
        self.assertIn('Existing controls',result)
        self.assertEqual(result,self.mod.patch_dashboard(result,'new code'))
    def test_gateway_reuses_authenticated_existing_write_path(self):
        text="def authenticated(request): pass\ndef origin_check(request): pass\nREADS={}\nCELESTIAL_WRITES=set()\n@app.post('/celestial/{path:path}')\ndef route(): pass\n"
        result=self.mod.patch_gateway(text); ast.parse(result)
        self.assertIn("CELESTIAL_WRITES.add('kadima/stock-fast/config')",result)
        self.assertEqual(result,self.mod.patch_gateway(result))
    def test_unknown_gateway_and_unsafe_script_rejected(self):
        with self.assertRaises(ValueError): self.mod.patch_gateway('app = None')
        with self.assertRaises(ValueError): self.mod.patch_dashboard('<body></body>','</script>')

if __name__=='__main__': unittest.main()
