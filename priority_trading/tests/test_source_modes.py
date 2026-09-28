from contextlib import ExitStack
import copy
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('source_modes', Path('normalize_source_modes.py'))
modes = importlib.util.module_from_spec(spec); spec.loader.exec_module(modes)


class SourceModes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'kadima').mkdir()
        self.owner = (os.geteuid(), os.getegid())
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.manifest = {'runtime': {}}
        self.records = []

    def source(self, relative='kadima/auto_executor.py', mode=0o664, raw=b'reviewed = True\n'):
        path = self.root/relative
        path.write_bytes(raw); path.chmod(mode)
        self.manifest['runtime'][relative] = {'old': modes.blob(raw)}
        return path

    def plan(self):
        return modes.plan_sources(self.root, self.manifest, self.owner, self.stack)

    def record(self, value):
        self.records.append(copy.deepcopy(value))

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_plan_does_not_change_permissions_or_create_record(self):
        path = self.source()
        result = modes.repair_modes(self.plan(), self.record, False)
        self.assertEqual(result['state'], 'source_permission_plan')
        self.assertEqual(self.mode(path), 0o664)
        self.assertEqual(self.records, [])

    def test_tightens_all_reviewed_0664_without_changing_bytes_owner_or_inode(self):
        paths = [self.source(), self.source('kadima/engine.py')]
        before = {p:(p.stat(), p.read_bytes()) for p in paths}
        result = modes.repair_modes(self.plan(), self.record, True)
        self.assertEqual(result['state'], 'source_permissions_verified')
        self.assertEqual(result['tightened'], ['kadima/auto_executor.py', 'kadima/engine.py'])
        self.assertFalse(result['deployment_performed'])
        self.assertFalse(result['service_changes_requested'])
        for path, (info, raw) in before.items():
            current = path.stat()
            self.assertEqual(self.mode(path), 0o644)
            self.assertEqual((current.st_ino, current.st_uid, current.st_gid, current.st_mtime_ns),
                             (info.st_ino, info.st_uid, info.st_gid, info.st_mtime_ns))
            self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.records[0]['mutation_attempted'])

    def test_protected_new_and_unlisted_files_are_untouched_and_repeat_is_noop(self):
        self.source()
        protected = self.source('run.py', mode=0o755)
        absent = self.root/'kadima/cas_engine.py'
        self.manifest['runtime']['kadima/cas_engine.py'] = {'old':None}
        other = self.root/'unlisted.py'; other.write_bytes(b'unrelated'); other.chmod(0o664)
        modes.repair_modes(self.plan(), self.record, True)
        repeat = modes.repair_modes(self.plan(), self.record, True)
        self.assertEqual(repeat['planned'], [])
        self.assertEqual(repeat['tightened'], [])
        self.assertEqual(self.mode(protected), 0o755)
        self.assertEqual(self.mode(other), 0o664)
        self.assertFalse(absent.exists())

    def test_later_source_hash_mismatch_prevents_any_permission_change(self):
        first = self.source(); second = self.source('kadima/engine.py')
        second.write_bytes(b'operator edit')
        with self.assertRaisesRegex(modes.Blocked, 'source_bytes_changed:kadima/engine.py'):
            self.plan()
        self.assertEqual(self.mode(first), 0o664)

    def test_existing_new_module_blocks_before_any_permission_change(self):
        first = self.source(); self.source('kadima/cas_engine.py')
        self.manifest['runtime']['kadima/cas_engine.py']['old'] = None
        with self.assertRaisesRegex(modes.Blocked, 'unexpected_existing_module'):
            self.plan()
        self.assertEqual(self.mode(first), 0o664)

    def test_other_writable_or_special_modes_are_not_repaired(self):
        path = self.source()
        for mode in (0o666, 0o660, 0o674, 0o2664, 0o4644):
            with self.subTest(mode=mode):
                path.chmod(mode)
                with self.assertRaisesRegex(modes.Blocked, 'source_mode_not_repairable'):
                    self.plan()
                self.assertEqual(self.mode(path), mode)

    def test_hardlinks_symlinks_and_fifo_are_refused(self):
        path = self.source()
        link = self.root/'linked'; os.link(path, link)
        with self.assertRaisesRegex(modes.Blocked, 'regular_single_link_required'):
            self.plan()
        path.unlink(); path.symlink_to(link)
        with self.assertRaisesRegex(modes.Blocked, 'source_path_noncanonical'):
            self.plan()
        path.unlink(); os.mkfifo(path)
        with self.assertRaisesRegex(modes.Blocked, 'regular_single_link_required'):
            self.plan()

    def test_mode_or_bytes_changed_after_plan_blocks_before_chmod(self):
        path = self.source(); rows = self.plan(); path.chmod(0o666)
        with self.assertRaisesRegex(modes.Blocked, 'source_identity_changed'):
            modes.repair_modes(rows, self.record, True)
        path.chmod(0o664); path.write_bytes(b'changed')
        with self.assertRaisesRegex(modes.Blocked, 'source_bytes_changed'):
            modes.repair_modes(rows, self.record, True)
        self.assertEqual(self.mode(path), 0o664)
        self.assertEqual(self.records, [])

    def test_replaced_inode_after_plan_is_not_modified(self):
        path = self.source(); rows = self.plan()
        replacement = self.root/'replacement'; replacement.write_bytes(path.read_bytes()); replacement.chmod(0o664)
        os.replace(replacement, path)
        with self.assertRaisesRegex(modes.Blocked, 'source_identity_changed'):
            modes.repair_modes(rows, self.record, True)
        self.assertEqual(self.mode(path), 0o664)

    def test_change_during_audit_write_is_checked_before_chmod(self):
        path = self.source(); rows = self.plan()
        def record(value):
            self.record(value)
            if value.get('in_progress'): path.write_bytes(b'changed during record')
        with self.assertRaisesRegex(modes.Blocked, 'source_bytes_changed'):
            modes.repair_modes(rows, record, True)
        self.assertEqual(self.mode(path), 0o664)
        self.assertEqual(self.records[-1]['state'], 'source_permission_repair_incomplete')

    def test_partial_failure_retains_tightening_and_truthful_audit(self):
        first = self.source(); second = self.source('kadima/engine.py'); rows = self.plan()
        original = os.fchmod
        def chmod(fd, mode):
            if fd == rows[1]['fd']: raise OSError('fixture failure')
            original(fd, mode)
        with patch.object(modes.os, 'fchmod', side_effect=chmod), self.assertRaises(OSError):
            modes.repair_modes(rows, self.record, True)
        self.assertEqual(self.mode(first), 0o644)
        self.assertEqual(self.mode(second), 0o664)
        self.assertEqual(self.records[-1]['tightened'], ['kadima/auto_executor.py'])
        self.assertEqual(self.records[-1]['in_progress'], 'kadima/engine.py')

    def test_no_chmod_if_initial_audit_cannot_be_saved(self):
        path = self.source()
        with self.assertRaises(OSError):
            modes.repair_modes(self.plan(), lambda _: (_ for _ in ()).throw(OSError('fixture')), True)
        self.assertEqual(self.mode(path), 0o664)

    def test_late_audit_failure_keeps_actual_mutation_in_error_report(self):
        path = self.source(); calls = []
        def record(value):
            calls.append(copy.deepcopy(value))
            if len(calls) >= 3: raise OSError('fixture disk failure')
        with self.assertRaises(OSError) as caught:
            modes.repair_modes(self.plan(), record, True)
        self.assertEqual(self.mode(path), 0o644)
        report = caught.exception.report
        self.assertTrue(report['mutation_attempted'])
        self.assertTrue(report['audit_update_failed'])
        self.assertEqual(report['tightened'], ['kadima/auto_executor.py'])
        self.assertEqual(report['state'], 'source_permission_repair_incomplete')

    def test_installer_pin_is_unchanged(self):
        self.assertEqual(modes.blob(Path('install.py').read_bytes()), modes.INSTALL_BLOB)

    @unittest.skipUnless(os.geteuid() == 0, 'real cross-UID fixture runs as root in CI')
    def test_real_engine_uid_gid_required_for_0664_root_files_remain_strict(self):
        path = self.source()
        try:
            os.chown(path, 11001, 11001)
        except OSError as exc:
            if exc.errno not in (1,22) or os.environ.get('CI'): raise
            self.skipTest('local user namespace has no cross-UID capability')
        self.owner = (11001,11001)
        modes.repair_modes(self.plan(), self.record, True)
        self.assertEqual((path.stat().st_uid, path.stat().st_gid, self.mode(path)), (11001,11001,0o644))
        for uid,gid in ((0,0),(11001,11002),(11002,11001)):
            os.chown(path,uid,gid); path.chmod(0o664)
            with self.assertRaisesRegex(modes.Blocked, 'source_mode_not_repairable'):
                self.plan()
            self.assertEqual(self.mode(path),0o664)


if __name__ == '__main__': unittest.main()
