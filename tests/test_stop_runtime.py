"""Execution integration using mocked broker transport and real SQLite ledger."""
import copy
from dataclasses import replace
import importlib
import unittest
from datetime import timedelta
from unittest.mock import patch
from kadima import config as real_config
import test_execution_lifecycle as f

r=importlib.import_module(f.PKG+'.stop_runtime')
s=r.stops


class Runtime(unittest.TestCase):
    def setUp(self):
        f.Lifecycle.setUp(self)
        for key in ('STOP_ENGINE','STOP_REGIMES','STRUCTURE_GUARD','STRUCTURE_GUARD_VALIDATIONS',
                    'SESSION_MODIFIERS','MANUAL_STOP_POLICY'):
            patch.object(f.config,key,copy.deepcopy(getattr(real_config,key)),create=True).start()
        patch.object(f.config,'REGIME_STOPS_ENABLED',True,create=True).start()
        self.pos.kind='bull_shield'
        self.pos.structural_stop=90
        self.pos.effective_stop=90
        self.now=f.NOW.replace(hour=4)  # Before CAS in IST.
        self.pos.entry_ts=self.now
        self.market=dict(now=self.now,ts=self.now,expiry='2026-09-29',spot=1000,
            atr_spot_5m=10,atr_premium=2,premium_bar_seconds=300,vwap=980,ema20=975,swing=985,
            oi_wall=980,expected_iv_crush_points=1,
            hard_exit=self.now.replace(hour=10),session_close=self.now.replace(hour=10),
            broker_squareoff=self.now.replace(hour=10))
        signal=dict(source='bull_shield',asset='INDEX',direction='CE',score=95,
            risk_budget_inr=10000,available_capital_inr=100000,max_lots=10,costs_per_unit=.5)
        row=dict(entry=100,bid=99.5,ask=100.5,lot_size=10,token='T',exchange='NFO',
                 delta=.5,gamma=.001,vega=1,ts=self.now)
        self.plan=s.initial_stop(signal,row,self.market)
        self.assertIsInstance(self.plan,s.StopPlan)
        patch.object(f.executor,'now_ist',return_value=self.now+timedelta(seconds=1)).start()
        self.ex.publish_stop_context(1,dict(self.market,asset='INDEX',tick_size=.05))
        f.storage.upsert_position(self.pos)

    tearDown=f.Lifecycle.tearDown

    def bind(self):
        return self.ex.bind_stop_plan(1,self.plan)

    def depth(self,now=None,bid=80,ask=81):
        with patch.object(f.executor.celestial,'now_ist',return_value=now or self.now+timedelta(seconds=1)):
            f.executor.celestial.health('quote:NFO:T',dict(bid=bid,ask=ask))

    def test_binding_requires_ownership_identity_and_final_fill(self):
        self.ex._positions_verified=False
        with self.assertRaises(ValueError):self.bind()
        self.ex._positions_verified=True
        self.pos.status='entry_pending'
        with self.assertRaises(ValueError):self.bind()
        self.pos.status='open'
        self.pos.entry_price=101
        with self.assertRaises(ValueError):self.bind()
        self.pos.entry_price=100
        state=self.bind()
        self.assertEqual(state.active_stop,max(90,self.plan.initial_stop))
        with self.assertRaises(ValueError):self.bind()

    def test_real_monitor_routes_bound_exit_through_ledger(self):
        self.bind();self.depth()
        with patch.object(f.executor,'_poll_ltp',return_value={'T':80}),patch.object(f.executor.celestial,'enabled',return_value=False):
            self.ex.check_exits()
        self.assertEqual(self.pos.status,'exit_pending')
        self.assertEqual(self.pos.remaining_qty,10)
        state=s.load_state(1,f.storage.conn())
        self.assertEqual(state.remaining_qty,10)
        self.fill(4,80)
        self.assertEqual(s.load_state(1,f.storage.conn()).remaining_qty,6)
        self.fill(4,80)
        self.assertEqual(s.load_state(1,f.storage.conn()).remaining_qty,6)
        self.fill(10,80,'complete')
        self.assertEqual(s.load_state(1,f.storage.conn()).remaining_qty,0)
        self.assertEqual(self.pnl(),-200)

    fill=f.Lifecycle.fill
    pnl=f.Lifecycle.pnl

    def test_rejected_order_and_cancelled_partial_remain_latched(self):
        self.bind();self.depth()
        self.place.return_value=f.orders.OrderResult(False,None,'rate_limited')
        result=self.ex._close_position(self.pos,80,'operator_exit')
        self.assertFalse(result['ok'])
        self.assertEqual(self.pos.status,'open')
        self.assertEqual(s.load_state(1,f.storage.conn()).pending['quantity'],10)
        self.place.return_value=f.orders.OrderResult(True,'O1','ok')
        self.ex._close_position(self.pos,80,'operator_exit')
        self.fill(4,80,'cancelled')
        state=s.load_state(1,f.storage.conn())
        self.assertEqual((state.remaining_qty,state.pending['quantity']),(6,6))
        self.place.return_value=f.orders.OrderResult(True,'O2','ok')
        self.ex._close_position(self.pos,80,'operator_exit')
        self.assertEqual(self.place.call_args.kwargs['quantity'],6)
        self.fill(6,80,'complete','O2')
        self.assertEqual(s.load_state(1,f.storage.conn()).remaining_qty,0)

    def test_stale_depth_cannot_be_relabelled_as_fresh_by_ltp(self):
        self.bind();self.depth(now=self.now-timedelta(seconds=10))
        with patch.object(f.executor,'_poll_ltp',return_value={'T':80}):self.ex.check_exits()
        self.place.assert_not_called()
        self.assertEqual(s.load_state(1,f.storage.conn()).pending['action'],'EXIT')

    def test_restart_and_disabled_flag_do_not_drop_existing_binding(self):
        self.bind();self.depth()
        f.config.REGIME_STOPS_ENABLED=False
        self.ex._regime_contexts={}
        with patch.object(f.executor,'_poll_ltp',return_value={'T':80}):self.ex.check_exits()
        self.assertEqual(self.pos.status,'exit_pending')

    def test_manual_cas_skips_legacy_and_daily_loss_but_not_session_exit(self):
        self.pos.kind='MANUAL_ADOPTED'
        at=self.now.replace(hour=9,minute=45)  # 15:15 IST.
        with patch.object(f.executor,'now_ist',return_value=at),patch.object(f.executor,'_poll_ltp',return_value={'T':1}),patch.object(f.exit_manager,'realized_pnl_today',return_value=-100000):
            self.ex.check_exits()
        self.place.assert_not_called()
        with patch.object(f.executor,'now_ist',return_value=at.replace(hour=15,minute=38)),patch.object(f.executor,'_poll_ltp',return_value={'T':1}):
            self.ex.check_exits()
        self.place.assert_called_once()

    def test_operator_exit_in_manual_cas_not_suppressed(self):
        self.pos.kind='MANUAL_ADOPTED'
        at=self.now.replace(hour=9,minute=45)
        with patch.object(f.executor,'now_ist',return_value=at):
            self.ex._close_position(self.pos,80,'manual_exit_dashboard')
        self.place.assert_called_once()

    def test_invalid_link_rolls_back_intent_and_memory(self):
        state=self.bind()
        with self.assertRaises(ValueError):
            f.ledger.submit(self.pos,'SELL',10,80,stop_state=state)
        self.assertEqual(self.pos.status,'open')
        self.assertEqual(f.ledger.pending(1),[])
        self.place.assert_not_called()

    def test_fill_failure_rolls_back_all_cursors(self):
        self.bind();self.depth()
        self.ex._close_position(self.pos,80,'operator_exit')
        with patch.object(r,'confirm_fill',side_effect=ValueError('corrupt_binding')):
            self.fill(4,80)
        self.assertEqual(self.pos.remaining_qty,10)
        self.assertEqual(f.ledger.pending(1)[0]['filled'],0)
        self.assertEqual(s.load_state(1,f.storage.conn()).remaining_qty,10)
        self.assertEqual(self.pnl(),0)

    def test_full_exit_waits_for_partial_cancel_confirmation(self):
        state=self.bind();self.depth()
        decision=s._decision(state,'PARTIAL','test_partial',.5)
        # Two lots are needed to produce a partial whole-lot exit.
        state.plan=replace(state.plan,lot_size=1)
        state.pending=None
        decision=s._decision(state,'PARTIAL','test_partial',.5)
        s.save_state(state,f.storage.conn())
        self.ex._execute_regime_decision(self.pos,state,decision,self.now+timedelta(seconds=1))
        state,decision=r.decide(self.pos,{},None,self.now,'operator_exit')
        self.ex._execute_regime_decision(self.pos,state,decision,self.now+timedelta(seconds=1))
        self.ex._execute_regime_decision(self.pos,state,decision,self.now+timedelta(seconds=1))
        self.cancel.assert_called_once_with('O1')
        self.assertEqual(self.place.call_count,1)
        self.assertEqual(self.pos.remaining_qty,10)
        self.fill(2,80,'cancelled')
        self.assertEqual(self.pos.remaining_qty,8)
        self.assertEqual(s.load_state(1,f.storage.conn()).pending['quantity'],8)

    def test_dashboard_tighten_updates_persisted_stop(self):
        self.bind()
        with patch.object(f.executor,'_poll_ltp',return_value={'T':110}):
            result=self.ex.tighten_stop('1',95)
        self.assertTrue(result['ok'])
        self.assertEqual(s.load_state(1,f.storage.conn()).active_stop,95)
