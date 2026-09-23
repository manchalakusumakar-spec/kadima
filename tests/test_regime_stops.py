"""Synthetic offline regressions; no market-performance or fill claims."""
import copy
import importlib
import json
import random
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

PKG = '_regime_stop_test'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'kadima')]
sys.modules[PKG] = pkg
audit = types.ModuleType(PKG + '.audit')
audit.log = Mock()
sys.modules[audit.__name__] = audit
config = importlib.import_module(PKG + '.config')
stops = importlib.import_module(PKG + '.regime_stops')
IST = ZoneInfo('Asia/Kolkata')
NOW = datetime(2026, 9, 23, 10, tzinfo=IST)


class RegimeStops(unittest.TestCase):
    def setUp(self):
        self.signal = dict(source='bull_shield', direction='CE', score=95,
            risk_budget_inr=1000, available_capital_inr=100000, max_lots=10, costs_per_unit=.50)
        self.row = dict(entry=100., bid=99.5, ask=100.5, lot_size=10,
                        token='1', exchange='NFO', delta=.5, gamma=.001, vega=1., ts=NOW)
        self.market = dict(now=NOW, ts=NOW, expiry='2026-09-29', spot=1000.,
            atr_spot_5m=10., atr_premium=2., premium_bar_seconds=300,
            vwap=980., ema20=975., swing=985., oi_wall=980., expected_iv_crush_points=1.,
            hard_exit=NOW.replace(hour=15, minute=15), session_close=NOW.replace(hour=15, minute=30),
            broker_squareoff=NOW.replace(hour=15, minute=20))
        self.regimes = copy.deepcopy(config.STOP_REGIMES)
        self.regimes['CAS'].update(max_trades_per_day=2, risk_budget_inr=2000)

    def plan(self, regime='INTRADAY', **changes):
        self.signal['source'] = {'INTRADAY':'bull_shield','SCALP':'scalp',
                                  'GAMMA':'gamma_blast','CAS':'cas_blast'}[regime]
        self.signal.update(changes)
        self.market['premium_bar_seconds'] = self.regimes[regime]['bar_seconds']
        if regime == 'CAS':
            self.market.update(now=NOW.replace(hour=15, minute=15),
                               ts=NOW.replace(hour=15, minute=15), trigger_bar_low=90.)
            self.row['ts'] = self.market['now']
            self.signal['cas_trades_today'] = 0
        result = stops.initial_stop(self.signal, self.row, self.market, regimes=self.regimes)
        self.assertIsInstance(result, stops.StopPlan, result)
        return result

    def state(self, regime='INTRADAY', **changes):
        return stops.open_position(self.plan(regime, **changes), 1)

    def tick(self, state, price, sec=1, **extra):
        now = datetime.fromisoformat(state.plan.entry_ts) + timedelta(seconds=sec)
        market = dict(self.market, now=now, ts=now,
                      premium_bar_seconds=state.plan.params['bar_seconds'])
        market.update(extra)
        tick = dict(ts=now, token=state.plan.token, exchange=state.plan.exchange,
                    ltp=price, bid=price-.5, ask=price+.5)
        return stops.update_stop(state, tick, market)

    def thesis(self, state, sec=300, **extra):
        now = datetime.fromisoformat(state.plan.entry_ts) + timedelta(seconds=sec)
        signals = dict(now=now, ts=now, bar=dict(closed=True, seconds=state.plan.params['bar_seconds'], ts=now))
        signals.update(extra)
        return stops.thesis_check(state, signals)

    def test_signed_greeks_symmetric_ce_pe(self):
        ce = self.plan()
        self.signal['direction'] = 'PE'
        self.row['delta'] = -.5
        self.market.update(vwap=1020., ema20=1025., swing=1015.)
        pe = self.plan(direction='PE')
        self.assertAlmostEqual(ce.initial_stop, pe.initial_stop)
        self.assertLess(ce.invalidation_spot, 1000)
        self.assertGreater(pe.invalidation_spot, 1000)

    def test_noise_above_ceiling_rejects_without_changing_strike(self):
        self.market['atr_premium'] = 25
        before = copy.deepcopy(self.row)
        r = stops.initial_stop(self.signal, self.row, self.market)
        self.assertEqual(r.reason, 'stop_noise_floor_above_ceiling')
        self.assertEqual(self.row, before)

    def test_three_spreads_is_a_floor(self):
        self.row.update(bid=97., ask=103.)
        p = self.plan()
        self.assertGreaterEqual(p.r_value, 18.)

    def test_sizing_cannot_widen_stop_or_change_contract(self):
        self.signal['risk_budget_inr'] = .01
        r = stops.initial_stop(self.signal, self.row, self.market)
        self.assertEqual(r.reason, 'risk_budget_stop_too_wide')
        self.assertEqual(self.row['token'], '1')

    def test_stale_negative_nan_and_missing_data_rejected(self):
        for field, bad in [('delta', float('nan')), ('gamma', -.1), ('vega', -2), ('entry', float('inf'))]:
            row = dict(self.row, **{field:bad})
            self.assertIsInstance(stops.initial_stop(self.signal, row, self.market), stops.Reject)
        self.row['ts'] = NOW - timedelta(seconds=6)
        self.assertEqual(stops.initial_stop(self.signal, self.row, self.market).reason, 'stale_stop_inputs')
        self.assertIsInstance(stops.initial_stop({}, {}, {}), stops.Reject)

    def test_manual_regime_is_never_guessed(self):
        self.signal['source'] = 'MANUAL_ADOPTED'
        self.assertEqual(stops.initial_stop(self.signal, self.row, self.market).reason,
                         'explicit_supported_signal_regime_required')

    def test_preexpiry_gamma_conflict_blocks_plan(self):
        self.signal.update(source='gamma_blast', score=80)
        self.market['expiry_minus_one'] = True
        self.assertEqual(stops.initial_stop(self.signal, self.row, self.market).reason,
                         'unresolved_preexpiry_gamma_regime')

    def test_macro_blocks_entry(self):
        self.market['macro_event'] = True
        self.assertEqual(stops.initial_stop(self.signal, self.row, self.market).reason, 'macro_event_entry_suppressed')

    def test_intraday_be_trail_lock(self):
        s = self.state()
        self.tick(s, 100+s.r_value)
        self.assertEqual(s.phase, 'BREAKEVEN')
        self.assertAlmostEqual(s.active_stop, 100.5)
        self.tick(s, 100+2*s.r_value, 2)
        self.assertEqual(s.phase, 'LOCK')
        self.assertGreaterEqual(s.active_stop, 100+2*s.r_value*.65)

    def test_intraday_small_dip_does_not_exit_by_raw_vwap(self):
        s = self.state()
        r = self.tick(s, 98.28, vwap=1005, spot=1000)
        self.assertEqual(r.action, 'HOLD')
        self.assertEqual(self.thesis(s, vwap_ema_adverse=True).action, 'HOLD')
        self.assertEqual(self.thesis(s, 600, vwap_ema_adverse=True).action, 'EXIT')

    def test_duplicate_bar_and_gap_do_not_confirm(self):
        s = self.state()
        self.thesis(s, vwap_ema_adverse=True)
        self.assertEqual(self.thesis(s, vwap_ema_adverse=True).action, 'HOLD')
        self.assertEqual(self.thesis(s, 900, vwap_ema_adverse=True).action, 'HOLD')

    def test_no_incomplete_or_stale_thesis_exit(self):
        s = self.state()
        self.assertEqual(self.thesis(s, shield_score=0, bar=dict(closed=False)).action, 'HOLD')
        self.assertEqual(self.thesis(s, shield_score=0, ts=NOW).action, 'HOLD')

    def test_intraday_timeout_and_expiry_halving(self):
        s = self.state()
        self.assertEqual(self.tick(s, 100., 1800).reason, 'intraday_initial_timeout')
        self.market['expiry'] = '2026-09-23'
        s = self.state()
        self.assertEqual(self.tick(s, 100., 900).reason, 'intraday_initial_timeout')

    def test_expiry_from_date_not_weekday(self):
        s = self.state()
        self.assertFalse(s.plan.session['expiry_day'])
        self.market['expiry'] = '2026-09-23'
        self.assertTrue(self.state().plan.session['expiry_day'])

    def test_macro_tightens_existing_trade(self):
        s = self.state()
        self.tick(s, 100+.6*s.r_value, macro_event=True)
        self.assertGreaterEqual(s.active_stop, 100.5)

    def test_scalp_starts_8pct_and_rejects_above_10pct(self):
        s = self.state('SCALP')
        self.assertEqual(s.active_stop, 92.)
        self.market['atr_premium'] = 9
        self.assertEqual(stops.initial_stop(self.signal, self.row, self.market).reason, 'stop_noise_floor_above_ceiling')

    def test_scalp_targets_and_whole_lot_partial(self):
        s = self.state('SCALP', max_lots=2)
        r = self.tick(s, 112.1)
        self.assertEqual((r.action, r.quantity), ('PARTIAL', 10))
        self.assertGreaterEqual(s.active_stop, 106.)
        stops.acknowledge_fill(s, 10, terminal=True)
        self.assertEqual(s.remaining_qty, 10)
        r = self.tick(s, 120.1, 2, momentum_score=80)
        self.assertEqual(r.action, 'EXIT')

    def test_single_lot_partial_is_explicit_full_exit(self):
        s = self.state('SCALP', max_lots=1)
        r = self.tick(s, 112.1)
        self.assertEqual((r.action, r.quantity), ('EXIT', 10))

    def test_scalp_no_favourable_move_timeout(self):
        s = self.state('SCALP')
        self.assertEqual(self.tick(s, 99., 120).reason, 'scalp_no_favourable_move')

    def test_scalp_spread_needs_two_distinct_ticks(self):
        s = self.state('SCALP')
        now = NOW+timedelta(seconds=1)
        tick = dict(ts=now,token='1',exchange='NFO',ltp=100,bid=98,ask=102)
        market = dict(self.market, now=now, ts=now)
        self.assertEqual(stops.update_stop(s,tick,market).action,'HOLD')
        self.assertEqual(stops.update_stop(s,tick,market).action,'HOLD')
        tick['ts'] = now+timedelta(seconds=1)
        market.update(now=tick['ts'],ts=tick['ts'])
        self.assertEqual(stops.update_stop(s,tick,market).reason,'scalp_spread_widened')

    def test_gamma_locks_interpolate_and_cap_lots(self):
        s = self.state('GAMMA')
        self.assertEqual(s.plan.allowed_lots, 2)
        self.tick(s, 150., atr_premium=20)
        self.assertEqual(s.active_stop, 100.)
        self.tick(s, 175., 2, atr_premium=20)
        self.assertAlmostEqual(s.active_stop, 115.)
        self.tick(s, 250., 3, atr_premium=40)
        self.assertAlmostEqual(s.active_stop, 155.)
        self.tick(s, 400., 4, atr_premium=50)
        self.assertAlmostEqual(s.active_stop, 288.)

    def test_gamma_preentry_fast_exit_and_upgrade_never_lowers_stop(self):
        s = self.state('GAMMA', source='gamma_pre_entry')
        self.assertEqual(self.tick(s,100.,120).reason,'gamma_preentry_not_confirmed')
        s = self.state('GAMMA', source='gamma_pre_entry')
        before = s.active_stop
        self.tick(s,101.,60,gamma_confirmed=True)
        self.assertTrue(s.graduated)
        self.assertGreaterEqual(s.active_stop,before)
        self.assertNotEqual(self.tick(s,102.,121).action,'EXIT')

    def test_gamma_thesis_full_dominates_partial(self):
        s = self.state('GAMMA')
        r = self.thesis(s,60,wall_oi_growth=.1,wall_window_seconds=600,
                        opposing_wall_ratio=1.5,opposing_wall_distance=20)
        self.assertEqual((r.action,r.reason),('EXIT','gamma_wall_restrengthened'))

    def test_gamma_oi_distinct_polls_and_iv_crush(self):
        s = self.state('GAMMA')
        self.thesis(s,1,oi_velocity_flipped=True)
        self.assertEqual(self.thesis(s,1,oi_velocity_flipped=True).action,'HOLD')
        self.assertEqual(self.thesis(s,2,oi_velocity_flipped=True).action,'PARTIAL')
        s = self.state('GAMMA')
        self.thesis(s,1,iv_points=20.)
        self.assertEqual(self.thesis(s,2,iv_points=17.).reason,'gamma_iv_crush')

    def test_cas_unresolved_budget_blocks(self):
        self.plan('CAS')
        r = stops.initial_stop(self.signal,self.row,self.market)
        self.assertEqual(r.reason,'unresolved_cas_risk_sleeve')

    def test_cas_full_premium_risk_not_technical_stop_sizing(self):
        s = self.state('CAS')
        self.assertEqual(s.plan.allowed_lots,2)
        self.assertEqual(s.active_stop,90.)

    def test_cas_initial_stop_needs_closed_bar(self):
        s = self.state('CAS')
        self.assertEqual(self.tick(s,89.,1).action,'HOLD')
        ts = datetime.fromisoformat(s.plan.entry_ts)+timedelta(seconds=15)
        r = self.tick(s,89.,15,premium_bar=dict(ts=ts,seconds=15,close=89.,closed=True))
        self.assertEqual(r.reason,'cas_trigger_bar_low_broken')

    def test_cas_ignores_general_1515_but_exits_151830(self):
        s = self.state('CAS')
        self.assertEqual(self.tick(s,101.,1).action,'HOLD')
        now = datetime.fromisoformat(s.plan.entry_ts).replace(minute=18,second=30)
        self.assertEqual(stops.update_stop(s,{},dict(now=now)).reason,'cas_hard_exit')

    def test_cas_stall_and_free_ride(self):
        s = self.state('CAS')
        self.assertEqual(self.tick(s,100.,60).reason,'cas_no_new_high')
        s = self.state('CAS')
        r = self.tick(s,200.)
        self.assertEqual((r.action,r.quantity),('PARTIAL',10))
        stops.acknowledge_fill(s,10,terminal=True)
        self.tick(s,501.,2)
        self.assertAlmostEqual(s.active_stop,501*.75)

    def test_stale_or_wrong_contract_tick_cannot_exit(self):
        s = self.state()
        r = stops.update_stop(s,dict(ts=NOW,token='other',exchange='NFO',ltp=1),dict(now=NOW))
        self.assertEqual(r.action,'HOLD')

    def test_exit_remains_latched_after_rebound(self):
        s = self.state()
        first = self.tick(s,1)
        self.assertEqual(self.tick(s,200,2),first)

    def test_restarts_preserve_phase_r_peak_stop_and_pending(self):
        s = self.state('SCALP',max_lots=2)
        self.tick(s,112.1)
        c = sqlite3.connect(':memory:')
        stops.save_state(s,c)
        restored = stops.load_state(1,c)
        self.assertEqual(restored,s)
        stops.acknowledge_fill(restored,5,terminal=False)
        stops.acknowledge_fill(restored,5,terminal=False)
        self.assertEqual(restored.remaining_qty,15)
        stops.save_state(restored,c)
        restored=stops.load_state(1,c)
        stops.acknowledge_fill(restored,10,terminal=True)
        self.assertEqual(restored.remaining_qty,10)
        self.assertEqual(self.tick(restored,113,2).quantity,0)

    def test_missing_and_corrupt_state(self):
        s = self.state()
        c = sqlite3.connect(':memory:')
        self.assertIsNone(stops.load_state(1,c))
        r = stops.restore_missing_state(s.plan,1,s.remaining_qty,99.)
        self.assertEqual(r.active_stop,99.)
        stops.save_state(s,c)
        c.execute("UPDATE regime_stop_states SET payload='{}'")
        with self.assertRaises((ValueError,KeyError)):
            stops.load_state(1,c)

    def test_exit_limit_depth_tick_rounding_and_final_reprice(self):
        s = self.state('SCALP')
        quote=dict(token='1',exchange='NFO',ts=NOW,bid=99.98,ask=100.,tick_size=.05)
        self.assertAlmostEqual(stops.exit_limit(s,quote,NOW),99.85)
        self.assertAlmostEqual(stops.exit_limit(s,quote,NOW,3),99.95)
        self.assertIsInstance(stops.exit_limit(s,quote,NOW+timedelta(seconds=6)),stops.Reject)

    def test_config_is_snapshotted_and_cannot_change_open_trade(self):
        s=self.state()
        self.regimes['INTRADAY']['max_sl_pct']=.9
        self.assertEqual(s.plan.params['max_sl_pct'],.3)

    def test_ratchet_property_all_regimes(self):
        for regime in ('INTRADAY','SCALP','GAMMA','CAS'):
            for seed in range(12):
                s=self.state(regime)
                rnd=random.Random(seed)
                price=100.
                for sec in range(1,101):
                    previous=s.active_stop
                    price=max(.01,price+rnd.uniform(-5,8))
                    d=self.tick(s,price,sec)
                    self.assertGreaterEqual(s.active_stop,previous)
                    if d.action=='EXIT':
                        break
                    if d.action=='PARTIAL':
                        stops.acknowledge_fill(s,d.quantity,terminal=True)


if __name__ == '__main__':
    unittest.main()
