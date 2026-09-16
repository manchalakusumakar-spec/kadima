"""Offline proof of the Shield-to-order boundary; no network or live orders."""
import importlib
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_execution_lifecycle as f
from kadima import config, utils
from kadima.shield_confirmation import (BANK_NAMES, ShieldEvidence,
    confirmation_checks, is_shield_confirmed)

NOW = f.NOW

def evidence(direction='CE', score=65, **overrides):
    bull = direction == 'CE'
    snap = SimpleNamespace(vwap=100, ema_fast=102 if bull else 98,
                           ema_slow=101 if bull else 99, rsi=60 if bull else 40)
    e = ShieldEvidence('NIFTY', direction, score, snap, 101 if bull else 99,
        closed_at=NOW, technical_at=NOW, spot_at=NOW,
        prior_closed_at=NOW-timedelta(minutes=5), prior_direction=direction,
        prior_ready=True, oi_confirmed=True, oi_at=NOW)
    return replace(e, **overrides)


class Gate(unittest.TestCase):
    def test_threshold_both_directions(self):
        for direction in ('CE','PE'):
            for score in (65,70,80,90,100):
                self.assertTrue(is_shield_confirmed(evidence(direction,score),NOW))
            for score in (64.99,0,-1,101,True,float('nan'),float('inf'),None):
                self.assertFalse(is_shield_confirmed(evidence(direction,score),NOW))

    def test_all_conditions_are_required(self):
        e=evidence()
        bad=[dict(prior_ready=False),dict(prior_direction='PE'),dict(prior_closed_at=None),
             dict(prior_closed_at=NOW),dict(prior_closed_at=NOW-timedelta(minutes=10)),
             dict(closed_at=None),dict(closed_at=NOW-timedelta(minutes=7)),
             dict(closed_at=NOW+timedelta(seconds=1)),dict(oi_confirmed=False),
             dict(oi_at=NOW-timedelta(seconds=31)),dict(technical_at=NOW-timedelta(seconds=241)),
             dict(spot_at=NOW-timedelta(seconds=31)),dict(invalidated=True),
             dict(spot=100),dict(snap=SimpleNamespace(vwap=100,ema_fast=100,ema_slow=100,rsi=60)),
             dict(snap=SimpleNamespace(vwap=100,ema_fast=102,ema_slow=101,rsi=50))]
        for fields in bad:
            with self.subTest(fields=fields):
                self.assertFalse(is_shield_confirmed(replace(e,**fields),NOW))
        self.assertFalse(is_shield_confirmed(None,NOW))

    def test_bank_requires_two_named_banks(self):
        for direction in ('CE','PE'):
            e=evidence(direction,index='BANKNIFTY')
            self.assertFalse(is_shield_confirmed(e,NOW))
            self.assertFalse(is_shield_confirmed(replace(e,banks={'HDFCBANK':True,'OTHER':True}),NOW))
            self.assertTrue(is_shield_confirmed(replace(e,banks={x:True for x in BANK_NAMES[:2]}),NOW))

    def test_policy_cannot_bypass_shared_gate(self):
        for direction in ('CE','PE'):
            e=evidence(direction)
            for kind in ('bull_shield','bear_shield'):
                args=(kind,'CONFIRMED',65,direction,e.snap,e.spot)
                self.assertFalse(f.executor.trade_policy.eligible(*args))
                # The fixture package owns its own module class; use its evidence type.
                module=importlib.import_module(f.PKG+'.shield_confirmation')
                fe=module.ShieldEvidence(**e.__dict__)
                self.assertTrue(f.executor.trade_policy.eligible(*args,shield_evidence=fe,now=NOW))
                self.assertFalse(f.executor.trade_policy.eligible(*args,shield_evidence=replace(fe,oi_confirmed=False),now=NOW))


def load_engine():
    for key in dir(config):
        if not hasattr(f.config,key):setattr(f.config,key,getattr(config,key))
    for key in ('past_cutoff','candle_start'):
        setattr(f.pkg.utils,key,getattr(utils,key))
    return importlib.import_module(f.PKG+'.engine')


class EnginePath(unittest.TestCase):
    def setUp(self):
        self.m=load_engine()
        self.engine=self.m.Engine.__new__(self.m.Engine)
        self.engine.states={};self.engine.bank_states={}
        self.now=NOW
        self.patches=[patch.object(self.m,'now_ist',side_effect=lambda:self.now),
            patch.object(self.m.auto_executor.celestial,'enabled',return_value=False),
            patch.object(self.m,'in_market_hours',return_value=True),
            patch.object(self.m,'past_signal_cutoff',return_value=False)]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        self.state=self.m.IndexState('NIFTY')
        self.engine.states={'NIFTY':self.state}
        self.bull(True)

    def bull(self,bull):
        self.state.snap=self.m.indicators.Snapshot(close=101 if bull else 99,vwap=100,
            ema_fast=102 if bull else 98,ema_slow=101 if bull else 99,rsi=60 if bull else 40)
        self.state.spot=101 if bull else 99
        self.state.chain=SimpleNamespace(_last_poll=f.executor.time.monotonic(),
            velocity=SimpleNamespace(alerts=lambda:[{'token':'C' if bull else 'P'}]),
            quotes={'C':SimpleNamespace(token='C',opt_type='CE'),
                    'P':SimpleNamespace(token='P',opt_type='PE')},pcr=lambda:1,max_pain=lambda:100)

    def close(self,minutes=0):
        self.now+=timedelta(minutes=minutes)
        self.state.last_updated=self.state.technical_at=self.now
        self.state.chain._last_poll=f.executor.time.monotonic()
        self.engine._score(self.state,self.now,self.state.spot)

    def test_score_alone_is_not_confirmed(self):
        self.state.chain.velocity.alerts=lambda:[]
        self.close();self.close(5)
        self.assertEqual((self.state.score,self.state.tier),(65,'ACTIVE'))
        self.assertFalse(self.state.components['confirmation']['oi'])

    def test_hold_consecutive_5m_both_directions(self):
        self.close();self.assertEqual(self.state.tier,'ACTIVE')
        self.close(5);self.assertEqual(self.state.tier,'CONFIRMED')
        self.bull(False)
        self.close(5);self.assertEqual(self.state.tier,'ACTIVE')
        self.close(5);self.assertEqual((self.state.direction,self.state.tier),('PE','CONFIRMED'))

    def test_repeat_and_missing_bars_cannot_satisfy_hold(self):
        self.close();self.close()
        self.assertEqual(self.state.tier,'ACTIVE')
        self.close(10);self.assertEqual(self.state.tier,'ACTIVE')
        self.close(5);self.assertEqual(self.state.tier,'CONFIRMED')

    def test_flip_between_closes_cancels_hold(self):
        self.close();self.close(5)
        self.state.spot=99;self.engine._invalidate_shield(self.state)
        self.assertEqual(self.state.tier,'ACTIVE')
        self.state.spot=101;self.close(5)
        self.assertEqual(self.state.tier,'ACTIVE')
        self.close(5);self.assertEqual(self.state.tier,'ACTIVE')
        self.close(5);self.assertEqual(self.state.tier,'CONFIRMED')

    def test_restart_does_not_inherit_confirmed_without_evidence(self):
        saved={'NIFTY':dict(spot=101,score=65,tier='CONFIRMED',direction='CE',ts=NOW.isoformat())}
        with patch.object(self.m.last_known,'load_all',return_value=saved):
            self.engine._seed_last_known()
        self.assertEqual(self.state.tier,'NONE')
        self.close();self.close(5)
        self.assertEqual(self.state.tier,'CONFIRMED')
        self.assertIsNotNone(self.state.tier_since)
        self.assertTrue(self.state.components)

    def test_bank_votes_reject_stale_wrong_direction_or_missing_data(self):
        for name in BANK_NAMES[:2]:
            self.engine.bank_states[name]=self.m.IndexState(name,spot=101,snap=self.state.snap,
                technical_at=NOW,last_updated=NOW)
        self.assertEqual(sum(self.engine._bank_votes('CE',NOW).values()),2)
        self.assertEqual(sum(self.engine._bank_votes('PE',NOW).values()),0)
        self.assertEqual(sum(self.engine._bank_votes('CE',NOW+timedelta(seconds=31)).values()),0)

    def test_signal_retries_same_id_and_ledger_blocks_duplicate(self):
        self.close();self.close(5)
        emitted=Mock(return_value=42);entered=Mock(return_value=dict(ok=False,reason='quote_stale'))
        with patch.object(self.m,'INDICES',{'NIFTY':SimpleNamespace(weight=1)}), \
             patch.object(self.m.storage,'insert_signal',emitted), \
             patch.object(self.m.strike_selector,'best',return_value=SimpleNamespace(as_dict=lambda:{}),create=True), \
             patch.object(self.engine,'_dte',return_value=1), \
             patch.object(self.m.alerts,'send_signal',Mock(),create=True), \
             patch.object(self.m.auto_executor.execution_ledger,'seen',return_value=False) as seen, \
             patch.object(self.m.auto_executor.EXECUTOR,'evaluate_and_enter',entered):
            self.engine._evaluate();self.engine._evaluate()
            emitted.assert_called_once()
            self.assertEqual(entered.call_count,2)
            self.assertEqual(entered.call_args.kwargs['source_signal_id'],42)
            self.assertTrue(self.m.is_shield_confirmed(entered.call_args.kwargs['shield_context'](),self.now))
            seen.return_value=True
            self.engine._evaluate();self.assertEqual(entered.call_count,2)


class Transport(unittest.TestCase):
    def test_live_transport_logs_request_response_with_same_signal(self):
        with patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',True), \
             patch.object(f.orders.SESSION.api,'placeOrder',return_value='O1') as broker, \
             patch.object(f.orders.audit,'log') as log:
            r=f.orders.place_limit_order(tradingsymbol='TESTCE',symboltoken='T',exchange='NFO',
                transactiontype='BUY',quantity=10,price=100,ordertag='INTENT',signal_id=42)
            self.assertTrue(r.ok)
            self.assertEqual(broker.call_count,1)
            events=[c for c in log.call_args_list if c.args[0].startswith('ANGELONE_')]
            self.assertEqual([c.args[0] for c in events],['ANGELONE_REQUEST','ANGELONE_RESPONSE'])
            self.assertTrue(all(c.kwargs['signal_id']==42 and c.kwargs['intent']=='INTENT' for c in events))

    def test_dry_run_does_not_claim_broker_request(self):
        with patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',False), \
             patch.object(f.orders.SESSION.api,'placeOrder') as broker, \
             patch.object(f.orders.audit,'log') as log:
            r=f.orders.place_limit_order(tradingsymbol='TESTCE',symboltoken='T',exchange='NFO',
                transactiontype='BUY',quantity=10,price=100,signal_id=42)
            self.assertTrue(r.dry_run);broker.assert_not_called()
            self.assertNotIn('ANGELONE_REQUEST',[c.args[0] for c in log.call_args_list])

import test_celestial as cf

class Dispatch(unittest.TestCase):
    setUp=cf.Celestial.setUp
    tearDown=cf.Celestial.tearDown
    entry_fixture=cf.Celestial.entry_fixture
    arm=cf.Celestial.arm

    def args(self,direction='CE'):
        args=self.entry_fixture()
        patch.object(f.executor.celestial,'enabled',return_value=False).start()
        snap=SimpleNamespace(vwap=9990 if direction=='CE' else 10010,
            ema_fast=10000,ema_slow=9990 if direction=='CE' else 10010,rsi=60 if direction=='CE' else 40)
        args.update(kind='bull_shield' if direction=='CE' else 'bear_shield',direction=direction,
                    snap=snap,candidate_id=None)
        q=args['chain'].quotes['T'];q.opt_type=direction;q.delta=.5 if direction=='CE' else -.5
        patch.object(f.pkg.strike_selector,'rank',return_value=[SimpleNamespace(rejected=False,quote=q)],create=True).start()
        m=importlib.import_module(f.PKG+'.shield_confirmation')
        self.current=m.ShieldEvidence(**(evidence(direction).__dict__|dict(snap=snap,spot=10000)))
        args['shield_context']=lambda:self.current
        return args

    def test_65_shield_reaches_broker_once(self):
        args=self.args()
        result=self.ex.evaluate_and_enter(**args)
        self.assertTrue(result['ok'],result)
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.kwargs['signal_id'],40)
        self.ex.evaluate_and_enter(**args)
        self.place.assert_called_once()

    def test_65_bear_reaches_broker(self):
        args=self.args('PE')
        result=self.ex.evaluate_and_enter(**args)
        self.assertTrue(result['ok'],result)
        self.place.assert_called_once()

    def test_missing_confirmation_never_reaches_broker(self):
        args=self.args();self.current=replace(self.current,oi_confirmed=False)
        self.assertFalse(self.ex.evaluate_and_enter(**args)['ok'])
        self.place.assert_not_called()

    def test_gate_rechecked_immediately_before_submission(self):
        args=self.args()
        def quote(*a):
            self.current=replace(self.current,invalidated=True)
            return {'T':100}
        with patch.object(f.executor,'_poll_ltp',side_effect=quote):
            result=self.ex.evaluate_and_enter(**args)
        self.assertEqual(result['reason'],'shield_invalidated_before_submission')
        self.place.assert_not_called()
