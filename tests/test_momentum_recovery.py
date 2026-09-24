"""Offline regression of missed-move recovery; no broker submission."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kadima.config import IST
from kadima import move_capture as detector
from kadima import move_capture_service as service
from kadima import move_capture_execution as execution
from kadima import move_capture_handoff as handoff
from kadima import storage, stock_fast
from kadima.momentum_quote_watch import QuoteWatch, MAX_CONTRACTS
from kadima.preparation_diagnostics import describe
from kadima.stock_execution import parse_candles

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=IST)


class TapeTests(unittest.TestCase):
    def test_continuation_both_directions_without_gamma(self):
        for sign, direction in ((1, 'CE'), (-1, 'PE')):
            engine = detector.MoveCaptureDetector()
            identity = detector.SpotIdentity('TEST', 'NSE', '1')
            volume = 0
            for sec in range(481):
                volume += 10 if sec < 420 else 30
                row, _ = engine.ingest(identity, NOW+timedelta(seconds=sec),
                    100+sign*.006*sec, volume, 100)
            self.assertEqual(row['trigger_reason'], 'directional_continuation')
            self.assertEqual(row['direction'], direction)
            self.assertIn('TEST', engine.hot)

    def test_old_tick_cannot_mutate_price_volume_or_day(self):
        tracker = detector.SymbolTracker(detector.SpotIdentity('TEST', 'NSE', '1'))
        tracker.add(NOW, 100, 10)
        tracker.add(NOW+timedelta(seconds=1), 101, 20)
        self.assertFalse(tracker.add(NOW-timedelta(days=1), 999, 99999))
        self.assertEqual(tracker.high, 101)
        self.assertEqual(tracker._last_cum_volume, 20)
        self.assertEqual(tracker.day, NOW.date())

    def test_gap_restarts_evidence_without_fake_vwap(self):
        tracker = detector.SymbolTracker(detector.SpotIdentity('TEST', 'NSE', '1'))
        tracker.add(NOW, 100, 10)
        tracker.add(NOW+timedelta(seconds=1), 101, 20)
        tracker.add(NOW+timedelta(seconds=30), 130, 90000)
        self.assertIsNone(tracker.vwap)
        self.assertFalse(tracker.features()['ready'])
        self.assertEqual(len(tracker.samples), 1)

    def test_feed_rejects_stale_future_duplicate_and_stopped_ticks(self):
        identity = detector.SpotIdentity('TEST', 'NSE', '1')
        fake = Mock(hot={})
        fake.ingest.return_value = (None, False)
        with patch.object(service, 'universe', return_value={('NSE', '1'): identity}), \
             patch.object(service, 'now_ist', return_value=NOW), \
             patch.object(service, 'in_market_hours', return_value=True):
            feed = service.MoveCaptureService(fake)
            message = dict(token='1', exchange_type=1, last_traded_price=10000,
                           volume_trade_for_the_day=10)
            for offset in (-9, 1):
                feed.on_data(None, dict(message, exchange_timestamp=(NOW.timestamp()+offset)*1000))
            fake.ingest.assert_not_called()
            good = dict(message, exchange_timestamp=NOW.timestamp()*1000)
            feed.on_data(None, good)
            feed.on_data(None, good)
            self.assertEqual(fake.ingest.call_count, 1)
            feed._stop.set()
            feed.on_data(None, good)
            self.assertEqual(fake.ingest.call_count, 1)


class HistoryAndDiagnosticsTests(unittest.TestCase):
    def test_old_gap_recovers_only_from_real_contiguous_suffix(self):
        rows = [[(NOW-timedelta(minutes=m)).isoformat(), 100, 102, 99, 101, 10]
                for m in (10, 9, 7, 6, 5, 4, 3, 2, 1)]
        with self.assertRaisesRegex(ValueError, 'Missing session candles'):
            parse_candles(rows, 1, NOW)
        recovered = parse_candles(rows, 1, NOW, recent_contiguous=True)
        self.assertEqual(len(recovered), 7)
        self.assertEqual(recovered[0]['start_ts'], rows[2][0])
        with self.assertRaisesRegex(ValueError, 'Stale historical candles'):
            parse_candles(rows, 1, NOW+timedelta(minutes=10), recent_contiguous=True)

    def test_known_reason_and_redaction(self):
        self.assertEqual(describe(ValueError('Missing session candles'), 'history')['reason'],
                         'history_session_gap')
        value = describe(RuntimeError('Authorization: Bearer SECRET'), 'history')
        self.assertEqual(value['reason'], 'preparation_error')
        self.assertNotIn('SECRET', str(value))
        self.assertEqual(value['preparation_stage'], 'history')


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(storage.SCHEMA)
        self.addCleanup(self.db.close)
        self.stack.enter_context(patch.object(storage, 'conn', return_value=self.db))
        self.stack.enter_context(patch.object(execution, 'now_ist', return_value=NOW))
        self.stack.enter_context(patch.object(handoff, 'now_ist', return_value=NOW))
        self.stack.enter_context(patch.object(execution, 'live_requested', return_value=True))
        self.controls = dict(live_enabled=True, holding_minutes=15, revision=1)
        self.control_mock = self.stack.enter_context(patch.object(stock_fast, 'controls', side_effect=lambda: dict(self.controls)))
        self.stack.enter_context(patch.object(execution.fast_momentum, 'live_blocks', return_value=[]))
        self.stack.enter_context(patch.object(execution.audit, 'log'))
        self.executor = SimpleNamespace(fast_rows={}, evaluate_and_enter=Mock(return_value={'ok': True, 'reason': 'submitted'}))
        self.worker = execution.MoveCaptureExecutionWorker(self.executor, Mock())
        self.worker.quote_watch = Mock()
        self.quote = SimpleNamespace(token='123', symbol='TEST29SEP26100PE',
            ltp=10., bid=9.99, ask=10.01, volume=100, strike=100, opt_type='PE')
        self.chain = SimpleNamespace()
        self.worker._chain = Mock(return_value=(self.chain, self.quote, 5))
        self.data = dict(underlying='TEST', episode='move:TEST:PE:1', direction='PE',
            score=80., price=98., spot_stop=99., asset_scope='STOCK',
            observed_at=NOW.isoformat())
        handoff.publish(self.data)
        self.row = handoff.current('TEST')

    def confirm_and_dispatch(self):
        with patch.object(execution.time, 'monotonic', return_value=100):
            self.worker.process(self.row)
        self.executor.evaluate_and_enter.assert_not_called()
        self.quote.ltp = 10.1
        self.quote.bid = 10.09
        self.quote.ask = 10.11
        self.quote.volume = 110
        with patch.object(execution.time, 'monotonic', return_value=103):
            self.worker.process(self.row)

    def test_two_samples_dispatch_shared_executor_and_persist_deadline(self):
        self.confirm_and_dispatch()
        self.executor.evaluate_and_enter.assert_called_once()
        args, kwargs = self.executor.evaluate_and_enter.call_args
        self.assertEqual(args[1], 'PE')
        self.assertEqual(args[6], 'fast_momentum')
        self.assertEqual(kwargs['fast_context']()['source_strategy'], 'move_capture')
        self.assertEqual(self.db.execute('SELECT hold_minutes FROM stock_fast_policies').fetchone()[0], 15)

    def test_control_change_and_stop_crossing_invalidate_before_submission(self):
        self.confirm_and_dispatch()
        context = self.executor.evaluate_and_enter.call_args.kwargs['fast_context']
        self.controls['revision'] = 2
        self.assertIsNone(context())
        self.controls['revision'] = 1
        handoff.publish(dict(self.data, price=100))
        self.assertIsNone(context())

    def test_pause_prevents_option_reads_and_dispatch(self):
        self.controls['live_enabled'] = False
        self.worker.process(self.row)
        self.worker._chain.assert_not_called()
        self.executor.evaluate_and_enter.assert_not_called()
        self.assertIn('stock_live_control_disabled', self.worker.rows['TEST']['blocked_reasons'])

    def test_flat_option_volume_cannot_confirm(self):
        with patch.object(execution.time, 'monotonic', return_value=100):
            self.worker.process(self.row)
        self.quote.ltp = 10.5
        with patch.object(execution.time, 'monotonic', return_value=103):
            self.worker.process(self.row)
        self.executor.evaluate_and_enter.assert_not_called()
        self.assertEqual(self.worker.rows['TEST']['blocked_reasons'], ['move_capture_option_volume_not_confirmed'])

    def test_slot_rejection_keeps_same_signal_id_for_retry(self):
        self.executor.evaluate_and_enter.return_value = dict(ok=False, reason='max_positions_reached')
        self.confirm_and_dispatch()
        sid = self.executor.evaluate_and_enter.call_args.args[7]
        self.quote.ltp = 10.2
        self.quote.volume = 120
        with patch.object(execution.time, 'monotonic', return_value=106):
            self.worker.process(self.row)
        self.assertEqual(self.executor.evaluate_and_enter.call_count, 2)
        self.assertEqual(self.executor.evaluate_and_enter.call_args.args[7], sid)
        self.assertEqual(self.db.execute('SELECT count(*) FROM signals').fetchone()[0], 1)

    def test_quote_watch_persists_contract_outside_ladder_and_caps_growth(self):
        watch = QuoteWatch()
        with patch('kadima.momentum_quote_watch.now_ist', return_value=NOW):
            self.assertTrue(watch.pin('TEST', self.quote))
            self.assertTrue(QuoteWatch().pin('TEST', self.quote))
            for i in range(1, MAX_CONTRACTS):
                self.quote.token = str(1000+i)
                self.assertTrue(watch.pin('TEST', self.quote))
            self.quote.token = 'over-cap'
            self.assertFalse(watch.pin('TEST', self.quote))
            self.assertEqual(self.db.execute('SELECT count(*) FROM momentum_quote_watch').fetchone()[0], MAX_CONTRACTS)
            self.assertEqual(self.db.execute('SELECT token FROM momentum_quote_watch WHERE token=?', ('123',)).fetchone()[0], '123')


if __name__ == '__main__':
    unittest.main()
