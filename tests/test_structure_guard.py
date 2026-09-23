"""Synthetic path tests. Validation registry entries below are test doubles.

They do not constitute an approved model, a market backtest or a fill claim.
"""
import copy
import importlib
import importlib.util
import json
import sqlite3
import sys
import types
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

PKG = '_structure_guard_test'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'kadima')]
sys.modules[PKG] = pkg
audit = types.ModuleType(PKG + '.audit')
audit.log = Mock()
sys.modules[audit.__name__] = audit
config = importlib.import_module(PKG + '.config')
stops = importlib.import_module(PKG + '.regime_stops')
guard = importlib.import_module(PKG + '.structure_guard')
NOW = datetime(2026, 9, 23, 10, tzinfo=ZoneInfo('Asia/Kolkata'))


class StructureGuard(unittest.TestCase):
    def setUp(self):
        self.validation = dict(status='approved', report_id='SYNTHETIC-TEST-ONLY',
            model_id='fixture', event=guard.EVENT, asset='NIFTY', regime='INTRADAY',
            max_horizon_seconds=60, valid_from=NOW-timedelta(days=1),
            valid_until=NOW+timedelta(days=1))
        self.registry = patch.object(config, 'STRUCTURE_GUARD_VALIDATIONS', {'test': self.validation})
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.signal = dict(source='bull_shield', direction='CE', score=95, asset='NIFTY',
            risk_budget_inr=1000, available_capital_inr=100000, max_lots=10,
            costs_per_unit=.5, hard_stop_premium=70.)
        self.row = dict(entry=100., bid=99.9, ask=100.1, lot_size=10,
            token='1', exchange='NFO', delta=.5, gamma=0., vega=1., ts=NOW)
        self.market = dict(now=NOW, ts=NOW, asset='NIFTY', expiry='2026-09-29', spot=23340.,
            atr_spot_5m=40., atr_premium=2., premium_bar_seconds=300,
            vwap=23320., ema20=23315., expected_iv_crush_points=0.,
            hard_exit=NOW.replace(hour=15, minute=15))

    def state(self, pe=False, reserve=True):
        if pe:
            self.signal['direction'] = 'PE'
            self.row['delta'] = -.5
            self.market.update(spot=23260., vwap=23280., ema20=23285.)
        if not reserve:
            self.signal.pop('hard_stop_premium', None)
        plan = stops.initial_stop(self.signal, self.row, self.market)
        self.assertIsInstance(plan, stops.StopPlan, plan)
        self.assertAlmostEqual(plan.initial_stop, 82.5)
        return stops.open_position(plan, 7)

    def inputs(self, state, price, sec, spot=None, confidence=65., deadline=None):
        now = NOW + timedelta(seconds=sec)
        tick = dict(token='1', exchange='NFO', ts=now, ltp=price, bid=price-.1, ask=price+.1)
        pe = state.plan.direction == 'PE'
        market = dict(self.market, now=now, ts=now,
            spot=(23260. if pe else 23340.) if spot is None else spot,
            structure=dict(id='pivot-23300', asset='NIFTY', role='resistance' if pe else 'support',
                low=23300., high=23300., ts=now, bar_seconds=300,
                reaction_at=NOW, confirmed_reactions=1, references=['pivot']),
            option_risk=dict(token='1', exchange='NFO', ts=now, delta=-.5 if pe else .5,
                gamma=0., vega=1., iv_down_points=0., theta_per_second=0.))
        g = state.structure_guard
        if g:
            end = deadline or g.get('deadline') or now+timedelta(seconds=60)
            market['recovery_forecast'] = dict(event=guard.EVENT, validation_id='test', model_id='fixture',
                asset='NIFTY', regime='INTRADAY', direction=state.plan.direction,
                token='1', exchange='NFO', signal_id=7, level_id=g['level_id'],
                boundary_spot=g['boundary_spot'], technical_stop_premium=g['technical_stop_premium'],
                hard_floor_premium=guard.hard_floor(state), confidence_pct=confidence,
                generated_at=now, market_data_at=now, outcome_deadline=end,
                recovery_target_premium=90., mean_success_premium=95., mean_failure_premium=70.,
                remaining_costs_per_unit=.5)
        return tick, market

    def send(self, s, price, sec, **kw):
        t, m = self.inputs(s, price, sec, **kw)
        return stops.update_stop(s, t, m)

    def arm(self, **kw):
        s = self.state(**kw)
        self.send(s, 100., 1)
        self.assertEqual(s.structure_guard.get('status'), 'ARMED')
        return s

    def activate(self, s=None, **kw):
        s = s or self.arm()
        spot = 23296. if s.plan.direction == 'PE' else 23304.
        result = self.send(s, 82.4, 2, spot=spot, **kw)
        self.assertEqual((result.action, result.reason), ('HOLD', 'structure_grace_active'))
        return s

    def test_ce_support_and_pe_resistance_are_mirrored(self):
        for pe, stop, boundary in ((False, 23305., 23288.), (True, 23295., 23312.)):
            with self.subTest(pe=pe):
                s = self.arm(pe=pe)
                self.assertAlmostEqual(s.structure_guard['technical_stop_spot'], stop)
                self.assertAlmostEqual(s.structure_guard['boundary_spot'], boundary)
                self.assertAlmostEqual(s.structure_guard['projected_boundary_premium'], 74.)
                self.activate(s)

    def test_exact_65_qualifies_lower_and_invalid_values_do_not(self):
        self.activate(confidence=65.)
        for confidence in (64.999, .65, -1, 101, float('nan'), True):
            with self.subTest(confidence=confidence):
                s = self.arm()
                self.assertEqual(self.send(s, 82.4, 2, spot=23304, confidence=confidence).action, 'EXIT')

    def test_confidence_does_not_replace_expected_value(self):
        s = self.arm()
        t, m = self.inputs(s, 82.4, 2, spot=23304)
        m['recovery_forecast'].update(mean_success_premium=90., mean_failure_premium=50.)
        self.assertEqual(stops.update_stop(s,t,m).action, 'EXIT')

    def test_wider_hard_stop_is_reserved_in_entry_size(self):
        s = self.state()
        self.assertEqual(s.plan.allowed_lots, 3)
        self.assertLessEqual((100-70+.5)*s.remaining_qty, self.signal['risk_budget_inr'])
        self.assertGreater(self.state(reserve=False).plan.allowed_lots, 3)

    def test_reserve_outside_percentage_cap_or_rupee_budget_is_rejected(self):
        self.signal['hard_stop_premium'] = 69.99
        self.assertEqual(stops.initial_stop(self.signal,self.row,self.market).reason,
                         'hard_stop_outside_regime_limits')
        self.signal.update(hard_stop_premium=70., risk_budget_inr=300.)
        self.assertEqual(stops.initial_stop(self.signal,self.row,self.market).reason,
                         'risk_budget_stop_too_wide')

    def test_without_reserved_room_initial_stop_stays_binding(self):
        s = self.state(reserve=False)
        self.send(s,100.,1)
        self.assertEqual(s.structure_guard, {})
        self.assertEqual(self.send(s,82.4,2,spot=23304).reason, 'hard_risk_floor_breached')

    def test_no_zone_selection_at_or_after_stop_touch(self):
        s = self.state()
        self.assertEqual(self.send(s,82.4,1,spot=23304).action, 'EXIT')
        self.assertNotEqual(s.structure_guard.get('status'), 'ACTIVE')

    def test_rebound_consumes_permission_and_second_touch_exits(self):
        s = self.activate()
        self.assertEqual(self.send(s,83.,3,spot=23306).reason, 'structure_recovered')
        self.assertEqual(s.structure_guard['status'], 'SPENT')
        self.send(s,100.,4)
        self.assertEqual(self.send(s,82.4,5,spot=23304).action,'EXIT')

    def test_original_nominal_stop_never_moves_down(self):
        s = self.arm()
        original = s.active_stop
        self.activate(s)
        self.send(s,81.,3,spot=23301)
        self.assertEqual(s.active_stop, original)

    def test_timeout_exits_without_a_quote_and_survives_rebound(self):
        s = self.activate()
        deadline = datetime.fromisoformat(s.structure_guard['deadline'])
        first = stops.update_stop(s,{},dict(now=deadline))
        self.assertEqual(first.reason,'structure_grace_expired')
        self.assertEqual(self.send(s,100.,63),first)

    def test_new_forecast_cannot_extend_deadline_or_change_target(self):
        for field, value in (('outcome_deadline', NOW+timedelta(seconds=63)),
                             ('recovery_target_premium', 89.)):
            with self.subTest(field=field):
                s = self.activate()
                t,m = self.inputs(s,81.,3,spot=23301)
                m['recovery_forecast'][field] = value
                self.assertEqual(stops.update_stop(s,t,m).action,'EXIT')

    def test_closed_bar_beyond_boundary_exits_for_both_directions(self):
        for pe, close in ((False,23287.),(True,23313.)):
            with self.subTest(pe=pe):
                s = self.activate(self.arm(pe=pe))
                t,m = self.inputs(s,75.,10,spot=close)
                m['spot_bar'] = dict(asset='NIFTY',ts=NOW+timedelta(seconds=10),
                                    close=close,closed=True,seconds=60)
                self.assertEqual(stops.update_stop(s,t,m).reason,'structure_buffer_breached')

    def test_wick_incomplete_old_and_wrong_asset_bars_do_not_confirm(self):
        for patch_bar in ({'closed':False},{'ts':NOW},{'asset':'BANKNIFTY'},{'seconds':300}):
            with self.subTest(patch_bar=patch_bar):
                s = self.activate()
                t,m = self.inputs(s,75.,10,spot=23287)
                m['spot_bar'] = dict(asset='NIFTY',ts=NOW+timedelta(seconds=10),
                                    close=23287.,closed=True,seconds=60)
                m['spot_bar'].update(patch_bar)
                self.assertEqual(stops.update_stop(s,t,m).reason,'structure_grace_active')

    def test_confirmed_break_cannot_be_erased_by_rebound_or_missing_option_tick(self):
        for missing_tick in (False,True):
            with self.subTest(missing_tick=missing_tick):
                s = self.activate()
                t,m = self.inputs(s,90.,10,spot=23320)
                m['spot_bar'] = dict(asset='NIFTY',ts=NOW+timedelta(seconds=9),
                                    close=23287.,closed=True,seconds=60)
                r = stops.update_stop(s,{} if missing_tick else t,m)
                self.assertEqual(r.reason,'structure_buffer_breached')

    def test_hard_floor_and_profit_lock_override_forecast(self):
        s = self.activate()
        self.assertEqual(self.send(s,70.1,3,spot=23280).reason,'hard_risk_floor_breached')
        s = self.activate()
        stops._ratchet(s,83.,'LOCK')
        self.assertEqual(self.send(s,82.9,3,spot=23304).reason,'hard_risk_floor_breached')

    def test_bad_forecast_cannot_self_approve_or_reuse_other_context(self):
        cases = [dict(validation_id='unknown',calibrated=True,validated=True),
                 dict(signal_id=8),dict(token='2'),dict(exchange='BFO'),dict(direction='PE'),
                 dict(regime='GAMMA'),dict(asset='BANKNIFTY'),dict(event='bullish_direction'),
                 dict(boundary_spot=23200),dict(hard_floor_premium=60),
                 dict(model_id='another'),dict(validation_id=['bad']),
                 dict(generated_at=NOW+timedelta(seconds=100)),
                 dict(market_data_at=NOW-timedelta(seconds=100)),dict(remaining_costs_per_unit=0)]
        for changes in cases:
            with self.subTest(changes=changes):
                s = self.arm()
                t,m = self.inputs(s,82.4,2,spot=23304)
                m['recovery_forecast'].update(changes)
                self.assertEqual(stops.update_stop(s,t,m).action,'EXIT')

    def test_active_forecast_loss_restores_exit_and_missing_tick_watchdog(self):
        for missing_tick in (False,True):
            with self.subTest(missing_tick=missing_tick):
                s = self.activate()
                t,m = self.inputs(s,82.4,3,spot=23304,confidence=64.)
                self.assertEqual(stops.update_stop(s,{} if missing_tick else t,m).action,'EXIT')
        s = self.activate()
        self.validation['valid_until'] = NOW
        self.assertEqual(self.send(s,82.4,3,spot=23304).action,'EXIT')

    def test_stale_forecast_and_missing_depth_or_spot_end_exception(self):
        for case in ('forecast','depth','spot','macro'):
            with self.subTest(case=case):
                s = self.activate()
                t,m = self.inputs(s,82.4,3,spot=23304)
                if case == 'forecast': m.pop('recovery_forecast')
                if case == 'depth': t.pop('bid')
                if case == 'spot': m['ts'] = NOW-timedelta(seconds=10)
                if case == 'macro': m['macro_event'] = True
                self.assertEqual(stops.update_stop(s,t,m).action,'EXIT')

    def test_structure_needs_confluence_and_fresh_signed_greeks(self):
        cases = [('structure',dict(confirmed_reactions=0)),('structure',dict(references=[])),
                 ('structure',dict(references=['oi_wall'],oi_valid=False)),
                 ('structure',dict(role='resistance')),('structure',dict(low=23200,high=23200)),
                 ('structure',dict(ts=NOW-timedelta(seconds=10))),
                 ('option_risk',dict(delta=-.5)),('option_risk',dict(gamma=float('nan'))),
                 ('option_risk',dict(theta_per_second=-1.)),('option_risk',dict(token='2'))]
        for field, changes in cases:
            with self.subTest(field=field,changes=changes):
                s = self.state()
                t,m = self.inputs(s,100.,1)
                m[field].update(changes)
                stops.update_stop(s,t,m)
                self.assertFalse(s.structure_guard)

    def test_operator_registry_defaults_empty(self):
        with patch.object(config,'STRUCTURE_GUARD_VALIDATIONS',{}):
            s = self.state()
            self.send(s,100.,1)
            self.assertFalse(s.structure_guard)
            self.assertEqual(self.send(s,82.4,2,spot=23304).action,'EXIT')

    def test_regime_and_asset_scope_cannot_be_changed_by_forecast(self):
        s = self.state()
        s.plan = replace(s.plan,asset='BANKNIFTY')
        self.send(s,100.,1)
        self.assertFalse(s.structure_guard)
        s.plan = replace(s.plan,asset='NIFTY',regime='GAMMA')
        self.assertIsNone(guard.policy(s))

    def test_guard_state_survives_restart_and_preserves_original_deadline(self):
        s = self.activate()
        c = sqlite3.connect(':memory:')
        stops.save_state(s,c)
        restored = stops.load_state(7,c)
        self.assertEqual(s,restored)
        self.assertEqual(self.send(restored,83.,3,spot=23306).reason,'structure_recovered')
        stops.save_state(restored,c)
        again = stops.load_state(7,c)
        self.assertEqual(self.send(again,82.4,4,spot=23304).action,'EXIT')

    def test_missing_legacy_and_corrupt_state_never_reset_allowance(self):
        s = self.activate()
        fallback = stops.restore_missing_state(s.plan,7,s.remaining_qty,s.active_stop)
        self.assertTrue(fallback.structure_guard['used'])
        c = sqlite3.connect(':memory:')
        stops.save_state(s,c)
        payload = dict(version=1,state=asdict(s))
        payload['state'].pop('protected_stop')
        payload['state'].pop('structure_guard')
        payload['state']['plan'].pop('hard_stop_premium')
        c.execute('UPDATE regime_stop_states SET payload=?',(json.dumps(payload),))
        old = stops.load_state(7,c)
        self.assertEqual(old.protected_stop,old.active_stop)
        self.assertTrue(old.structure_guard['used'])
        s.structure_guard['used'] = False
        stops.save_state(s,c)
        with self.assertRaises(ValueError): stops.load_state(7,c)

    def test_pending_partial_is_not_overwritten_by_full_risk_exit(self):
        s = self.activate()
        partial = stops._decision(s,'PARTIAL','fixture_partial',.5)
        stops.acknowledge_fill(s,5,terminal=False)
        result = self.send(s,69.,3,spot=23270)
        self.assertEqual(result.reason,'full_exit_waiting_for_pending_resolution')
        self.assertEqual(s.pending['reason'],'fixture_partial')
        self.assertEqual(s.counters['pending_filled'],5)
        stops.acknowledge_fill(s,5,terminal=True)
        self.assertEqual(s.pending['action'],'EXIT')
        self.assertEqual(s.pending['quantity'],s.remaining_qty)

    def test_normalized_replay_exposes_guard_without_assuming_an_exit_fill(self):
        path = Path(__file__).resolve().parents[1] / 'tools' / 'replay_stops.py'
        spec = importlib.util.spec_from_file_location('_guard_replay_test',path)
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        s = self.state()
        records = [dict(event='entry',signal_id=7,signal=self.signal,chain_row=self.row,market=self.market)]
        t,m = self.inputs(s,100.,1)
        records.append(dict(event='tick',signal_id=7,tick=t,market=m))
        stops.update_stop(s,t,m)
        t,m = self.inputs(s,82.4,2,spot=23304)
        records.append(dict(event='tick',signal_id=7,tick=t,market=m))
        stops.update_stop(s,t,m)
        deadline = s.structure_guard['deadline']
        records.append(dict(event='tick',signal_id=7,tick={},market=dict(now=deadline)))
        # Exercise the same JSON conversion as a recorded JSONL session.
        records = json.loads(json.dumps(records,default=lambda v:v.isoformat()))
        with patch.object(replay,'stops',stops):
            output = list(replay.replay(records))
        self.assertEqual(output[1]['structure_guard']['status'],'ARMED')
        self.assertEqual(output[2]['structure_guard']['status'],'ACTIVE')
        self.assertEqual(output[3]['decision']['reason'],'structure_grace_expired')
        self.assertEqual(output[3]['remaining_qty'],s.remaining_qty)
        self.assertEqual(output[3]['hard_stop_premium'],70.)


if __name__ == '__main__':
    unittest.main()
