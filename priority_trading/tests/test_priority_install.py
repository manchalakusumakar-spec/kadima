import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('priority_install_tests', Path('install.py'))
install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
base_path = Path('stock_fast_update/install.py')
assert install.blob(base_path.read_bytes()) == '00ac2c1ba6b5a68a21cd2bf532dd53e1440e6e35'
spec = importlib.util.spec_from_file_location('priority_metadata_base', base_path)
base = importlib.util.module_from_spec(spec); sys.modules[spec.name] = base
spec.loader.exec_module(base)


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
    def test_initial_admission_failure_does_not_claim_services_disabled(self):
        output = io.StringIO()
        with patch.object(install.os, 'geteuid', return_value=0), \
             patch.object(install.os, 'umask'), \
             patch.object(install.Deployment, '__init__', side_effect=install.Blocked('fixture_metadata')), \
             patch.object(install, 'transaction') as transaction, patch('sys.stdout', output):
            with self.assertRaises(SystemExit):
                install.main(['--history', '/unused', '--evidence', '/unused', '--mcx-capital', '15000', '--apply'])
        transaction.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertIsNone(value['scheduler_enabled'])
        self.assertIsNone(value['watchdog_enabled'])
        self.assertFalse(value['service_changes_requested'])
        self.assertFalse(value['attempt_claimed'])

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


@unittest.skipUnless(os.geteuid() == 0, 'real source ownership fixtures run as root in CI')
class RuntimeMetadata(unittest.TestCase):
    owner = (11001, 11001)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, name='source.py', uid=0, gid=0, mode=0o644):
        path = self.root/name
        path.write_bytes(b'old = 1\n')
        os.chown(path, uid, gid); path.chmod(mode)
        return path

    def plan(self, path):
        return base.change_for(path, b'new = 2\n', self.owner)

    def require_cross_uid(self):
        probe = self.root/'ownership-probe'
        probe.touch()
        try:
            os.chown(probe, self.owner[0]+1, self.owner[1])
        except OSError as exc:
            if exc.errno not in (1, 22) or os.environ.get('CI'):
                raise
            self.skipTest('cross-UID ownership unavailable in local user namespace; required in root CI')

    def ops(self, paths):
        value = install.Deployment.__new__(install.Deployment)
        value.base = base; value.user = NS(pw_uid=self.owner[0], pw_gid=self.owner[1])
        value.changes = [self.plan(path) for path in paths]
        value.files = {item.path:item.original for item in value.changes}
        value.runtime_metadata = {item.path:install.runtime_metadata(item, self.owner, self.root)
                                  for item in value.changes}
        return value

    def test_root_and_engine_owners_are_preserved_by_real_atomic_writer(self):
        self.require_cross_uid()
        cases = ((0, 0, 0o644), (0, 0, 0o755), (0, self.owner[1], 0o640),
                 (*self.owner, 0o600), (*self.owner, 0o644))
        for i, expected in enumerate(cases):
            with self.subTest(expected=expected):
                path = self.source(str(i)+'.py', *expected)
                item = self.plan(path)
                install.runtime_metadata(item, self.owner, self.root)
                base.write_atomic(item, item.replacement)
                info = path.stat()
                self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), expected)
                self.assertEqual(path.read_bytes(), item.replacement)

    def test_untrusted_writable_special_or_unreadable_sources_remain_unchanged(self):
        self.require_cross_uid()
        cases = ((11002, 0, 0o644), (0, 0, 0o664), (*self.owner, 0o666),
                 (0, 0, 0o4644), (0, 0, 0o600), (*self.owner, 0o000))
        for i, values in enumerate(cases):
            with self.subTest(values=values):
                path = self.source(str(i)+'.py', *values)
                item = self.plan(path)
                before = path.stat()
                with self.assertRaisesRegex(install.Blocked, 'runtime_source_metadata:'+path.name+':'):
                    install.runtime_metadata(item, self.owner, self.root)
                self.assertEqual(path.read_bytes(), item.original)
                after = path.stat()
                self.assertEqual((before.st_ino, before.st_uid, before.st_gid, before.st_mode),
                                 (after.st_ino, after.st_uid, after.st_gid, after.st_mode))

    def test_new_modules_use_engine_owner_and_become_bound_after_install(self):
        self.require_cross_uid()
        old = self.source(); new = self.root/'added.py'
        ops = self.ops([old, new])
        self.assertIsNone(ops.runtime_metadata[new])
        with patch.object(install, 'ROOT', self.root):
            ops.install_sources()
            ops.check_runtime_metadata()
        self.assertEqual(old.stat().st_uid, 0)
        self.assertEqual((new.stat().st_uid, new.stat().st_gid, stat.S_IMODE(new.stat().st_mode)),
                         (*self.owner, 0o644))
        self.assertIsNotNone(ops.runtime_metadata[new])
        for path in (old, new): self.assertEqual(ops.files[path], b'new = 2\n')

    def test_metadata_change_after_plan_blocks_before_writing(self):
        path = self.source(); ops = self.ops([path]); path.chmod(0o600)
        with patch.object(install, 'ROOT', self.root), self.assertRaisesRegex(install.Blocked, 'metadata_changed_since_plan'):
            ops.install_sources()
        self.assertEqual(path.read_bytes(), b'old = 1\n')

    def test_same_bytes_replaced_inode_is_detected(self):
        path = self.source(); ops = self.ops([path]); original = path.stat().st_ino
        other = self.source('replacement.py'); os.replace(other, path)
        self.assertNotEqual(path.stat().st_ino, original)
        with patch.object(install, 'ROOT', self.root), self.assertRaisesRegex(install.Blocked, 'runtime_source_identity_changed'):
            ops.install_sources()
        self.assertEqual(path.read_bytes(), b'old = 1\n')

    def test_source_appearing_after_plan_is_not_overwritten(self):
        path = self.root/'added.py'; ops = self.ops([path]); self.source('added.py')
        with patch.object(install, 'ROOT', self.root), self.assertRaisesRegex(install.Blocked, 'source_appeared_since_plan'):
            ops.install_sources()
        self.assertEqual(path.read_bytes(), b'old = 1\n')

    def test_source_bytes_changed_after_plan_are_not_overwritten(self):
        path = self.source(); ops = self.ops([path]); path.write_bytes(b'operator_edit = 3\n')
        with patch.object(install, 'ROOT', self.root), self.assertRaisesRegex(base.Blocked, 'source_changed_since_plan'):
            ops.install_sources()
        self.assertEqual(path.read_bytes(), b'operator_edit = 3\n')

    def test_hardlink_and_symlink_are_not_admitted(self):
        path = self.source(); item = self.plan(path)
        os.link(path, self.root/'linked.py')
        with self.assertRaisesRegex(install.Blocked, 'regular_single_link_required'):
            install.runtime_metadata(item, self.owner, self.root)
        link = self.root/'symlink.py'; link.symlink_to(path)
        item.path = link
        with self.assertRaisesRegex(install.Blocked, 'noncanonical_path'):
            install.runtime_metadata(item, self.owner, self.root)


if __name__ == '__main__': unittest.main()
