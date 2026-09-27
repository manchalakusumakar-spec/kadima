import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('manual_deploy_test', ROOT/'momentum_recovery/deploy_after_flat.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ManualDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.bundle = self.state/'bundle'
        self.helper = m.load_reviewed_helper()
        self.calls = []
        self.records = []
        self.units = {}
        for name in m.SERVICES:
            self.units[name] = dict(LoadState='not-found', ActiveState='inactive', MainPID='0')
        for name in m.TIMERS:
            self.units[name] = dict(LoadState='loaded', ActiveState='active', UnitFileState='enabled')
        self.units['kadima.service'] = dict(ActiveState='active')
        self.units['kadima-universe-scanner.service'] = dict(ActiveState='active')
        self.units['kadima-dashboard.service'] = dict(ActiveState='active')
        for name, replacement in (
            ('verify_bundle', lambda bundle: None),
            ('check_controls', lambda **kw: None),
            ('check_session_auto', lambda: None),
            ('record_status', lambda state, value: self.records.append(value.copy())),
            ('claim_attempt', lambda state, now: (state/'attempted.json').write_text('claimed')),
            ('observe_unit', lambda name: self.units[name].copy()),
            ('final_database_check', lambda bundle: None),
        ):
            p = patch.object(self.helper, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        self.fail_command = None

    def invoke(self, command):
        self.calls.append(command)
        if self.fail_command and self.fail_command(command):
            return SimpleNamespace(returncode=1)
        if command[0] == '/usr/bin/systemctl':
            self.units[command[-1]].update(ActiveState='inactive', UnitFileState='disabled')
        return SimpleNamespace(returncode=0)

    def execute(self, apply=True):
        return m.execute(self.helper, self.bundle, self.state, apply=apply, invoke=self.invoke)

    def assert_no_apply(self):
        self.assertFalse(any('--apply' in cmd for cmd in self.calls))
        self.assertFalse((self.state/'attempted.json').exists())

    def test_success_keeps_paused_and_runs_exact_reviewed_sequence(self):
        self.execute()
        self.assertIn('--broker-check-only', self.calls[0])
        self.assertEqual([cmd[-1] for cmd in self.calls[1:3]], list(m.TIMERS))
        stages = self.calls[3:]
        self.assertEqual(stages, [cmd for name, cmd in self.helper.stage_commands(self.bundle)])
        self.assertTrue((self.state/'attempted.json').exists())
        self.assertEqual(self.records[-1]['state'], 'verified')
        self.assertTrue(all(r['activation'] is False for r in self.records))
        self.assertFalse(any('go_live.py' in str(cmd) or 'activate_execution.py' in str(cmd) for cmd in self.calls))

    def test_nonflat_broker_blocks_before_timer_or_source_changes(self):
        self.fail_command = lambda cmd: '--broker-check-only' in cmd
        with self.assertRaisesRegex(self.helper.Blocked, 'broker_or_historical_admission_failed'):
            self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assert_no_apply()

    def test_plan_checks_broker_but_does_not_disable_timers(self):
        self.execute(apply=False)
        self.assertEqual(len(self.calls), 1)
        self.assert_no_apply()
        self.assertEqual(self.records[-1]['state'], 'plan_verified')

    def test_unknown_or_stale_controls_block_before_broker(self):
        with patch.object(self.helper, 'check_controls', side_effect=self.helper.Blocked('stale_control_state')):
            with self.assertRaisesRegex(self.helper.Blocked, 'stale_control_state'):
                self.execute()
        self.assertEqual(self.calls, [])
        self.assert_no_apply()

    def test_changed_source_blocks_before_broker(self):
        with patch.object(self.helper, 'verify_bundle', side_effect=self.helper.Blocked('bundle_source_changed')):
            with self.assertRaisesRegex(self.helper.Blocked, 'bundle_source_changed'):
                self.execute()
        self.assertEqual(self.calls, [])
        self.assert_no_apply()

    def test_prior_attempt_never_replays_historical_repair(self):
        (self.state/'attempted.json').write_text('previous attempt')
        with self.assertRaisesRegex(self.helper.Blocked, 'attempt_already_recorded'):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_running_scheduled_deployment_is_never_stopped(self):
        self.units[m.SERVICES[1]].update(LoadState='loaded', ActiveState='active', MainPID='42')
        with self.assertRaisesRegex(self.helper.Blocked, 'competing_service_active'):
            self.execute()
        self.assertEqual(self.calls, [])
        self.assert_no_apply()

    def test_timer_disable_failure_stops_before_attempt(self):
        self.fail_command = lambda cmd: cmd[0] == '/usr/bin/systemctl'
        with self.assertRaisesRegex(self.helper.Blocked, 'timer_hold_failed'):
            self.execute()
        self.assert_no_apply()

    def test_repair_failure_does_not_install_momentum_or_audit(self):
        self.fail_command = lambda cmd: '--apply' in cmd and 'reconcile_contract_note.py' in cmd[3]
        with self.assertRaisesRegex(self.helper.Blocked, 'stage_failed:repair'):
            self.execute()
        self.assertEqual(len(self.calls), 4)
        self.assertTrue((self.state/'attempted.json').exists())
        self.assertEqual(self.records[-1]['stage'], 'repair')

    def test_momentum_failure_does_not_install_audit(self):
        self.fail_command = lambda cmd: '--apply' in cmd and '/momentum/momentum_recovery/install.py' in cmd[3]
        with self.assertRaisesRegex(self.helper.Blocked, 'stage_failed:momentum'):
            self.execute()
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.records[-1]['stage'], 'momentum')

    def test_audit_failure_is_not_reported_as_complete(self):
        self.fail_command = lambda cmd: '--apply' in cmd and 'trade_audit_update/install.py' in cmd[3]
        with self.assertRaisesRegex(self.helper.Blocked, 'stage_failed:audit'):
            self.execute()
        self.assertEqual(self.records[-1]['state'], 'blocked')
        self.assertEqual(self.records[-1]['stage'], 'audit')

    def test_postcheck_failure_retains_attempt_and_reports_block(self):
        with patch.object(self.helper, 'final_database_check', side_effect=self.helper.Blocked('final_local_order_unresolved')):
            with self.assertRaisesRegex(self.helper.Blocked, 'final_local_order_unresolved'):
                self.execute()
        self.assertTrue((self.state/'attempted.json').exists())
        self.assertEqual(self.records[-1]['state'], 'blocked')
        self.assertEqual(self.records[-1]['stage'], 'final_verification')

    def test_controls_rechecked_between_stages(self):
        def controls(**unused):
            if any('reconcile_contract_note.py' in str(cmd) and '--apply' in cmd for cmd in self.calls):
                raise self.helper.Blocked('global_auto_must_remain_paused')
        with patch.object(self.helper, 'check_controls', side_effect=controls):
            with self.assertRaisesRegex(self.helper.Blocked, 'global_auto_must_remain_paused'):
                self.execute()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.records[-1]['stage'], 'momentum')

    def test_unsafe_exception_text_is_not_persisted(self):
        with patch.object(self.helper, 'check_controls', side_effect=ValueError('secret-token-example')):
            with self.assertRaises(ValueError):
                self.execute()
        self.assertEqual(self.records[-1]['reason'], 'ValueError')
        self.assertNotIn('secret-token-example', str(self.records))


if __name__ == '__main__':
    unittest.main()
