"""Offline tests of policy boundaries, persistence and asymmetric CE/PE structure."""
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from kadima import trade_policy as p
import test_execution_lifecycle as fixtures
from test_execution_lifecycle import executor, storage, PKG

NOW=datetime(2026,9,14,10,tzinfo=timezone.utc)

def bars(lows=(102,101,98,100,101), highs=(104,105,108,106,105)):
    return [dict(start_ts=(NOW-timedelta(minutes=25-5*i)).isoformat(),low=l,high=h)
            for i,(l,h) in enumerate(zip(lows,highs))]

class Policy(unittest.TestCase):
    def test_confirmation_score_and_alignment(self):
        bull=SimpleNamespace(vwap=100,ema_fast=102,ema_slow=101,rsi=60)
        bear=SimpleNamespace(vwap=100,ema_fast=98,ema_slow=99,rsi=40)
        self.assertTrue(p.eligible('gamma_blast','CONFIRMED',70,'CE',bull,101))
        self.assertTrue(p.eligible('gamma_blast','CONFIRMED',70,'PE',bear,99))
        for tier,score,snap,direction,spot in [('ACTIVE',99,bull,'CE',101),
                ('CONFIRMED',69,bull,'CE',101),('CONFIRMED',90,bull,'PE',99),
                ('CONFIRMED',90,None,'CE',101),('CONFIRMED',float('nan'),bull,'CE',101)]:
            self.assertFalse(p.eligible('gamma_blast',tier,score,direction,snap,spot))
    def test_confirmed_unbroken_pivots(self):
        self.assertEqual(p.swing(bars(),'CE',103,NOW),98)
        self.assertEqual(p.swing(bars(),'PE',103,NOW),108)
        self.assertIsNone(p.swing(bars(),'CE',97,NOW))
        self.assertIsNone(p.swing(bars(),'PE',109,NOW))
        self.assertIsNone(p.swing(bars()[:4],'CE',103,NOW))
    def test_stale_future_and_missing_bars_fail(self):
        for rows,now in [(bars(),NOW+timedelta(minutes=7)),
                         (bars(),NOW-timedelta(seconds=1)),
                         (bars()[1:],NOW), (bars(),NOW+timedelta(days=1))]:
            self.assertIsNone(p.swing(rows,'CE',103,now))
    def test_mapping_both_long_option_directions(self):
        for direction,level in [('CE',98),('PE',102)]:
            quote=p.bs_price(100,100,2,.3,direction)
            stop=p.premium_stop(100,level,100,quote,2,direction)
            self.assertGreater(stop,0); self.assertLess(stop,quote)
        self.assertIsNone(p.premium_stop(100,98,100,1000,2,'CE'))
        self.assertIsNone(p.premium_stop(100,98,100,10,0,'CE'))
    def test_fifteen_percent_allocation_costs_and_whole_lots(self):
        # 100-unit contract, 100 entry, 50 stop: 5,225 rupees planned risk.
        self.assertAlmostEqual(p.risk_per_unit(100,50),52.25)
        self.assertEqual(p.lots_within_allocation(30000,100,50,100,9),0)
        self.assertEqual(p.lots_within_allocation(40000,100,50,100,9),1)
        self.assertEqual(p.lots_within_allocation(10000,100,99,100,9),0) # fee reserve
        for bad in (0,-1,float('nan'),float('inf'),None):
            self.assertEqual(p.lots_within_allocation(bad,100,90,100,9),0)
    def test_activation_peak_profit_and_no_widening(self):
        self.assertEqual(p.ratchet(100,104.99,90,90),(90,False))
        self.assertEqual(p.ratchet(100,105,90,90),(102.5,True))
        self.assertEqual(p.ratchet(100,140,125,90),(125,True))
        self.assertEqual(p.ratchet(100,140,120,130),(130,True))

class Persistence(unittest.TestCase):
    setUp = fixtures.Lifecycle.setUp
    tearDown = fixtures.Lifecycle.tearDown
    def test_policy_survives_database_reload(self):
        self.pos.structural_stop=91.5
        self.pos.effective_stop=110
        self.pos.swing_level=9900
        self.pos.allocated_capital=40000
        storage.upsert_position(self.pos)
        row=storage.load_open_positions()[0]
        for field in ('structural_stop','effective_stop','swing_level','allocated_capital'):
            self.assertEqual(row[field],getattr(self.pos,field))
    def test_structure_ratchet_rejects_widening(self):
        self.pos.structural_stop=90
        self.pos.effective_stop=110
        self.pos.swing_level=9900
        chain=SimpleNamespace(_last_poll=executor.time.monotonic(),quotes={'T':SimpleNamespace(strike=10000,ltp=120)})
        with patch.object(self.ex,'_structure',return_value=(9910,1)), patch.object(executor.trade_policy,'premium_stop',return_value=95):
            self.ex.update_structure('INDEX',chain,10000)
        self.assertEqual((self.pos.structural_stop,self.pos.effective_stop),(95,110))
        with patch.object(self.ex,'_structure',return_value=(9890,1)), patch.object(executor.trade_policy,'premium_stop',return_value=80):
            self.ex.update_structure('INDEX',chain,10000)
        self.assertEqual((self.pos.structural_stop,self.pos.effective_stop),(95,110))


class RiskSelection(unittest.TestCase):
    def test_actual_selector_rejects_missing_stop_and_sizes_allocation(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location(PKG+'.policy_risk',
            fixtures.ROOT/'kadima/risk_budget.py')
        risk=importlib.util.module_from_spec(spec); fixtures.sys.modules[spec.name]=risk
        spec.loader.exec_module(risk)
        c=risk.StrikeCandidate('TESTCE','T',10000,'CE',100,99.5,100.5,.5,2000,6000)
        self.assertIsNone(risk.select_within_budget([c],10000,allocated_capital=4000).candidate)
        c.stop_price=50
        self.assertIsNotNone(risk.select_within_budget([c],10000,allocated_capital=4000).candidate)
        self.assertIsNone(risk.select_within_budget([c],10000,allocated_capital=3000).candidate)
        c.ask=105
        self.assertIsNone(risk.select_within_budget([c],10000,allocated_capital=4000).candidate)
