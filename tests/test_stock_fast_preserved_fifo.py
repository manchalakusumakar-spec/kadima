"""Offline checks against the exact deployed FIFO blob carried into PR10."""
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'kadima' / 'rate_limiter.py'
spec = importlib.util.spec_from_file_location('stock_fast_fifo_tested', PATH)
limiter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limiter)


class PreservedFIFOTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'budget.sqlite3')

    def test_exact_host_blob_preserved(self):
        data = PATH.read_bytes()
        self.assertEqual(hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest(),
                         '9886d951be6997e0ddf8fdda640529f5f751a25c')
        self.assertEqual(limiter.SHARED_QUOTE_WAIT_SEC, 12.0)

    def test_legacy_budget_not_reset_on_upgrade(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE budget (id INTEGER PRIMARY KEY, next REAL)')
            db.execute('INSERT INTO budget VALUES (1,?)', (time.time()+60,))
        self.assertFalse(limiter.SharedQuoteLimiter(self.path).wait_and_allow(.08))
        with sqlite3.connect(self.path) as db:
            self.assertGreater(db.execute('SELECT next FROM budget WHERE id=1').fetchone()[0],time.time())
            self.assertEqual(db.execute('SELECT count(*) FROM quote_waiters').fetchone()[0],0)

    def test_existing_waiter_cannot_be_overtaken(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE quote_waiters (seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT UNIQUE, expires REAL)')
            db.execute('INSERT INTO quote_waiters(ticket,expires) VALUES (?,?)',('earlier',time.time()+60))
        self.assertFalse(limiter.SharedQuoteLimiter(self.path).wait_and_allow(.08))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT ticket FROM quote_waiters').fetchall(),[('earlier',)])

    def test_expired_waiter_does_not_block_forever(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE quote_waiters (seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT UNIQUE, expires REAL)')
            db.execute('INSERT INTO quote_waiters(ticket,expires) VALUES (?,?)',('expired',time.time()-60))
        self.assertTrue(limiter.SharedQuoteLimiter(self.path).wait_and_allow(.5))

    def test_zero_wait_does_not_create_budget(self):
        self.assertFalse(limiter.SharedQuoteLimiter(self.path).wait_and_allow(0))
        self.assertFalse(Path(self.path).exists())

    def test_nonpositive_or_nonfinite_settings_rejected(self):
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError): limiter.SharedQuoteLimiter(self.path).wait_and_allow(value)
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError): limiter.SharedQuoteLimiter(self.path,interval=value)

    def test_budget_denial_never_calls_transport(self):
        api=Mock(); original=api.getMarketData
        original._kadima_budgeted=False
        with patch.object(limiter.SHARED_QUOTE,'wait_and_allow',return_value=False) as allow:
            limiter.protect_quote_api(api)
            with self.assertRaises(TimeoutError): api.getMarketData(mode='FULL')
        allow.assert_called_once_with(timeout_sec=12.0)
        original.assert_not_called()

    def test_wrapper_is_idempotent(self):
        class API:
            def getMarketData(self, **kwargs): return kwargs
        api=API()
        with patch.object(limiter.SHARED_QUOTE,'wait_and_allow',return_value=True) as allow:
            limiter.protect_quote_api(api); wrapped=api.getMarketData
            limiter.protect_quote_api(api)
            self.assertIs(api.getMarketData,wrapped)
            self.assertEqual(api.getMarketData(mode='FULL'),{'mode':'FULL'})
        allow.assert_called_once()


if __name__ == '__main__': unittest.main()
