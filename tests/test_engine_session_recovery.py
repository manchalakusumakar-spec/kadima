"""Offline session-recovery tests. No broker, HTTP or systemd access."""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('tested_engine_session_recovery',
    BASE/'multi_position_update/recover_engine_session.py')
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.cfg = dict(auto_trade_enabled=False, kill_latched=True, revision=4,
                        rollout_enabled=True, broker_orders_enabled=True)
        self.stock = dict(live_enabled=False, holding_minutes=15, revision=2)
        self.expected = (self.cfg.copy(), self.stock.copy())
        self.units = {recovery.TIMER: 'active', recovery.JOB: 'inactive', 'kadima.service': 'active'}
        def command(args, **kwargs):
            self.events.append(tuple(args))
            if args[1] == 'stop':
                for unit in args[2:]:
                    self.units[unit] = 'inactive'
            elif args[1] == 'start':
                for unit in args[2:]:
                    self.units[unit] = 'active'
            return types.SimpleNamespace(stdout='')
        self.helper = types.SimpleNamespace(ENGINE='kadima.service', command=Mock(side_effect=command),
            unit_state=lambda unit: {'ActiveState': self.units[unit]})
        self.client = object()
        def snapshot(client):
            self.assertIs(client, self.client)
            return (self.cfg.copy(), self.stock.copy())
        def reset_once(client, apply=False):
            self.events.append('reset')
            self.assertTrue(apply)
            self.assertEqual(self.units[recovery.TIMER], 'inactive')
            self.cfg.update(kill_latched=False, auto_trade_enabled=False, revision=5)
            return 'RESET VERIFIED: broker-flat reset accepted; global auto and stock live remain OFF.'
        self.reset = types.SimpleNamespace(snapshot=snapshot, reset_once=Mock(side_effect=reset_once))
        self.check = Mock(side_effect=lambda: self.events.append('check'))
        self.fresh = Mock(side_effect=lambda: self.events.append('fresh_broker'))
        self.wait = Mock(side_effect=lambda: self.events.append('new_engine_verified'))
        self.restore = Mock(return_value=True)

    def run_recovery(self, apply=True):
        return recovery.recover(helper=self.helper, reset=self.reset, client=self.client,
            expected=self.expected, check=self.check, fresh_flat=self.fresh,
            wait_engine=self.wait, restore_safe=self.restore, apply=apply)

    def test_plan_is_not_restart_login_or_reset(self):
        self.assertTrue(self.run_recovery(False).startswith('PLAN ONLY'))
        self.fresh.assert_not_called()
        self.helper.command.assert_not_called()
        self.reset.reset_once.assert_not_called()

    def test_already_clear_does_not_restart_or_reset(self):
        self.cfg['kill_latched'] = False
        self.expected[0]['kill_latched'] = False
        self.assertTrue(self.run_recovery().startswith('ALREADY CLEAR'))
        self.fresh.assert_not_called()
        self.helper.command.assert_not_called()
        self.reset.reset_once.assert_not_called()

    def test_success_fresh_flat_before_restart_before_one_reset(self):
        result = self.run_recovery()
        self.assertTrue(result.startswith('RESET VERIFIED'))
        self.reset.reset_once.assert_called_once_with(self.client, apply=True)
        restart = ('systemctl', 'restart', 'kadima.service')
        self.assertLess(self.events.index('fresh_broker'), self.events.index(restart))
        self.assertLess(self.events.index(restart), self.events.index('new_engine_verified'))
        self.assertLess(self.events.index('new_engine_verified'), self.events.index('reset'))
        self.assertEqual(self.units[recovery.TIMER], 'active')
        self.assertFalse(self.cfg['auto_trade_enabled'])
        self.assertFalse(self.stock['live_enabled'])
        self.assertEqual(self.stock, self.expected[1])

    def test_only_engine_restart_not_scanner_dashboard_or_machine(self):
        self.run_recovery()
        commands = [call.args[0] for call in self.helper.command.call_args_list]
        self.assertEqual(commands, [
            ['systemctl', 'stop', recovery.TIMER, recovery.JOB],
            ['systemctl', 'restart', 'kadima.service'],
            ['systemctl', 'start', recovery.TIMER]])

    def test_failed_source_or_exposure_check_changes_nothing(self):
        self.check.side_effect = recovery.Blocked('source_changed')
        with self.assertRaisesRegex(recovery.Blocked, 'source_changed'):
            self.run_recovery()
        self.fresh.assert_not_called()
        self.helper.command.assert_not_called()

    def test_failed_fresh_broker_check_never_restarts_or_resets(self):
        self.fresh.side_effect = recovery.Blocked('broker_positions:broker_response_unsuccessful:AG8001')
        with self.assertRaisesRegex(recovery.Blocked, 'AG8001'):
            self.run_recovery()
        self.fresh.assert_called_once()
        self.helper.command.assert_not_called()
        self.reset.reset_once.assert_not_called()

    def test_nonflat_current_broker_blocks_before_restart(self):
        self.fresh.side_effect = recovery.Blocked('broker_positions:broker_positions_not_flat:NO_RECOGNIZED_CODE')
        with self.assertRaisesRegex(recovery.Blocked, 'not_flat'):
            self.run_recovery()
        self.helper.command.assert_not_called()
        self.reset.reset_once.assert_not_called()

    def test_controls_changed_before_login_is_not_overridden(self):
        self.stock['revision'] = 3
        with self.assertRaisesRegex(recovery.Blocked, 'controls_changed'):
            self.run_recovery()
        self.fresh.assert_not_called()

    def test_control_change_after_preflight_prevents_restart(self):
        def change():
            self.stock['live_enabled'] = True
        self.fresh.side_effect = change
        with self.assertRaisesRegex(recovery.Blocked, 'controls_changed'):
            self.run_recovery()
        self.helper.command.assert_not_called()

    def test_timer_failed_to_stop_prevents_engine_restart(self):
        base = self.helper.command.side_effect
        def run(args, **kwargs):
            result = base(args, **kwargs)
            if args[1] == 'stop':
                self.units[recovery.JOB] = 'active'
            return result
        self.helper.command.side_effect = run
        with self.assertRaisesRegex(recovery.Blocked, 'scheduler_not_stopped'):
            self.run_recovery()
        self.reset.reset_once.assert_not_called()
        self.assertNotIn(('systemctl', 'restart', 'kadima.service'), self.events)
        self.assertEqual(self.units[recovery.TIMER], 'active')

    def test_restart_failure_does_not_reset(self):
        base = self.helper.command.side_effect
        def run(args, **kwargs):
            if args[1] == 'restart':
                raise subprocess.TimeoutExpired(args, 90)
            return base(args, **kwargs)
        self.helper.command.side_effect = run
        self.restore.return_value = False
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_recovery()
        self.reset.reset_once.assert_not_called()
        self.assertEqual(self.units[recovery.TIMER], 'inactive')

    def test_unverified_new_engine_does_not_reset(self):
        self.wait.side_effect = recovery.Blocked('new_engine_identity_not_confirmed')
        self.restore.return_value = False
        with self.assertRaisesRegex(recovery.Blocked, 'new_engine_identity'):
            self.run_recovery()
        self.reset.reset_once.assert_not_called()

    def test_failed_reset_is_not_retried_and_timer_can_be_restored(self):
        self.reset.reset_once.side_effect = recovery.Blocked('verified_flat_account_required')
        with self.assertRaisesRegex(recovery.Blocked, 'verified_flat_account_required'):
            self.run_recovery()
        self.reset.reset_once.assert_called_once()
        self.assertTrue(self.cfg['kill_latched'])
        self.assertEqual(self.units[recovery.TIMER], 'active')

    def test_lost_reset_response_does_not_replay_or_claim_success(self):
        def lost(*args, **kwargs):
            self.cfg.update(kill_latched=False, revision=5)
            raise recovery.Blocked('reset_result_unconfirmed_read_status_before_retry')
        self.reset.reset_once.side_effect = lost
        self.restore.return_value = False
        with self.assertRaisesRegex(recovery.Blocked, 'reset_result_unconfirmed'):
            self.run_recovery()
        self.reset.reset_once.assert_called_once()
        self.assertEqual(self.units[recovery.TIMER], 'inactive')

    def test_post_reset_change_blocks_next_upgrade(self):
        self.restore.return_value = False
        with self.assertRaisesRegex(recovery.Blocked, 'post_reset_state_changed'):
            self.run_recovery()
        self.assertEqual(self.units[recovery.TIMER], 'inactive')

    def test_final_verification_failure_cannot_report_success(self):
        self.restore.side_effect = [True, False]
        with self.assertRaisesRegex(recovery.Blocked, 'timer_restore_requires_verified_state'):
            self.run_recovery()
        self.assertEqual(self.units[recovery.TIMER], 'inactive')

    def test_timer_restore_failure_not_claimed_success(self):
        base = self.helper.command.side_effect
        def run(args, **kwargs):
            if args[1] == 'start':
                raise subprocess.CalledProcessError(1, args)
            return base(args, **kwargs)
        self.helper.command.side_effect = run
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_recovery()

    def test_timer_restore_still_inactive_is_reported(self):
        base = self.helper.command.side_effect
        def run(args, **kwargs):
            result = base(args, **kwargs)
            if args[1] == 'start':
                self.units[recovery.TIMER] = 'inactive'
            return result
        self.helper.command.side_effect = run
        with self.assertRaisesRegex(recovery.Blocked, 'timer_restore_unconfirmed'):
            self.run_recovery()


class BrokerDiagnosticsTests(unittest.TestCase):
    broker = types.SimpleNamespace(STAGES={'broker_positions', 'broker_login'},
        REASONS={'broker_response_unsuccessful', 'exception'})

    def error(self, value, prefix=''):
        return subprocess.CalledProcessError(1, ['python'],
            output=prefix + 'KADIMA_DIAGNOSTIC ' + json.dumps(value) + '\n',
            stderr='SECRET_RAW_SDK_RESPONSE')

    def test_only_approved_code_no_raw_provider_message(self):
        exc = self.error(dict(stage='broker_positions', reason='broker_response_unsuccessful',
            broker_code='AG8001', message='SECRET_BODY'), 'SECRET_HEADER\n')
        result = recovery.broker_failure(exc, self.broker)
        self.assertEqual(result, 'broker_positions:broker_response_unsuccessful:AG8001')
        self.assertNotIn('SECRET', result)

    def test_unknown_code_not_echoed(self):
        exc = self.error(dict(stage='broker_positions', reason='broker_response_unsuccessful',
                             broker_code='SECRET_TOKEN'))
        self.assertTrue(recovery.broker_failure(exc, self.broker).endswith('NO_RECOGNIZED_CODE'))

    def test_invalid_stage_not_echoed(self):
        exc = self.error(dict(stage='SECRET_TOKEN', reason='exception'))
        self.assertEqual(recovery.broker_failure(exc, self.broker),
                         'fresh_broker_check_failed_private_details_suppressed')

    def test_malformed_diagnostic_safe(self):
        for value in (None, 'SECRET', [], 1):
            result = recovery.broker_failure(self.error(value), self.broker)
            self.assertEqual(result, 'fresh_broker_check_failed_private_details_suppressed')

    def test_unhashable_values_safe(self):
        # Malformed child output must not be allowed to escape sanitization.
        exc = self.error(dict(stage='broker_positions', reason='exception', broker_code=[]))
        self.assertTrue(recovery.broker_failure(exc, self.broker).endswith('NO_RECOGNIZED_CODE'))

    def test_nonstring_stage_or_reason_safe(self):
        for value in (dict(stage=[], reason='exception'), dict(stage='broker_positions', reason={})):
            self.assertEqual(recovery.broker_failure(self.error(value), self.broker),
                             'fresh_broker_check_failed_private_details_suppressed')

    def test_timeout_only_reports_timeout(self):
        exc = subprocess.TimeoutExpired(['SECRET'], 60, output='SECRET', stderr='SECRET')
        self.assertEqual(recovery.broker_failure(exc, self.broker), 'fresh_broker_check_timed_out')


class SourceTests(unittest.TestCase):
    fixture = """SESSION.ensure()
code = str(r.get('errorcode') or '') if isinstance(r,dict) else ''
"""
    def test_fresh_login_and_camel_case_code_handling(self):
        h = types.SimpleNamespace(broker_preflight_code=lambda x: x)
        b = types.SimpleNamespace(PREFLIGHT=self.fixture)
        result = recovery.fresh_preflight_code(h, b)
        self.assertEqual(result.count('SESSION.ensure(force=True)'), 1)
        self.assertIn("r.get('errorCode')", result)
        compile(result, 'test', 'exec')

    def test_changed_preflight_not_silently_modified(self):
        h = types.SimpleNamespace(broker_preflight_code=lambda x: x)
        b = types.SimpleNamespace(PREFLIGHT='unknown code')
        with self.assertRaisesRegex(recovery.Blocked, 'shape_changed'):
            recovery.fresh_preflight_code(h, b)

    def test_unknown_helper_not_imported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'multi_position_update').mkdir()
            (root/'multi_position_update/reset_kill.py').write_text("raise RuntimeError('should not execute')")
            with self.assertRaisesRegex(recovery.Blocked, 'reviewed_recovery_helper'):
                recovery.load_pinned(root, 'multi_position_update/reset_kill.py')

    def test_real_dependency_hashes_and_read_only_broker_preflight(self):
        if not (BASE/'executor_fix/install.py').exists():
            self.skipTest('Complete repository dependency check runs in GitHub CI')
        for name, expected in recovery.PINNED.items():
            self.assertEqual(recovery.digest((BASE/name).read_bytes()), expected, name)
        h = recovery.load_pinned(BASE, 'stock_fast_update/install.py')
        b = recovery.load_pinned(BASE, 'executor_fix/install.py')
        source = recovery.fresh_preflight_code(h, b)
        self.assertIn("if 'data' not in r:", source)
        self.assertIn("for name in ('position','orderBook')", source)
        self.assertNotIn('placeOrder', source)
        self.assertNotIn('cancelOrder', source)
        self.assertNotIn('modifyOrder', source)
        self.assertNotIn('terminateSession', source)

    def test_no_live_trading_package_imports_at_module_load(self):
        tree = ast.parse((BASE/'multi_position_update/recover_engine_session.py').read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        for node in imports:
            names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            self.assertFalse(any(n and (n.startswith('kadima') or n.startswith('SmartApi')) for n in names))

    def test_reset_occurs_only_once_and_requires_success_return(self):
        source = (BASE/'multi_position_update/recover_engine_session.py').read_text()
        self.assertEqual(source.count('reset.reset_once('), 1)
        self.assertIn("startswith('RESET VERIFIED:')", source)
        self.assertNotIn('reset_kill=True', source)
        self.assertNotIn("['systemctl', 'enable'", source)
        self.assertNotIn('write_atomic(', source)
        self.assertNotIn('write_text(', source)


if __name__ == '__main__':
    unittest.main()
