"""Root-only configuration must stay private; the engine must read its calendar.

Only temporary directories and isolated child processes are used. No systemd,
HTTP, broker or production filesystem writes occur in these tests.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install = load('private_layout_installer_test', BASE/'session_auto/install.py')
gate = load('private_layout_gate_test', BASE/'session_auto/gate.py')
POLICY = dict(schema=1, valid_from='2026-09-19', valid_through='2026-12-31',
              holidays=[], sources=['local test fixture'], arm_at='09:00',
              entries_at='09:15', stop_entries_at='15:35')


class PrivateLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o755)
        self.etc = self.root/'etc'
        self.etc.mkdir(mode=0o755)
        self.private = self.etc/'kadima'
        self.private.mkdir(mode=0o700)
        self.secret = self.private/'credentials.env'
        self.secret.write_bytes(b'private fixture, never printed')
        self.secret.chmod(0o600)

    def ensure(self, path, mode=0o755, **kwargs):
        return install.ensure_root_directory(path, mode, owner=os.geteuid(), **kwargs)

    def test_root_only_existing_0700_parent_is_preserved(self):
        before = self.private.stat()
        self.ensure(self.private, public=False)
        after = self.private.stat()
        self.assertEqual(before.st_mode, after.st_mode)
        self.assertEqual(before.st_uid, after.st_uid)
        self.assertEqual(before.st_gid, after.st_gid)
        self.assertEqual(self.secret.read_bytes(), b'private fixture, never printed')
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)

    def test_root_only_existing_0750_parent_is_preserved(self):
        self.private.chmod(0o750)
        self.ensure(self.private, public=False)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o750)

    def test_private_ancestor_is_valid_for_root_only_child(self):
        target = self.private/'root-only'
        self.ensure(target, public=False)
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)

    def test_public_child_under_private_parent_still_blocks(self):
        target = self.private/'public'
        with self.assertRaisesRegex(ValueError, 'private_parent_directory_requires_review'):
            self.ensure(target)
        self.assertFalse(target.exists())
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)

    def test_sibling_policy_works_without_chmod_of_private_tree(self):
        shared = self.etc/'kadima-session-auto'
        self.ensure(shared)
        self.ensure(self.private, public=False)
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)

    def test_public_existing_private_target_is_not_relaxed(self):
        target = self.etc/'kadima-session-auto'
        target.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'existing_directory_permissions_require_review'):
            self.ensure(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_dry_validation_creates_nothing(self):
        target = self.etc/'new'/'policy'
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.ensure(target, create=False)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_dry_validation_still_rejects_private_public_path(self):
        with self.assertRaisesRegex(ValueError, 'private_parent_directory_requires_review'):
            self.ensure(self.private/'policy', create=False)
        self.assertFalse((self.private/'policy').exists())

    def test_public_umask_077_compensated_only_for_new_directories(self):
        old = os.umask(0o077)
        try:
            target = self.etc/'new'/'policy'
            self.ensure(target)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)

    def test_private_state_and_new_private_ancestors_keep_0700(self):
        target = self.private/'new'/'state'
        old = os.umask(0o077)
        try:
            self.ensure(target, 0o700, public=False)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_writable_target_rejected_for_both_readers(self):
        self.private.chmod(0o777)
        for public in (True, False):
            with self.subTest(public=public), self.assertRaisesRegex(ValueError, 'unsafe_install_directory_owner'):
                self.ensure(self.private, public=public)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o777)

    def test_wrong_owner_is_not_reassigned(self):
        before = self.private.stat().st_uid
        with self.assertRaisesRegex(ValueError, 'unsafe_install_directory_owner'):
            install.ensure_root_directory(self.private, owner=before+1, public=False)
        self.assertEqual(self.private.stat().st_uid, before)

    def test_symlink_target_and_parent_rejected_for_both_readers(self):
        link = self.etc/'alias'
        link.symlink_to(self.private, target_is_directory=True)
        for path in (link, link/'nested'):
            for public in (True, False):
                with self.subTest(path=path, public=public), self.assertRaisesRegex(ValueError, 'unsafe_install_directory'):
                    self.ensure(path, public=public)

    def test_file_as_target_or_parent_rejected(self):
        for path in (self.secret, self.secret/'child'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.ensure(path, public=False)

    def test_actual_layout_scopes_only_public_calendar_and_lease(self):
        targets = [install.ROOT/'kadima/auto_executor.py', install.CONF/'policy.json',
                   install.ENV, install.DEST/'runner.py', install.SYSTEMD/install.TIMER]
        with patch.object(install, 'ensure_root_directory') as ensure:
            install.prepare_directories(targets, create=False)
        calls = {call.args[0]: call for call in ensure.call_args_list}
        self.assertTrue(calls[install.CONF].kwargs['public'])
        self.assertFalse(calls[install.ENV.parent].kwargs['public'])
        self.assertFalse(calls[install.DEST].kwargs['public'])
        self.assertFalse(calls[Path('/var/lib/kadima-session-auto')].kwargs['public'])
        self.assertFalse(any(call.kwargs['create'] for call in calls.values()))
        self.assertNotIn(install.ROOT/'kadima', calls)

    def test_gate_and_installer_use_same_separate_policy_path(self):
        self.assertEqual(gate.POLICY, install.CONF/'policy.json')
        self.assertEqual(install.CONF, Path('/etc/kadima-session-auto'))
        self.assertFalse(gate.POLICY.is_relative_to(install.ENV.parent))
        self.assertEqual(install.ENV, Path('/etc/kadima/session-auto.env'))

    def test_directory_drycheck_precedes_broker_and_stop(self):
        tree = ast.parse((BASE/'session_auto/install.py').read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='main')
        direct_calls = [n.value for n in main.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        validate = next(c for c in direct_calls if isinstance(c.func, ast.Name) and c.func.id=='prepare_directories')
        flat = next(c for c in direct_calls if isinstance(c.func, ast.Name) and c.func.id=='flat')
        self.assertLess(validate.lineno, flat.lineno)
        self.assertIn('create=False', ast.unparse(validate))

    def test_policy_read_before_restart_with_no_lease_or_order_write(self):
        child = Mock()
        install.check_engine_policy(child)
        child.assert_called_once()
        code = child.call_args.args[0]
        self.assertIn('gate.read_json(gate.POLICY)', code)
        self.assertNotIn('LEASE', code)
        self.assertNotIn('SESSION', code)
        self.assertNotIn('entry_block', code)
        source = (BASE/'session_auto/install.py').read_text()
        self.assertLess(source.index('        check_engine_policy(child)'), source.index('        started=True'))
        self.assertLess(source.index('        check_engine_policy(child)'), source.index("        helper.command(['systemctl','enable','--now',TIMER])"))

    def test_unreadable_policy_check_failure_propagates(self):
        with self.assertRaises(PermissionError):
            install.check_engine_policy(Mock(side_effect=PermissionError))

    @unittest.skipUnless(os.geteuid()==0, 'Needs isolated unprivileged reader to check kernel permissions')
    def test_engine_identity_can_read_policy_but_not_private_credentials(self):
        try:
            reader = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('No unprivileged fixture account')
        shared = self.etc/'kadima-session-auto'
        self.ensure(shared)
        policy = shared/'policy.json'
        policy.write_text(json.dumps(POLICY)); policy.chmod(0o644)
        package = self.root/'kadima'; package.mkdir(mode=0o755)
        (package/'__init__.py').write_text(''); (package/'__init__.py').chmod(0o644)
        source = (BASE/'session_auto/gate.py').read_text().replace(str(gate.POLICY), str(policy))
        module = package/'market_session_gate.py'; module.write_text(source); module.chmod(0o644)
        def child(code):
            code = ('import sys; sys.path.insert(0, '+repr(str(self.root))+'); '+code)
            result = subprocess.run(['/usr/bin/python3', '-I', '-B', '-S', '-c', code],
                user=reader.pw_uid, group=reader.pw_gid, extra_groups=[],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        install.check_engine_policy(child)
        secret_test = ('from pathlib import Path\ntry:\n Path('+repr(str(self.secret))+').read_bytes()\n'
                       'except PermissionError:\n raise SystemExit(0)\nraise SystemExit(1)')
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', '-S', '-c', secret_test],
            user=reader.pw_uid, group=reader.pw_gid, extra_groups=[], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)


if __name__=='__main__':
    unittest.main()
