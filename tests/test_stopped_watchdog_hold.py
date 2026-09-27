"""Exact cron hold tests with private real backups and a fake crontab process."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stopped_watchdog_hold_test',
    ROOT/'momentum_recovery/stopped_watchdog_hold.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CronHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root/'state'
        self.original = (b'CRON_TZ=UTC\r\nPRIVATE_TOKEN=never-print-me\r\n' +
                         m.LINE+b'\r\n7 2 * * * echo unchanged\n# final comment without newline')
        self.current = self.original
        self.calls = []
        self.list_count = 0
        self.write_count = 0
        self.on_list = None
        self.on_write = None
        root = patch.object(m.os, 'geteuid', return_value=0)
        root.start()
        self.addCleanup(root.stop)

        def parents(path):
            if path.resolve() != path:
                raise RuntimeError('noncanonical')
        def private(path, *, directory=False):
            info = path.lstat()
            if (path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    or (not directory and info.st_nlink != 1)):
                raise RuntimeError('not_private')
            return path
        def create(path, raw):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                   0o600), 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        self.helper = SimpleNamespace(root_parents=parents, private_path=private,
            read_regular=lambda path, limit: path.read_bytes(), create_private=create)

    def invoke(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(command[:3], ['/usr/bin/crontab', '-u', 'ubuntu'])
        self.assertEqual(len(command), 4)
        if command[3] == '-l':
            self.assertEqual(kwargs, {})
            self.list_count += 1
            if self.on_list:
                self.on_list(self.list_count)
            return SimpleNamespace(returncode=0, stdout=self.current, stderr=b'')
        self.assertEqual(command[3], '-')
        self.assertEqual(set(kwargs), {'input'})
        self.assertIsInstance(kwargs['input'], bytes)
        self.write_count += 1
        if self.on_write:
            return self.on_write(kwargs['input'])
        self.current = kwargs['input']
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    def hold(self):
        return m.hold_reviewed_cron(self.helper, invoke=self.invoke, state_dir=self.state)

    def verify(self):
        return m.verify_held_cron(self.helper, invoke=self.invoke, state_dir=self.state)

    def test_exact_known_job_only_is_held_with_every_other_byte_preserved(self):
        with (patch('sqlite3.connect', side_effect=AssertionError('database prohibited')),
              patch('socket.create_connection', side_effect=AssertionError('network prohibited'))):
            result = self.hold()
        self.assertEqual(self.current, self.original.replace(m.LINE, m.MARKER+m.LINE, 1))
        self.assertEqual((self.state/m.ORIGINAL).read_bytes(), self.original)
        self.assertEqual((self.state/m.HELD).read_bytes(), self.current)
        self.assertEqual(self.write_count, 1)
        self.assertEqual(self.list_count, 3)
        self.assertIs(result['held'], True)
        self.assertIs(result['mutation_attempted'], True)
        self.assertEqual(result['line'], 3)
        self.assertEqual(result['reviewed_line_sha256'], m.LINE_SHA256)
        self.assertEqual(result['schedule']['hour'], '9')
        self.assertEqual(result['schedule_timezone'], 'not_inferred')
        self.assertNotIn('never-print-me', json.dumps(result))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for path in self.state.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_verified_retry_is_read_only_and_does_not_replace_backups(self):
        self.hold()
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        result = self.hold()
        self.assertIs(result['held'], True)
        self.assertIs(result['already_held'], True)
        self.assertIs(result['mutation_attempted'], False)
        self.assertEqual(self.write_count, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})

    def test_duplicate_different_or_already_commented_lines_never_install(self):
        cases = (m.LINE+b'\n'+m.LINE+b'\n', m.LINE+b' \n', b'# '+m.LINE+b'\n')
        for index, current in enumerate(cases):
            self.state = self.root/f'state-{index}'
            self.current = current
            with self.subTest(current=current), self.assertRaises(m.Blocked):
                self.hold()
            self.assertEqual(self.current, current)
            self.assertEqual(self.write_count, 0)

    def test_concurrent_whole_crontab_change_before_install_is_preserved(self):
        changed = self.original+b'\n# concurrent operator edit\n'
        def change(n):
            if n == 2:
                self.current = changed
        self.on_list = change
        with self.assertRaisesRegex(m.Blocked, 'crontab_changed_before_hold') as error:
            self.hold()
        self.assertEqual(self.current, changed)
        self.assertEqual(self.write_count, 0)
        self.assertIs(error.exception.hold_receipt['mutation_attempted'], False)
        self.assertEqual((self.state/m.ORIGINAL).read_bytes(), self.original)
        with self.assertRaises(m.Blocked):
            self.hold()
        self.assertEqual(self.current, changed)
        self.assertEqual(self.write_count, 0)

    def test_failed_install_retains_backup_without_restore_or_retry_write(self):
        self.on_write = lambda raw: SimpleNamespace(returncode=1, stdout=b'', stderr=b'private error')
        with self.assertRaisesRegex(m.Blocked, 'crontab_hold_command_failed') as error:
            self.hold()
        self.assertEqual(self.current, self.original)
        self.assertEqual(self.write_count, 1)
        self.assertIs(error.exception.hold_receipt['held'], False)
        self.assertIs(error.exception.hold_receipt['mutation_attempted'], True)
        self.assertNotIn('private error', str(error.exception)+json.dumps(error.exception.hold_receipt))
        self.assertEqual((self.state/m.ORIGINAL).read_bytes(), self.original)
        with self.assertRaises(m.Blocked):
            self.hold()
        self.assertEqual(self.write_count, 1)

    def test_command_error_after_successful_install_reports_verified_hold(self):
        def fail_after_install(raw):
            self.current = raw
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'private detail')
        self.on_write = fail_after_install
        with self.assertRaises(m.Blocked) as error:
            self.hold()
        self.assertIs(error.exception.hold_receipt['held'], True)
        self.assertEqual(error.exception.hold_receipt['state'], 'held_verified')
        self.assertIs(self.hold()['already_held'], True)
        self.assertEqual(self.write_count, 1)

    def test_timeout_is_uncertain_until_exact_readback_without_any_restore(self):
        def timeout(raw):
            self.current = raw+b'\n# another operator edit\n'
            raise subprocess.TimeoutExpired(['/usr/bin/crontab'], 15)
        self.on_write = timeout
        with self.assertRaises(m.Blocked) as error:
            self.hold()
        self.assertIs(error.exception.hold_receipt['held'], False)
        self.assertEqual(self.write_count, 1)
        self.assertTrue(self.current.endswith(b'# another operator edit\n'))
        self.assertEqual((self.state/m.ORIGINAL).read_bytes(), self.original)

    def test_post_install_mismatch_blocks_without_overwriting_new_cron_changes(self):
        def change(n):
            if n == 3:
                self.current += b'\n# another operator edit\n'
        self.on_list = change
        with self.assertRaisesRegex(m.Blocked, 'crontab_hold_not_verified'):
            self.hold()
        changed = self.current
        self.assertEqual(self.write_count, 1)
        with self.assertRaises(m.Blocked):
            self.hold()
        self.assertEqual(self.current, changed)
        self.assertEqual(self.write_count, 1)

    def test_changed_backup_or_manifest_refuses_retry_before_crontab_write(self):
        self.hold()
        original_files = {p: p.read_bytes() for p in self.state.iterdir()}
        for path in (self.state/m.ORIGINAL, self.state/m.HELD, self.state/m.MANIFEST):
            with self.subTest(path=path.name):
                path.write_bytes(original_files[path]+b'changed')
                with self.assertRaises(m.Blocked):
                    self.hold()
                self.assertEqual(self.write_count, 1)
                path.write_bytes(original_files[path])

    def test_changed_current_crontab_fails_read_only_guard(self):
        self.hold()
        self.current += b'\n# new cron job\n'
        before = self.current
        with self.assertRaisesRegex(m.Blocked, 'held_crontab_changed_requires_review') as error:
            self.verify()
        self.assertIs(error.exception.hold_receipt['held'], False)
        self.assertEqual(self.current, before)
        self.assertEqual(self.write_count, 1)

    def test_partial_private_record_or_public_backup_never_gets_overwritten(self):
        self.state.mkdir(mode=0o700)
        (self.state/m.ORIGINAL).write_bytes(self.original)
        (self.state/m.ORIGINAL).chmod(0o600)
        with self.assertRaisesRegex(m.Blocked, 'partial_watchdog_hold_record'):
            self.hold()
        self.assertEqual(self.calls, [])
        (self.state/m.ORIGINAL).unlink()
        self.hold()
        (self.state/m.ORIGINAL).chmod(0o644)
        with self.assertRaises(m.Blocked):
            self.hold()
        self.assertEqual(self.write_count, 1)

    def test_unprivileged_call_cannot_read_or_install_crontab(self):
        with patch.object(m.os, 'geteuid', return_value=1000), self.assertRaisesRegex(m.Blocked, 'sudo_required'):
            self.hold()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
