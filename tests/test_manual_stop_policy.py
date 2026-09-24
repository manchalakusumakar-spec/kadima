"""User-approved manual policy; isolated draft engine, no broker calls."""
import sqlite3
import json
import unittest
from datetime import timedelta
from dataclasses import replace
import test_regime_stops as fixture

s=fixture.stops
NOW=fixture.NOW


class ManualPolicy(unittest.TestCase):
    def setUp(self):
        f=fixture.RegimeStops();f.setUp()
        self.signal=dict(f.signal,source='MANUAL_ADOPTED',risk_budget_inr=5000,score=0)
        self.market=dict(f.market,hard_exit=NOW.replace(hour=15,minute=30))
        self.row=f.row
        self.plan=s.initial_stop(self.signal,self.row,self.market)
        self.assertIsInstance(self.plan,s.StopPlan)
        self.state=s.open_position(self.plan,901)

    def decide(self,at,price=1):
        return s.update_stop(self.state,dict(token='1',exchange='NFO',ltp=price,ts=at),
                             dict(self.market,now=at,ts=at))

    def test_exact_source_mapping_and_preexpiry_manual_exception(self):
        for source,regime in [('manual','INTRADAY'),('MANUAL_ADOPTED','INTRADAY'),
                ('gamma_pre_entry','GAMMA'),('gamma_blast','GAMMA'),('index_scalp','SCALP'),
                ('fast_momentum','SCALP'),('cas_blast','CAS'),('sniper','INTRADAY')]:
            self.assertEqual(s.assign_regime(dict(source=source),{}),regime)
        self.assertEqual(s.assign_regime(dict(source='manual',score=0),{'expiry_minus_one':True}),'INTRADAY')

    def test_manual_reserves_full_premium_plus_cost(self):
        self.assertEqual(self.plan.allowed_lots,4)
        self.assertEqual(self.plan.source,'MANUAL_ADOPTED')
        self.assertEqual(self.plan.manual_cas_start,NOW.replace(hour=15,minute=15).isoformat())

    def test_pre_cas_stops_apply(self):
        self.assertEqual(self.decide(NOW.replace(hour=15,minute=14,second=59)).action,'EXIT')

    def test_exact_cas_boundary_disables_price_and_profit_stops(self):
        self.state.active_stop=110
        self.state.protected_stop=110
        self.state.peak_premium=130
        result=self.decide(NOW.replace(hour=15,minute=15))
        self.assertEqual((result.action,result.reason,result.stop),('HOLD','manual_cas_no_stop',None))
        self.assertIsNone(self.state.pending)

    def test_cas_thesis_cannot_reintroduce_stop(self):
        at=NOW.replace(hour=15,minute=16)
        result=s.thesis_check(self.state,dict(now=at,ts=at,shield_invalidated=True,shield_score=0,
                    bar=dict(closed=True,seconds=300,ts=at)))
        self.assertEqual(result.reason,'manual_cas_no_stop')

    def test_session_exit_works_without_quote_or_thesis(self):
        at=NOW.replace(hour=15,minute=30)
        result=s.update_stop(self.state,{},dict(now=at))
        self.assertEqual(result.action,'EXIT')
        other=s.open_position(self.plan,902)
        self.assertEqual(s.thesis_check(other,dict(now=at)).action,'EXIT')

    def test_exit_already_latched_cannot_be_cancelled_by_cas(self):
        first=self.decide(NOW.replace(hour=15,minute=14))
        later=self.decide(NOW.replace(hour=15,minute=15),price=200)
        self.assertEqual(first,later)

    def test_restart_preserves_manual_policy(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        s.save_state(self.state,db)
        self.state=s.load_state(901,db)
        self.assertEqual(self.decide(NOW.replace(hour=15,minute=15)).reason,'manual_cas_no_stop')

    def test_automated_intraday_not_given_manual_override(self):
        self.state.plan=replace(self.plan,source='bull_shield',manual_cas_start=None)
        self.assertEqual(self.decide(NOW.replace(hour=15,minute=15)).action,'EXIT')

    def test_earlier_session_deadline_still_binding(self):
        self.state.plan=replace(self.plan,hard_exit=NOW.replace(hour=15,minute=15).isoformat())
        self.assertEqual(self.decide(NOW.replace(hour=15,minute=15)).action,'EXIT')

    def test_cas_with_missing_quote_does_not_revive_structure_deadline(self):
        self.state.structure_guard=dict(status='ACTIVE',used=True,
            deadline=NOW.replace(hour=15,minute=15).isoformat())
        result=s.update_stop(self.state,{},dict(now=NOW.replace(hour=15,minute=15)))
        self.assertEqual(result.reason,'manual_cas_no_stop')
        self.assertEqual(self.state.structure_guard['status'],'SPENT')

    def test_pending_partial_and_queued_exit_remain_reconcilable(self):
        partial=s._decision(self.state,'PARTIAL','existing_partial',.5)
        s.acknowledge_fill(self.state,5,terminal=False)
        self.assertEqual(self.decide(NOW.replace(hour=15,minute=15)).action,'PARTIAL')
        self.assertEqual(self.state.counters['pending_filled'],5)
        self.decide(NOW.replace(hour=15,minute=30))
        s.acknowledge_fill(self.state,5,terminal=True)
        self.assertEqual(self.state.pending['action'],'EXIT')
        self.assertEqual(self.state.pending['quantity'],self.state.remaining_qty)

    def test_v2_does_not_gain_manual_permission_and_clock_corruption_rejected(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        self.state.plan=replace(self.plan,source='bull_shield',manual_cas_start=None)
        s.save_state(self.state,db)
        payload=json.loads(db.execute('SELECT payload FROM regime_stop_states').fetchone()[0])
        payload['version']=2
        payload['state']['plan'].pop('source')
        payload['state']['plan'].pop('manual_cas_start')
        db.execute('UPDATE regime_stop_states SET payload=?',(json.dumps(payload),))
        old=s.load_state(901,db)
        self.assertEqual(old.plan.source,'')
        self.assertIsNone(old.plan.manual_cas_start)
        self.state.plan=replace(self.plan,manual_cas_start=NOW.replace(hour=9).isoformat())
        s.save_state(self.state,db)
        with self.assertRaises(ValueError):
            s.load_state(901,db)
