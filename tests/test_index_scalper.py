"""Offline scalper tests: all broker transports are mocks."""
import copy
import importlib
import os
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import test_execution_lifecycle as f
import test_celestial as cf
import test_fast_momentum as ff
from kadima import index_scalper as scalp
from kadima.option_chain import OptionQuote


class Plans(unittest.TestCase):
    def test_two_lots_and_ten_premium_points_for_dynamic_sizes(self):
        for lot in (10, 15, 20, 25, 30, 40, 65, 75, 120):
            row = scalp.plan(100.25, 95, lot)
            self.assertEqual(row['quantity'], 2 * lot)
            self.assertEqual(row['target'], 110.25)
            self.assertEqual(row['gross_target_inr'], 20 * lot)
            self.assertLess(row['estimated_net_target_inr'], row['gross_target_inr'])

    def test_unknown_fractional_or_invalid_size_never_defaults(self):
        for lot in (None, 0, -1, 1.5, '25', True):
            with self.subTest(lot=lot), self.assertRaises(ValueError):
                scalp.plan(100, 95, lot)
        for stop in (None, float('nan'), -1, 100, 101):
            with self.subTest(stop=stop), self.assertRaises(ValueError):
                scalp.plan(100, stop, 10)
        with self.assertRaisesRegex(ValueError, 'cost_reserve'):
            scalp.plan(500, 490, 10)

    def test_entry_chase_cap_uses_absolute_half_point_and_valid_tick(self):
        self.assertEqual(scalp.entry_limit(100.5, 100), 100.5)
        self.assertIsNone(scalp.entry_limit(100.51, 100))
        self.assertEqual(scalp.entry_limit(10.01, 10.01), 10.5)
        for value in (None, 0, -1, float('nan'), float('inf')):
            self.assertIsNone(scalp.entry_limit(value, 100))

    def test_target_uses_actual_average_fill_for_ce_and_pe(self):
        for side in ('CE', 'PE'):
            p = NS(kind=scalp.KIND, entry_price=100.25, remaining_qty=9,
                   entry_ts=ff.NOW, direction=side)
            self.assertIsNone(scalp.exit_reason(p, 110.2, ff.NOW))
            self.assertEqual(scalp.exit_reason(p, 110.25, ff.NOW), 'index_scalp_target_10_points')
            p.entry_price = 101.25
            self.assertIsNone(scalp.exit_reason(p, 110.25, ff.NOW))

    def test_timeout_and_no_target_on_unfilled_or_other_strategies(self):
        p = NS(kind=scalp.KIND, entry_price=100, remaining_qty=10, entry_ts=ff.NOW)
        self.assertIsNone(scalp.exit_reason(p, 101, ff.NOW + timedelta(seconds=899)))
        self.assertEqual(scalp.exit_reason(p, 101, ff.NOW + timedelta(minutes=15)), 'index_scalp_timeout_15min')
        for kind in ('gamma_blast', 'fast_momentum', 'bull_shield', 'MANUAL_ADOPTED'):
            p.kind = kind
            self.assertIsNone(scalp.exit_reason(p, 120, ff.NOW))
        p.kind = scalp.KIND
        p.remaining_qty = 0
        self.assertIsNone(scalp.exit_reason(p, 120, ff.NOW))

    def test_opt_in_is_independent_and_does_not_remove_shared_blockers(self):
        with patch.object(scalp.fast_momentum, 'live_blocks', return_value=[
                'fast_observation_only', 'auto_paused', 'fresh_news_coverage_required']), \
             patch.dict(os.environ, {'KADIMA_INDEX_SCALP_LIVE': '0'}):
            self.assertEqual(scalp.live_blocks('NIFTY', 'CE', ff.NOW, 80),
                             ['index_scalp_observation_only', 'auto_paused', 'fresh_news_coverage_required'])
            with patch.dict(os.environ, {'KADIMA_INDEX_SCALP_LIVE': '1'}):
                self.assertEqual(scalp.live_blocks('NIFTY', 'CE', ff.NOW, 80),
                                 ['auto_paused', 'fresh_news_coverage_required'])
                self.assertIn('index_contract_required', scalp.live_blocks('RELIANCE', 'CE', ff.NOW, 80))


class Detection(unittest.TestCase):
    def setUp(self):
        self.one, self.five = ff.candles()
        self.q = OptionQuote('T', 'TESTCE', 1000, 'CE', ltp=12, bid=11.99, ask=12.01,
                             volume=1000000, oi=1000000, delta=.5, iv=25)
        self.chain = NS(_expiry='22SEP2026', _last_poll=time.monotonic(),
                        _alignment_at=time.monotonic(), atm=1000,
                        index=NS(token='U', opt_exchange='NFO', strike_step=50),
                        quotes={'T': self.q}, walls=lambda: {'CE': [], 'PE': []})
        self.st = NS(name='NIFTY', spot=1002.5, last_updated=ff.NOW, chain=self.chain,
                     snap=NS(vwap=1000, ema_fast=1001, ema_slow=1000, rsi=60))
        patch.object(scalp.storage, 'recent_candles', side_effect=lambda t, i, n:
                     self.one if i == 1 else self.five).start()
        patch.object(scalp.fast_momentum.PremiumHistory, 'metrics',
                     return_value=dict(premium_move_pct=3, volume_ratio=2)).start()
        patch.object(scalp, 'get_lot_size', return_value=25).start()
        patch.object(scalp, 'live_blocks', return_value=['index_scalp_observation_only']).start()

    def tearDown(self):
        patch.stopall()

    def test_each_configured_index_exports_observation_plan_without_quote_mutation(self):
        original = copy.copy(self.q)
        for name in scalp.config.GAMMA_INDICES:
            self.st.name = name
            row = scalp.Lane().scan(self.st, ff.NOW)
            self.assertEqual(row['stage'], 'SETUP_READY', row)
            self.assertEqual(row['quantity'], 50)
            self.assertEqual(row['target'], 22.01)
            self.assertFalse(row['eligible'])
        self.assertEqual(self.q, original)

    def test_otm_wrong_alignment_expiry_stale_and_missing_size_block(self):
        for case in ('otm', 'crossed', 'alignment', 'stale', 'expiry', 'size', 'cutoff'):
            with self.subTest(case=case):
                st = copy.deepcopy(self.st)
                now = ff.NOW
                if case == 'otm': st.chain.quotes['T'].strike = 1050
                if case == 'crossed': st.chain.quotes['T'].bid = 13
                if case == 'alignment': st.snap.rsi = 40
                if case == 'stale': st.chain._last_poll -= 31
                if case == 'expiry': st.chain._expiry = '16SEP2026'
                if case == 'cutoff': now = now.replace(hour=15, minute=20)
                with patch.object(scalp, 'get_lot_size', return_value=None if case == 'size' else 25):
                    row = scalp.Lane().scan(st, now)
                self.assertEqual(row['stage'], 'BLOCKED', row)
                self.assertFalse(row['eligible'])


class Dispatch(unittest.TestCase):
    setUp = cf.Celestial.setUp
    tearDown = cf.Celestial.tearDown
    entry_fixture = cf.Celestial.entry_fixture
    arm = cf.Celestial.arm

    def args(self):
        args = self.entry_fixture()
        self.sc = f.executor.index_scalper
        self.row = dict(strategy=self.sc.KIND, eligible=True, ts=f.NOW.isoformat(),
                        underlying='NIFTY', direction='CE', token='T', symbol='TESTCE',
                        exchange='NFO', spot_stop=9900, days=2, episode='scalp:one', entry=100)
        args.update(kind=self.sc.KIND, tier='EXPERIMENTAL', score=80,
                    fast_context=lambda: self.row, candidate_id=None)
        self.gates = patch.object(self.sc, 'live_blocks', return_value=[]).start()
        self.budget = patch.object(f.pkg.risk_budget, 'select_within_budget',
            side_effect=lambda rows, *a, **kw: NS(candidate=rows[0], lots=2)).start()
        patch.object(f.capital, 'enforce', return_value=(2, 'ok')).start()
        return args

    def test_exact_two_lots_use_ledger_and_no_assumed_or_duplicate_fill(self):
        args = self.args()
        result = self.ex.evaluate_and_enter(**args)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.place.call_args.kwargs['quantity'], 20)
        self.assertEqual(self.budget.call_args.kwargs['max_lots'], 2)
        pos = self.ex.state.positions['40']
        self.assertEqual((pos.kind, pos.entry_price, pos.remaining_qty), (self.sc.KIND, 0, 0))
        self.ex.evaluate_and_enter(**args)
        self.place.assert_called_once()

    def test_budget_and_capital_never_silently_size_down(self):
        args = self.args()
        self.budget.side_effect = lambda rows, *a, **kw: NS(candidate=rows[0], lots=1)
        self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'index_scalp_two_lots_exceed_budget')
        self.budget.side_effect = lambda rows, *a, **kw: NS(candidate=rows[0], lots=2)
        with patch.object(f.capital, 'enforce', return_value=(1, 'sized_down')):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'index_scalp_two_lots_exceed_capital')
        with patch.object(f.executor.trade_policy, 'lots_within_allocation', return_value=1):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'index_scalp_two_lots_exceed_fresh_budget')
        self.place.assert_not_called()

    def test_entry_quote_and_context_revalidated(self):
        args = self.args()
        with patch.object(f.executor, '_poll_ltp', return_value={'T': 100.55}):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'index_scalp_entry_price_ran_away')
        def changed(*a):
            self.row['episode'] = 'changed'
            return {'T': 100}
        # Return separate snapshots so an in-place update cannot alter both versions.
        args['fast_context'] = lambda: dict(self.row)
        with patch.object(f.executor, '_poll_ltp', side_effect=changed):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'fast_invalidated_before_submission')
        self.place.assert_not_called()

    def test_observation_memory_reconciliation_and_shared_slot_block(self):
        args = self.args()
        self.gates.return_value = ['index_scalp_observation_only']
        self.assertFalse(self.ex.evaluate_and_enter(**args)['ok'])
        self.gates.return_value = []
        with patch.object(f.memory_guard, 'entry_block_reason', return_value='memory_restricted'):
            self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'memory_restricted')
        self.ex._reconcile_blocked = True
        self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'startup_reconcile_not_complete')
        self.ex._reconcile_blocked = False
        self.ex.state.positions['1'] = self.pos
        self.assertEqual(self.ex.evaluate_and_enter(**args)['reason'], 'max_concurrent_positions')
        self.place.assert_not_called()

    def test_signal_identity_persists_and_is_separate_from_fast(self):
        self.args()
        row = dict(self.row, ts=f.NOW.isoformat())
        sid = self.sc.signal_id(row)
        self.assertEqual(sid, self.sc.signal_id(row))
        self.assertNotEqual(sid, f.executor.fast_momentum.signal_id(row))
        self.assertEqual(f.storage.conn().execute('SELECT kind FROM signals WHERE id=?', (sid,)).fetchone()[0], self.sc.KIND)

    def test_partial_buy_target_cancels_then_exits_only_confirmed_quantity(self):
        args = self.args()
        self.ex.evaluate_and_enter(**args)
        pos = self.ex.state.positions['40']
        self.book.return_value = [f.orders.OrderStatus('O1', 'open', 7, 100.25, {})]
        self.ex.refresh_orders()
        self.ex._celestial_verified.add('40')
        with patch.object(f.executor, '_poll_ltp', return_value={'T': 110.25}):
            self.ex.check_exits()
        self.assertEqual(pos.exit_reason, 'index_scalp_target_10_points')
        self.assertEqual(self.place.call_count, 1)
        self.cancel.assert_called()
        self.book.return_value = [f.orders.OrderStatus('O1', 'cancelled', 7, 100.25, {})]
        self.ex.refresh_orders()
        self.ex.check_exits()  # exit remains binding after price rebounds down
        self.assertEqual(self.place.call_args.kwargs['transactiontype'], 'SELL')
        self.assertEqual(self.place.call_args.kwargs['quantity'], 7)
        self.assertEqual(pos.remaining_qty, 7)  # acknowledgement is not a fill
        self.ex.check_exits()
        self.assertEqual(self.place.call_count, 2)

    def test_restored_scalp_keeps_target_when_feature_disabled(self):
        self.args()
        self.pos.kind = self.sc.KIND
        self.pos.signal_id = 401
        self.pos.entry_price = 100.25
        self.pos.structural_stop = self.pos.effective_stop = 90
        self.pos.entry_ts = f.NOW
        self.pos.status = 'open'
        f.storage.upsert_position(self.pos)
        data = next(r for r in f.storage.load_open_positions() if r['signal_id'] == 401)
        for key in ('entry_ts', 'exit_ts'):
            data[key] = f.datetime.fromisoformat(data[key]) if data.get(key) else None
        restored = f.executor.Position(**data)
        self.ex.state.positions = {'401': restored}
        self.ex._celestial_verified = {'401'}
        with patch.dict(os.environ, {'KADIMA_INDEX_SCALP_LIVE': '0'}), \
             patch.object(f.executor, '_poll_ltp', return_value={'T': 110.25}):
            self.ex.check_exits()
        self.assertEqual(restored.exit_reason, 'index_scalp_target_10_points')
        self.assertEqual(self.place.call_args.kwargs['quantity'], 10)

    def test_stop_still_exits_before_target(self):
        self.args()
        self.pos.kind = self.sc.KIND
        self.pos.status = 'open'
        self.pos.structural_stop = self.pos.effective_stop = 95
        self.ex.state.positions = {'1': self.pos}
        self.ex._celestial_verified = {'1'}
        with patch.object(f.executor, '_poll_ltp', return_value={'T': 94}):
            self.ex.check_exits()
        self.assertEqual(self.pos.exit_reason, 'structural_stop_breached')

    def test_daily_loss_overrides_target(self):
        self.args()
        self.pos.kind = self.sc.KIND
        self.pos.status = 'open'
        self.pos.structural_stop = self.pos.effective_stop = 95
        self.ex.state.positions = {'1': self.pos}
        self.ex._celestial_verified = {'1'}
        with patch.object(f.executor, '_poll_ltp', return_value={'T': 115}), \
             patch.object(f.exit_manager, 'realized_pnl_today', return_value=-10000):
            self.ex.check_exits()
        self.assertEqual(self.pos.exit_reason, 'daily_loss_limit_breached')


class EngineIsolation(unittest.TestCase):
    def test_observation_and_failure_never_dispatch(self):
        from test_shield_confirmation import load_engine
        module = load_engine()
        engine = module.Engine.__new__(module.Engine)
        engine.scalp_lane = NS(scan=Mock(return_value=dict(eligible=False, stage='SETUP_READY',
                               blocked_reasons=['index_scalp_observation_only'], underlying='NIFTY')))
        engine._scalp_reported = {}
        executor = NS(scalp_rows={}, evaluate_and_enter=Mock())
        with patch.object(module.auto_executor, 'EXECUTOR', executor), patch.object(module.audit, 'log'):
            engine._scan_scalp(NS(name='NIFTY'))
            executor.evaluate_and_enter.assert_not_called()
            engine.scalp_lane.scan.side_effect = RuntimeError('fixture')
            engine._scan_scalp(NS(name='NIFTY'))
            self.assertEqual(executor.scalp_rows['NIFTY']['blocked_reasons'], ['index_scalp_lane_error'])
            executor.evaluate_and_enter.assert_not_called()


if __name__ == '__main__':
    unittest.main()
