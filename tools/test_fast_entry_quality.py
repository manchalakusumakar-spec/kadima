"""Offline regression tests: python -m unittest discover -s tools -p test_fast_entry_quality.py.

Network access is blocked for this suite; no broker orders or credentials needed.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import os
import socket
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp(prefix='kadima-fast-quality-'))
from kadima import config, fast_entry_quality as quality, fast_momentum as fast
from kadima import stock_fast, trade_policy, auto_executor, market_session_gate

NOW = datetime(2026, 10, 6, 9, 45, tzinfo=config.IST)


def fixture(direction='CE'):
    one = [dict(open=99.5, high=101., low=99., close=100., volume=100)
           for _ in range(5)]
    one += [dict(open=100., high=102., low=100., close=101.8, volume=250),
            dict(open=101.8, high=102., low=101., close=101.7, volume=80)]
    five = [dict(open=x-.5, high=x+1, low=x-1, close=x, volume=500)
            for x in (98., 99., 100.)]
    if direction == 'PE':
        for row in one + five:
            row.update(open=200-row['open'], close=200-row['close'],
                       high=200-row['low'], low=200-row['high'])
    for rows, interval in ((one, 1), (five, 5)):
        for i, row in enumerate(rows):
            row.update(start_ts=(NOW-timedelta(minutes=(len(rows)-i)*interval)).isoformat(),
                       closed=True)
    return one, five, one[-1]['close']


def chopped():
    one, five, spot = fixture()
    for row, close in zip(five, (100., 99., 100.1)):
        row.update(open=close-.1, close=close, high=close+1, low=close-1)
    return one, five, spot


class FastQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        cls.network.start()

    @classmethod
    def tearDownClass(cls):
        cls.network.stop()

    def test_clean_ce_and_pe_setup_and_stop_symmetry(self):
        for direction, expected in (('CE', 99.5), ('PE', 100.5)):
            with self.subTest(direction=direction):
                one, five, spot = fixture(direction)
                result = fast.setup(one, five, spot, NOW)
                self.assertEqual(result['direction'], direction)
                self.assertAlmostEqual(result['spot_stop'], expected)
                self.assertEqual(result['entry_quality']['five_minute_efficiency'], 1)
                self.assertEqual(result['entry_quality']['baseline_bars'], 5)

    def test_directionally_positive_but_choppy_context_is_rejected(self):
        one, five, spot = chopped()
        self.assertGreater(five[-1]['close'], five[0]['close'])
        with self.assertRaisesRegex(ValueError, '^fast_choppy_five_minute_context$'):
            fast.setup(one, five, spot, NOW)

    def test_weak_body_and_long_rejection_wick_fail(self):
        for change in ({'open': 101.7}, {'high': 104.}):
            with self.subTest(change=change):
                one, five, spot = fixture()
                one[-2].update(change)
                with self.assertRaisesRegex(ValueError, '^fast_weak_breakout_candle$'):
                    fast.setup(one, five, spot, NOW)

    def test_extension_inside_old_limit_but_outside_new_limit_fails(self):
        one, five, _ = fixture()
        # Baseline range=2; original extension allowance=3; new allowance=2.
        with self.assertRaisesRegex(ValueError, '^fast_entry_overextended$'):
            fast.setup(one, five, 103.2, NOW)
        self.assertEqual(fast.setup(one, five, 103., NOW)['entry_quality']['extension_true_range'], 1)

    def test_absolute_price_scale_does_not_change_filter(self):
        one, five, spot = fixture()
        reference = fast.setup(one, five, spot, NOW)
        for r in one + five:
            for k in ('open', 'high', 'low', 'close'):
                r[k] *= 100
        result = fast.setup(one, five, spot*100, NOW)
        self.assertAlmostEqual(result['spot_stop'], reference['spot_stop']*100)
        self.assertEqual(result['entry_quality']['extension_true_range'],
                         reference['entry_quality']['extension_true_range'])

    def test_baseline_gap_enters_true_range_without_breakout_contamination(self):
        one, five, spot = fixture()
        one[0].update(open=97., high=98., low=96., close=97.)
        result = fast.setup(one, five, spot, NOW)
        self.assertAlmostEqual(result['entry_quality']['baseline_mean_true_range'], 2.4)
        self.assertAlmostEqual(result['entry_quality']['stop_buffer_points'], .6)

    def test_stale_and_missing_closed_bars_remain_blocked(self):
        one, five, spot = fixture()
        with self.assertRaisesRegex(ValueError, '^stale_candles$'):
            fast.setup(one, five, spot, NOW+timedelta(minutes=3))
        one[2]['start_ts'] = one[1]['start_ts']
        with self.assertRaisesRegex(ValueError, '^candle_gap$'):
            fast.setup(one, five, spot, NOW)

    def test_live_or_future_candle_never_passes(self):
        for change in ({'closed': False}, {'start_ts': NOW.isoformat()}):
            one, five, spot = fixture()
            one[-1].update(change)
            with self.assertRaisesRegex(ValueError, '^invalid_or_unclosed_bars$'):
                fast.setup(one, five, spot, NOW)

    def test_insufficient_history_and_failed_hold_still_fail(self):
        one, five, spot = fixture()
        with self.assertRaisesRegex(ValueError, '^candle_warmup$'):
            fast.setup(one[1:], five, spot, NOW)
        one[-1]['close'] = 100.8
        one[-1]['low'] = 100.7
        with self.assertRaisesRegex(ValueError, '^breakout_hold_not_confirmed$'):
            fast.setup(one, five, spot, NOW)

    def test_invalid_inputs_and_policy_fail_closed(self):
        one, five, spot = fixture()
        for invalid in (float('nan'), float('inf'), True, -1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, '^fast_quality_invalid_inputs$'):
                    quality.assess(one, five, invalid, 'CE', 101., 100.)
        for key in ('FAST_MIN_DIRECTIONAL_EFFICIENCY', 'FAST_MIN_BREAKOUT_BODY_FRACTION',
                    'FAST_MIN_BREAKOUT_CLOSE_LOCATION', 'FAST_MAX_EXTENSION_TRUE_RANGE',
                    'FAST_STOP_BUFFER_TRUE_RANGE'):
            with patch.object(config, key, float('nan')):
                with self.assertRaisesRegex(ValueError, '^fast_quality_invalid_policy$'):
                    fast.setup(one, five, spot, NOW)

    def test_inputs_are_not_mutated(self):
        one, five, spot = fixture()
        before = deepcopy((one, five))
        fast.setup(one, five, spot, NOW)
        self.assertEqual((one, five), before)

    def test_stock_and_index_surface_same_chop_blocker(self):
        one, five, spot = chopped()
        chain = NS(index=NS(name='NIFTY', token='index'), _last_poll=100.)
        st = NS(name='NIFTY', chain=chain, spot=spot, last_updated=NOW)
        with patch.object(fast.time, 'monotonic', return_value=100.), \
             patch.object(fast.storage, 'recent_candles', side_effect=[one, five]):
            row = fast.Lane().scan(st, now=NOW, record=False)
        self.assertFalse(row['eligible'])
        self.assertEqual(row['blocked_reasons'], ['fast_choppy_five_minute_context'])
        data = dict(chain_at=NOW.isoformat(), spot_at=NOW.isoformat(), spot=spot)
        cfg = dict(holding_minutes=15, revision=1, live_enabled=True)
        row = stock_fast.plan(data, chain, {1: one, 5: five}, cfg, NOW)
        self.assertFalse(row['eligible'])
        self.assertEqual(row['blocked_reasons'], ['fast_choppy_five_minute_context'])

    def test_both_lanes_use_buffered_structure_for_premium_stop(self):
        from kadima import strike_selector
        one, five, spot = fixture()
        q = NS(token='opt', symbol='TESTCE', strike=100., opt_type='CE', ltp=10.,
               bid=10., ask=10.01, iv=20., delta=.5, tradeable=True)
        chain = NS(index=NS(name='NIFTY', token='index', opt_exchange='NFO'),
                   _last_poll=100., _expiry='08OCT2026', quotes={'opt': q})
        st = NS(name='NIFTY', chain=chain, spot=spot, last_updated=NOW)
        metric = dict(premium_move_pct=4., volume_ratio=3., observed_at=NOW.isoformat())
        with ExitStack() as stack:
            stack.enter_context(patch.object(fast.time, 'monotonic', return_value=100.))
            stack.enter_context(patch.object(fast.storage, 'recent_candles', side_effect=[one, five]))
            stack.enter_context(patch.object(strike_selector, 'rank', return_value=[NS(quote=q, rejected=None)]))
            stack.enter_context(patch.object(fast.PremiumHistory, 'metrics', return_value=metric))
            stack.enter_context(patch.object(fast, 'live_blocks', return_value=[]))
            stack.enter_context(patch.object(fast, 'scenarios', return_value=[dict(minutes=15, central=15.)]))
            stop = stack.enter_context(patch.object(trade_policy, 'premium_stop', return_value=9.))
            index_row = fast.Lane().scan(st, NOW, record=False)
            self.assertTrue(index_row['eligible'], index_row)
            self.assertEqual(stop.call_args.args[1], 99.5)
            data = dict(chain_at=NOW.isoformat(), spot_at=NOW.isoformat(), spot=spot,
                        fast_metrics={'opt': metric})
            # Producer metric validation has separate tests; isolate shared setup wiring here.
            stack.enter_context(patch.object(stock_fast, 'accelerated', return_value=True))
            stock_row = stock_fast.plan(data, chain, {1: one, 5: five},
                                       dict(holding_minutes=15, revision=1, live_enabled=True), NOW)
            self.assertTrue(stock_row['eligible'], stock_row)
            self.assertEqual(stop.call_args.args[1], 99.5)
            self.assertEqual(index_row['entry_quality'], stock_row['entry_quality'])

    def test_wider_planned_stop_reduces_size_or_rejects_without_more_risk(self):
        allocation, entry, lot_size = 10000., 100., 50
        old = trade_policy.lots_within_allocation(allocation, entry, 95., lot_size, 20)
        new = trade_policy.lots_within_allocation(allocation, entry, 70., lot_size, 20)
        self.assertGreater(old, 0)
        self.assertEqual(new, 0)
        for stop in (95., 90., 85., 80., 70.):
            lots = trade_policy.lots_within_allocation(allocation, entry, stop, lot_size, 20)
            self.assertLessEqual(lots, old)
            self.assertLessEqual(lots*lot_size*trade_policy.risk_per_unit(entry, stop),
                                 allocation*trade_policy.RISK_FRACTION)

    def test_existing_stop_cannot_be_loosened(self):
        for previous in (90., 103., 110.):
            stop, _ = trade_policy.ratchet(100., 115., previous, 80.)
            self.assertGreaterEqual(stop, previous)

    def test_executor_rechecks_fast_context_before_any_order_path(self):
        executor = auto_executor.AutoExecutor()
        chain = NS(index=NS(opt_exchange='NFO'))
        with patch.object(market_session_gate, 'entry_block', return_value=None), \
             patch.object(auto_executor, 'ORDER_PLACEMENT_ENABLED', True), \
             patch.object(auto_executor.audit, 'log'), \
             patch.object(auto_executor.execution_ledger, 'submit') as submit:
            result = executor.evaluate_and_enter('NIFTY', 'CE', chain, 100., None, 2.,
                                                'fast_momentum', 1, 'EXPERIMENTAL', 99.,
                                                fast_context=lambda: {'eligible': False})
        self.assertEqual(result['reason'], 'fast_context_or_live_gate_blocked')
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
