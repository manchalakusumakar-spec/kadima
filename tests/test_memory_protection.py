import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock

from kadima import memory_guard as m
import test_execution_lifecycle as f


class MemoryProtection(unittest.TestCase):
    def setUp(self):
        self.clock=0;self.available=600
        self.guard=m.MemoryGuard(lambda:(1000,self.available),lambda:self.clock)

    def advance(self,seconds=60):
        for _ in range(seconds//2):
            self.clock+=2;self.guard.sample(force=True)
        return self.guard.sample()

    def test_startup_requires_continuous_recovery_samples(self):
        self.assertTrue(self.guard.sample()['entries_blocked'])
        self.assertFalse(self.advance()['entries_blocked'])
        self.assertEqual(self.guard.sample()['state'],'normal')

    def test_critical_blocks_entries_and_recovery_does_not_flap(self):
        self.guard.sample();self.advance()
        self.available=80;self.clock+=2
        self.assertEqual(self.guard.sample()['state'],'critical')
        self.available=260
        self.assertTrue(self.advance(30)['entries_blocked'])
        self.available=240
        self.assertTrue(self.advance(2)['entries_blocked'])
        self.available=260
        self.assertTrue(self.advance(58)['entries_blocked'])
        self.assertFalse(self.advance(4)['entries_blocked'])

    def test_warning_defers_only_optional_work(self):
        self.guard.sample();self.advance();self.available=150;self.clock+=2
        value=self.guard.sample()
        self.assertEqual(value['state'],'warning')
        self.assertFalse(value['entries_blocked'])
        self.assertTrue(value['optional_work_delayed'])
        self.assertTrue(value['gamma_cas_protected'])

    def test_bad_metrics_and_sampling_gap_fail_closed(self):
        self.guard.sample();self.advance()
        with patch.object(self.guard,'reader',side_effect=OSError('SECRET')):
            self.clock+=2;value=self.guard.sample()
        self.assertEqual(value['state'],'unavailable')
        self.assertNotIn('SECRET',str(value))
        self.clock+=100
        self.assertTrue(self.guard.sample()['entries_blocked'])

    def test_zero_available_retains_true_minimum(self):
        self.available=0;self.guard.sample();self.available=600;self.clock+=2
        self.assertEqual(self.guard.sample()['minimum_available_bytes'],0)

    def test_manual_hold_persists_and_requires_matching_release(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(m.config,'DATA_DIR',tmp):
            token='a'*32;m.hold_entries(token)
            with patch.object(m.GUARD,'sample',return_value={'entries_blocked':False}):
                self.assertEqual(m.entry_block_reason(),'operations_entries_paused')
            self.assertFalse(m.release_entries('b'*32))
            self.assertTrue(m.release_entries(token))
            self.assertIsNone(m.hold_status())
            (Path(tmp)/'operations-entry-hold.json').write_text('corrupt')
            self.assertEqual(m.entry_block_reason(),'operations_entries_paused')

    def test_buy_is_rechecked_after_broker_login_but_sell_can_exit(self):
        args=dict(tradingsymbol='TESTCE',symboltoken='T',exchange='NFO',quantity=10,price=100)
        with patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',True), \
             patch.object(f.orders.ORDER_WRITE,'allow',return_value=True), \
             patch.object(f.orders.SESSION,'ensure') as login, \
             patch.object(f.orders.SESSION.api,'placeOrder',return_value='ORDER1') as placed, \
             patch.object(f.memory_guard,'entry_block_reason',side_effect=[None,'memory_guard_critical']):
            result=f.orders.place_limit_order(transactiontype='BUY',**args)
            self.assertFalse(result.ok);self.assertFalse(result.uncertain)
            self.assertEqual(result.reason,'memory_guard_critical');placed.assert_not_called();login.assert_called_once()
        with patch.object(f.orders,'ORDER_PLACEMENT_ENABLED',True), \
             patch.object(f.orders.ORDER_WRITE,'allow',return_value=True), \
             patch.object(f.orders.SESSION,'ensure'), \
             patch.object(f.orders.SESSION.api,'placeOrder',return_value='ORDER1') as placed, \
             patch.object(f.memory_guard,'entry_block_reason',return_value='memory_guard_critical') as gate:
            self.assertTrue(f.orders.place_limit_order(transactiontype='SELL',**args).ok)
            placed.assert_called_once();gate.assert_not_called()

    def test_entry_admission_rejects_before_preparation(self):
        executor=f.executor.AutoExecutor()
        with patch.object(f.memory_guard,'entry_block_reason',return_value='memory_guard_critical'), \
             patch.object(f.orders.SESSION,'ensure') as login:
            result=executor.evaluate_and_enter('INDEX','CE',None,100,None,1,'gamma_blast',2,'CONFIRMED',90)
            self.assertEqual(result['reason'],'memory_guard_critical');login.assert_not_called()


if __name__=='__main__':unittest.main()
