import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

spec = importlib.util.spec_from_file_location('priority_install_tests', Path('install.py'))
install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)


class Ops:
    def __init__(self, fail=None):
        self.args = NS(mcx_capital=15000); self.changes = [1]; self.calls = []
        self.fail = fail; self.report = None
    def __getattr__(self, name):
        def action(*a, **kw):
            self.calls.append((name, kw))
            if name == self.fail: raise ValueError('fixture_failure')
            if name == 'record': self.report = dict(a[0])
            return {} if name in ('wait_health','verify_schedules','fail_closed') else None
        return action


class Transaction(unittest.TestCase):
    def test_plan_has_no_stop_write_or_activation(self):
        ops = Ops(); result = install.transaction(ops, False)
        self.assertEqual(result['state'], 'priority_trading_plan')
        self.assertEqual([x[0] for x in ops.calls], ['guard', 'broker_check'])

    def test_broker_is_rechecked_after_engine_stop_before_install(self):
        ops = Ops(); result = install.transaction(ops, True)
        calls = [x[0] for x in ops.calls]
        stop = calls.index('stop_engine'); change = calls.index('install')
        self.assertIn('broker_check', calls[stop:change])
        self.assertLess(calls.index('wait_health'), calls.index('start_schedules'))
        self.assertEqual(result['state'], 'priority_trading_installed_scheduled')
        self.assertFalse(result['fills_verified'])

    def test_failures_cannot_report_success_or_continue_activation(self):
        for stage in ('backup','hold_scheduler','stop_engine','install','start_engine',
                      'wait_health','prepare_schedule','start_schedules','verify_schedules'):
            with self.subTest(stage=stage):
                ops = Ops(stage)
                with self.assertRaises(ValueError): install.transaction(ops, True)
                self.assertEqual(ops.report['state'], 'priority_trading_incomplete')
                self.assertFalse(ops.report['scheduler_enabled'])
                self.assertEqual([x[0] for x in ops.calls][-2:], ['fail_closed','record'])

    def test_environment_preserves_literal_special_characters(self):
        raw = install.environment_bytes({'TOKEN': 'a"b\\c$d`e'})
        self.assertEqual(raw, b'TOKEN="a\\"b\\\\c$d`e"\n')
        for value in ('x\ny', 'x\x00y'):
            with self.assertRaises(install.Blocked): install.environment_bytes({'TOKEN':value})

    def test_watchdog_is_separate_and_uses_exclusive_runtime_module(self):
        service = install.watchdog_service(Path('/home/ubuntu/kadima-py/venv/bin/python3')).decode()
        self.assertIn('User=ubuntu', service)
        self.assertIn('-m kadima.eod_watchdog', service)
        self.assertNotIn('Requires=kadima', service)
        self.assertIn('Asia/Kolkata', install.watchdog_timer().decode())


if __name__ == '__main__': unittest.main()
