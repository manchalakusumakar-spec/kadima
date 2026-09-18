"""Offline quote-budget contention tests. No SDK, credentials or orders."""
import importlib.util
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'kadima' / 'rate_limiter.py'


def load_rate():
    spec = importlib.util.spec_from_file_location('quote_scheduler_under_test', SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def worker(path, name, results, interval, timeout, repeats=1, start=None):
    rate = load_rate()
    limiter = rate.SharedQuoteLimiter(path, interval=interval)
    if start is not None:
        start.wait(5)
    for _ in range(repeats):
        allowed = limiter.wait_and_allow(timeout)
        results.put((name, allowed, time.monotonic()))


class SharedQuoteSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / 'budget.sqlite3')
        self.rate = load_rate()
        self.context = multiprocessing.get_context('spawn')
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE budget (id INTEGER PRIMARY KEY, next REAL)')
            conn.execute('CREATE TABLE grants (next REAL)')
            conn.execute('CREATE TRIGGER record_grant AFTER INSERT ON budget '
                         'BEGIN INSERT INTO grants VALUES (NEW.next); END')

    def queue_size(self):
        with sqlite3.connect(self.path) as conn:
            try:
                return conn.execute('SELECT COUNT(*) FROM quote_waiters').fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    def await_queue_size(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.queue_size() == expected:
                return
            time.sleep(.01)
        self.fail('worker did not join quote queue')

    def process(self, *args, **kwargs):
        proc = self.context.Process(target=worker, args=args, kwargs=kwargs)
        proc.start()
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.is_alive():
            proc.terminate()
        proc.join(3)

    def block_budget(self, until):
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT OR REPLACE INTO budget VALUES (1,?)', (until,))
            conn.execute('DELETE FROM grants')

    def release_budget(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE budget SET next=0 WHERE id=1')

    def test_fifo_scanner_requeues_behind_waiting_oi(self):
        self.block_budget(time.time() + 30)
        results = self.context.Queue()
        scanner = self.process(self.path, 'scanner', results, .12, 6, repeats=2)
        self.await_queue_size(1)
        oi = self.process(self.path, 'oi', results, .12, 6)
        self.await_queue_size(2)
        self.release_budget()
        observed = [results.get(timeout=7) for _ in range(3)]
        for proc in (scanner, oi):
            proc.join(2)
            self.assertEqual(proc.exitcode, 0)
        self.assertEqual([row[:2] for row in observed],
                         [('scanner', True), ('oi', True), ('scanner', True)])
        self.assertEqual(self.queue_size(), 0)

    def test_competing_processes_make_progress_without_rate_burst(self):
        results = self.context.Queue()
        start = self.context.Event()
        processes = [self.process(self.path, str(i), results, .08, 3,
                                  repeats=3, start=start) for i in range(8)]
        start.set()
        observed = [results.get(timeout=7) for _ in range(24)]
        for proc in processes:
            proc.join(2)
            self.assertEqual(proc.exitcode, 0)
        self.assertTrue(all(row[1] for row in observed))
        with sqlite3.connect(self.path) as conn:
            grants = [row[0] for row in conn.execute('SELECT next FROM grants ORDER BY next')]
        self.assertEqual(len(grants), 24)
        self.assertTrue(all(b - a >= .07999 for a, b in zip(grants, grants[1:])))
        self.assertEqual(self.queue_size(), 0)

    def test_expired_crashed_worker_does_not_block_following_request(self):
        self.block_budget(time.time() + 30)
        results = self.context.Queue()
        crashed = self.process(self.path, 'crashed', results, .08, .6)
        self.await_queue_size(1)
        self.stop(crashed)
        following = self.process(self.path, 'following', results, .08, 3)
        self.release_budget()
        self.assertEqual(results.get(timeout=4)[:2], ('following', True))
        following.join(2)
        self.assertEqual(following.exitcode, 0)
        self.assertEqual(self.queue_size(), 0)

    def test_deadline_cancels_ticket_without_consuming_budget(self):
        self.block_budget(time.time() + 5)
        limiter = self.rate.SharedQuoteLimiter(self.path, interval=.1)
        start = time.monotonic()
        self.assertFalse(limiter.wait_and_allow(.15))
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(self.queue_size(), 0)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grants').fetchone()[0], 0)
        self.release_budget()
        self.assertTrue(limiter.wait_and_allow(.2))

    def test_locked_database_fails_closed_with_bounded_wait(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        conn.execute('BEGIN IMMEDIATE')
        start = time.monotonic()
        self.assertFalse(self.rate.SharedQuoteLimiter(self.path).wait_and_allow(.15))
        self.assertLess(time.monotonic() - start, .5)
        conn.rollback()

    def test_existing_budget_survives_queue_schema_upgrade(self):
        until = time.time() + .3
        self.block_budget(until)
        limiter = self.rate.SharedQuoteLimiter(self.path, interval=.08)
        self.assertFalse(limiter.wait_and_allow(.05))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT next FROM budget WHERE id=1').fetchone()[0], until)
        self.assertTrue(limiter.wait_and_allow(1))

    def test_overdue_tickets_are_spaced_from_actual_grants(self):
        self.block_budget(time.time() - 10)
        limiter = self.rate.SharedQuoteLimiter(self.path, interval=.12)
        self.assertTrue(limiter.wait_and_allow(.5))
        self.assertTrue(limiter.wait_and_allow(.5))
        with sqlite3.connect(self.path) as conn:
            grants = [row[0] for row in conn.execute('SELECT next FROM grants ORDER BY next')]
        self.assertGreaterEqual(grants[1] - grants[0], .11999)

    def test_wrapper_is_idempotent_and_preserves_arguments(self):
        method = Mock(return_value={'status': True})
        method._kadima_budgeted = False
        api = types.SimpleNamespace(getMarketData=method)
        with patch.object(self.rate, 'SHARED_QUOTE') as limiter:
            limiter.wait_and_allow.return_value = True
            self.rate.protect_quote_api(api)
            self.rate.protect_quote_api(api)
            self.assertEqual(api.getMarketData('FULL', {'NFO': ['1']}), {'status': True})
            limiter.wait_and_allow.assert_called_once_with(timeout_sec=12.0)
            method.assert_called_once_with('FULL', {'NFO': ['1']})
        self.assertEqual(self.rate.SHARED_QUOTE.interval, 1.1)

    def test_wrapper_never_calls_sdk_after_queue_timeout(self):
        method = Mock()
        method._kadima_budgeted = False
        api = types.SimpleNamespace(getMarketData=method)
        with patch.object(self.rate, 'SHARED_QUOTE') as limiter:
            limiter.wait_and_allow.return_value = False
            self.rate.protect_quote_api(api)
            with self.assertRaisesRegex(TimeoutError, 'shared quote budget'):
                api.getMarketData(mode='FULL', exchangeTokens={'NFO': ['1']})
        method.assert_not_called()

    def test_wrapper_does_not_retry_broker_errors(self):
        method = Mock(side_effect=ConnectionError('offline fixture'))
        method._kadima_budgeted = False
        api = types.SimpleNamespace(getMarketData=method)
        with patch.object(self.rate, 'SHARED_QUOTE') as limiter:
            limiter.wait_and_allow.return_value = True
            self.rate.protect_quote_api(api)
            with self.assertRaises(ConnectionError):
                api.getMarketData('FULL', {'NFO': ['1']})
        method.assert_called_once()


if __name__ == '__main__':
    unittest.main()
