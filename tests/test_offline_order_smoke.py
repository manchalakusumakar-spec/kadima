"""Test the no-money runner without broker or production-account access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
PATH = BASE/'tools/offline_order_smoke.py'
spec = importlib.util.spec_from_file_location('_offline_guard_tests', PATH)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base/'source'; self.source.mkdir()
        self.scratch = self.base/'scratch'; self.scratch.mkdir()
        self.live = self.base/'live'; self.live.mkdir()
        self.guard = smoke.Guard(self.source, self.scratch, self.live)

    def test_network_and_process_actions_are_denied(self):
        for event in ('socket.__new__', 'socket.connect', 'socket.getaddrinfo',
                      'subprocess.Popen', 'os.system', 'os.kill', 'os.exec', 'ctypes.dlopen'):
            with self.subTest(event=event), self.assertRaises(smoke.IsolationError):
                self.guard(event, ())

    def test_real_broker_dependencies_are_denied(self):
        for name in ('SmartApi.smartConnect', 'pyotp', 'requests', 'httpx'):
            with self.subTest(name=name), self.assertRaises(smoke.IsolationError):
                self.guard('import', (name,))

    def test_synthetic_source_reads_allowed_but_config_forbidden(self):
        self.guard('open', (str(self.source/'kadima/storage.py'), 'r', os.O_RDONLY))
        for filename in ('config.py', 'session.py', 'credentials.json', '.env'):
            with self.subTest(name=filename), self.assertRaises(smoke.IsolationError):
                self.guard('open', (str(self.source/'kadima'/filename), 'r', os.O_RDONLY))

    def test_only_explicit_live_source_comparison_reads_allowed(self):
        self.guard('open', (str(self.live/'kadima/execution_ledger.py'), 'r', os.O_RDONLY))
        for relative in ('kadima/config.py', 'state/capital_state.json', 'kadima.db'):
            with self.subTest(path=relative), self.assertRaises(smoke.IsolationError):
                self.guard('open', (str(self.live/relative), 'r', os.O_RDONLY))

    def test_no_writes_to_any_installed_file(self):
        for flags in (os.O_WRONLY, os.O_RDWR, os.O_CREAT, os.O_TRUNC):
            with self.subTest(flags=flags), self.assertRaises(smoke.IsolationError):
                self.guard('open', (str(self.live/'kadima/execution_ledger.py'), None, flags))

    def test_private_temp_database_only(self):
        self.guard('sqlite3.connect', (str(self.scratch/'simulation.db'),))
        for value in (str(self.live/'kadima.db'), 'file:/var/lib/kadima/kadima.db?mode=ro', ':memory:'):
            with self.subTest(value=value), self.assertRaises(smoke.IsolationError):
                self.guard('sqlite3.connect', (value,))

    def test_private_temp_files_allowed(self):
        self.guard('open', (str(self.scratch/'fixture'), 'w', os.O_WRONLY | os.O_CREAT))
        self.guard('os.mkdir', (str(self.scratch/'new'), 0o700, -1))
        self.guard('os.remove', (str(self.scratch/'old'), -1))
        self.guard('os.rename', (str(self.scratch/'a'), str(self.scratch/'b'), -1, -1))

    def test_delete_and_rename_outside_scratch_blocked(self):
        for event, args in (
            ('os.remove', (str(self.live/'a'), -1)),
            ('os.rename', (str(self.scratch/'a'), str(self.live/'b'), -1, -1)),
            ('os.chmod', (str(self.live/'a'), 0o777, -1)),
            ('os.symlink', ('a', 'b', -1)),
        ):
            with self.subTest(event=event), self.assertRaises(smoke.IsolationError):
                self.guard(event, args)

    def test_symlink_to_live_file_is_not_private_scratch(self):
        target = self.live/'secret'; target.write_text('DO_NOT_READ')
        link = self.scratch/'escape'; link.symlink_to(target)
        with self.assertRaises(smoke.IsolationError):
            self.guard('open', (str(link), 'r', os.O_RDONLY))

    def test_unknown_installed_version_does_not_get_approval(self):
        for name in smoke.FILES_TO_COMPARE:
            for root in (self.live, self.source):
                (root/name).parent.mkdir(parents=True, exist_ok=True)
                (root/name).write_bytes(b'reviewed')
        (self.source/'session_auto').mkdir()
        (self.source/'session_auto/gate.py').write_bytes(b'reviewed')
        (self.live/'kadima/angel_orders.py').write_bytes(b'unknown')
        with patch.object(smoke, 'load_module', return_value=NS(patch_executor=lambda x:x)):
            with self.assertRaisesRegex(ValueError, 'differs_from_reviewed'):
                smoke.compare_installed(self.live, self.source,
                                        NS(patcher=NS(patch_executor=lambda x:x)))

    def test_no_live_apply_argument(self):
        result = subprocess.run([sys.executable, '-I', '-B', '-S', str(PATH), '--apply'],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('OFFLINE_ORDER_TEST_PASSED', result.stdout)

    def test_refuses_running_in_production_tree(self):
        with self.assertRaisesRegex(ValueError, 'do_not_run_in_live_tree'):
            smoke.run(smoke.PRODUCTION)

    def test_incomplete_release_does_not_claim_pass(self):
        with self.assertRaisesRegex(ValueError, 'complete_reviewed_release_required'):
            smoke.run(self.source)


class FullRehearsal(unittest.TestCase):
    def test_complete_reviewed_execution_rehearsal_without_network(self):
        if not (BASE/'tests/test_execution_lifecycle.py').is_file():
            self.skipTest('Full reviewed repository is required; runs in GitHub CI')
        env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        if 'LD_LIBRARY_PATH' in os.environ:
            env['LD_LIBRARY_PATH'] = os.environ['LD_LIBRARY_PATH']
        result = subprocess.run([sys.executable, '-I', '-B', '-S', str(PATH)],
                                cwd=BASE, env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['result'], 'OFFLINE_ORDER_TEST_PASSED')
        self.assertGreaterEqual(report['tests_run'], 60)
        self.assertEqual(report['skipped'], 0)
        self.assertEqual(report['real_broker_requests'], 0)
        self.assertFalse(report['production_database_accessed'])
        self.assertFalse(report['live_settings_changed'])


if __name__ == '__main__':
    unittest.main()
