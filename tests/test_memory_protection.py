"""Synthetic counters and mocked broker boundaries only; no host writes."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import test_execution_lifecycle as f

spec = importlib.util.spec_from_file_location('memory_guard_under_test', f.ROOT/'kadima/memory_guard.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MemoryProtection(unittest.TestCase):
    def setUp(self):
        self.clock = 0
        self.available = 500
        self.guard = m.MemoryGuard(lambda:(1000,self.available), lambda:self.clock)

    def advance(self, seconds=60):
        for _ in range(seconds//2):
            self.clock += 2
            self.guard.sample(force=True)
        return self.guard.sample()

    def warm(self):
        self.guard.sample()
        return self.advance()

    def test_startup_and_continuous_recovery(self):
        self.assertTrue(self.guard.sample()['entries_blocked'])
        self.assertTrue(self.advance(58)['entries_blocked'])
        self.assertFalse(self.advance(2)['entries_blocked'])

    def test_warning_defers_optional_work_only(self):
        self.warm(); self.available = 190
        result = self.advance(2)
        self.assertEqual(result['state'], 'warning')
        self.assertFalse(result['entries_blocked'])
        self.assertTrue(result['optional_work_delayed'])

    def test_15_percent_entry_and_10_percent_critical_boundaries(self):
        self.warm()
        for available, state in ((150,'warning'),(149,'restricted'),(100,'restricted'),(99,'critical')):
            self.available = available
            result = self.advance(2)
            self.assertEqual(result['state'], state)
            self.assertEqual(result['entries_blocked'], available < 150)

    def test_pressure_recovery_requires_unbroken_60_seconds(self):
        self.warm(); self.available = 80; self.advance(2)
        self.available = 260; self.advance(30)
        self.available = 240; self.advance(2)
        self.available = 250
        self.assertTrue(self.advance(60)['entries_blocked'])
        self.assertFalse(self.advance(2)['entries_blocked'])

    def test_sampling_gap_and_clock_reset_fail_closed(self):
        self.warm(); self.clock += 11
        self.assertTrue(self.guard.sample()['entries_blocked'])
        self.clock = -1
        self.assertTrue(self.guard.sample()['entries_blocked'])

    def test_invalid_reading_is_unavailable(self):
        self.warm()
        for reader in (lambda:(0,0),lambda:(100,101),Mock(side_effect=OSError('PRIVATE'))):
            self.guard.reader = reader
            result = self.guard.sample(force=True)
            self.assertEqual(result['state'], 'unavailable')
            self.assertTrue(result['entries_blocked'])
            self.assertNotIn('PRIVATE', str(result))

    def test_zero_minimum_and_cached_result_are_preserved(self):
        self.available = 0
        result = self.guard.sample(); result['state'] = 'tampered'
        self.assertEqual(self.guard.sample()['state'], 'critical')
        self.available = 500
        self.assertEqual(self.advance(2)['minimum_available_bytes'], 0)

    def test_memavailable_not_memfree(self):
        with patch('builtins.open',return_value=io.StringIO('MemTotal: 1000 kB\nMemAvailable: 500 kB\nMemFree: 1 kB\n')):
            self.assertEqual(m.available_memory(), (1024000,512000))

    def test_missing_negative_and_oversized_counters_fail(self):
        for value in ('MemTotal: 1000 kB\n','MemTotal: 1000 kB\nMemAvailable: -1 kB\n','x'*65537):
            with patch('builtins.open',return_value=io.StringIO(value)):
                with self.assertRaises((ValueError,KeyError)):
                    m.available_memory()

    def test_public_entry_reason_samples_fresh(self):
        with patch.object(m, 'GUARD', self.guard):
            self.assertEqual(m.entry_block_reason(), 'memory_guard_recovering')
            self.warm()
            self.available = 90
            self.assertEqual(m.entry_block_reason(), 'memory_guard_critical')
            self.assertFalse(m.optional_work_allowed())


class ExecutionMemoryBoundary(unittest.TestCase):
    args = dict(tradingsymbol='TESTCE',symboltoken='T',exchange='NFO',quantity=10,price=100)

    def test_buy_rechecks_after_session_but_sell_never_checks(self):
        with patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',True), \
             patch.object(f.orders.ORDER_WRITE,'allow',return_value=True), \
             patch.object(f.orders.SESSION,'ensure') as login, \
             patch.object(f.orders.SESSION.api,'placeOrder',return_value='ORDER1') as place, \
             patch.object(f.memory_guard,'entry_block_reason',side_effect=[None,'memory_guard_critical']) as gate:
            result = f.orders.place_limit_order(transactiontype='BUY',**self.args)
            self.assertFalse(result.ok); self.assertFalse(result.uncertain)
            self.assertEqual(result.reason,'memory_guard_critical')
            login.assert_called_once(); place.assert_not_called()
            self.assertEqual(gate.call_count,2)
            gate.reset_mock(); gate.side_effect=AssertionError('SELL must bypass admission')
            self.assertTrue(f.orders.place_limit_order(transactiontype='SELL',**self.args).ok)
            gate.assert_not_called(); place.assert_called_once()

    def test_block_before_login(self):
        with patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',True), \
             patch.object(f.orders.ORDER_WRITE,'allow',return_value=True), \
             patch.object(f.memory_guard,'entry_block_reason',return_value='memory_guard_restricted'), \
             patch.object(f.orders.SESSION,'ensure') as login:
            result = f.orders.place_limit_order(transactiontype='BUY',**self.args)
            self.assertEqual(result.reason,'memory_guard_restricted'); login.assert_not_called()

    def test_entry_admission_before_preparation(self):
        executor = f.executor.AutoExecutor()
        with patch.object(f.memory_guard,'entry_block_reason',return_value='memory_guard_critical'), \
             patch.object(f.orders.SESSION,'ensure') as login:
            result=executor.evaluate_and_enter('INDEX','CE',None,100,None,1,'gamma_blast',2,'CONFIRMED',90)
            self.assertEqual(result['reason'],'memory_guard_critical'); login.assert_not_called()

    def test_cancel_and_reconciliation_do_not_use_memory_admission(self):
        with patch.object(f.memory_guard,'entry_block_reason',side_effect=AssertionError('must bypass')), \
             patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',True), \
             patch.object(f.orders.SESSION,'ensure'), \
             patch.object(f.orders.SESSION.api,'cancelOrder',return_value={'status':True}), \
             patch.object(f.orders.SESSION.api,'orderBook',return_value={'status':True,'data':[]}):
            self.assertTrue(f.orders.cancel_order('O1').ok)
            self.assertEqual(f.orders.fetch_order_book(),[])


if __name__ == '__main__':
    unittest.main()
