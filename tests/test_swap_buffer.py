"""Temporary files, mocked system commands; never creates real swap."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('swap_buffer_test',ROOT/'tools/install_swap_buffer.py')
swap=importlib.util.module_from_spec(spec); spec.loader.exec_module(swap)


class SwapBufferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        for key,value in dict(PATH=self.root/'buffer',UNIT_PATH=self.root/'buffer.swap',
                              LOCK_PATH=self.root/'lock',SIZE=4096).items():
            self.stack.enter_context(patch.object(swap,key,value))
        self.mem=self.stack.enter_context(patch.object(swap,'memory',return_value={
            'MemTotal':1000,'MemAvailable':500,'SwapTotal':0}))
        self.stack.enter_context(patch.object(swap.os,'geteuid',return_value=0))
        self.stack.enter_context(patch.object(swap.shutil,'disk_usage',return_value=SimpleNamespace(
            free=100000,total=200000)))
        def command(args):
            return SimpleNamespace(stdout='ext4' if args[0]=='findmnt' else swap.UNIT_PATH.name)
        self.run=self.stack.enter_context(patch.object(swap,'run',side_effect=command))
        self.active=self.stack.enter_context(patch.object(swap,'own_swap_active',return_value=True))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_plan_is_read_only(self):
        swap.main()
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual([call.args[0][0] for call in self.run.call_args_list],['findmnt','systemd-escape'])

    def test_existing_swap_is_preserved(self):
        self.mem.return_value={'SwapTotal':100}
        swap.main()
        self.run.assert_not_called(); self.assertEqual(list(self.root.iterdir()),[])

    def test_unknown_files_and_dangling_symlinks_are_refused(self):
        for target in (swap.PATH,swap.UNIT_PATH):
            target.symlink_to(self.root/'missing')
            try:
                with self.assertRaisesRegex(ValueError,'existing_unverified'):
                    swap.check()
            finally:
                target.unlink()

    def test_low_memory_defers_without_commands(self):
        self.mem.return_value['MemAvailable']=249
        with self.assertRaisesRegex(ValueError,'memory_recovers'):
            swap.check()
        self.run.assert_not_called()

    def test_insufficient_disk_is_refused(self):
        with patch.object(swap.shutil,'disk_usage',return_value=SimpleNamespace(free=16000,total=100000)):
            with self.assertRaisesRegex(ValueError,'disk_headroom'):
                swap.check()

    def test_unsupported_filesystem_and_unit_name_are_refused(self):
        self.run.side_effect=lambda args:SimpleNamespace(stdout='btrfs')
        with self.assertRaisesRegex(ValueError,'filesystem_requires'):
            swap.check()
        self.run.side_effect=lambda args:SimpleNamespace(stdout='ext4' if args[0]=='findmnt' else 'wrong.swap')
        with self.assertRaisesRegex(ValueError,'unit_name'):
            swap.check()

    def test_apply_requires_root(self):
        with patch.object(swap.os,'geteuid',return_value=1000):
            with self.assertRaises(SystemExit):swap.main(True)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_apply_creates_private_buffer_and_boot_unit_no_trading_restart(self):
        swap.main(True)
        self.assertEqual(swap.PATH.stat().st_size,swap.SIZE)
        self.assertEqual(swap.PATH.stat().st_mode & 0o777,0o600)
        commands=[call.args[0] for call in self.run.call_args_list]
        self.assertIn(['mkswap',str(swap.PATH)],commands)
        self.assertIn(['systemctl','start',swap.UNIT_PATH.name],commands)
        self.assertIn(['systemctl','enable',swap.UNIT_PATH.name],commands)
        self.assertFalse(any('restart' in c or 'swapoff' in c for c in commands))
        self.assertEqual(swap.UNIT_PATH.read_text(),swap.UNIT)

    def test_failure_after_activation_retains_files(self):
        original=self.run.side_effect
        def failing(args):
            if args[:2]==['systemctl','enable']:raise TimeoutError('unknown outcome')
            return original(args)
        self.run.side_effect=failing
        with self.assertRaisesRegex(SystemExit,'Partial files retained'):
            swap.main(True)
        self.assertTrue(swap.PATH.exists()); self.assertTrue(swap.UNIT_PATH.exists())

    def test_partial_allocation_is_retained_for_inspection(self):
        with patch.object(swap.os,'fsync',side_effect=OSError('disk issue')):
            with self.assertRaisesRegex(SystemExit,'Partial files retained'):
                swap.main(True)
        self.assertTrue(swap.PATH.exists())
        self.assertFalse(swap.UNIT_PATH.exists())

    def test_missing_exact_swap_verification_is_not_success(self):
        self.active.return_value=False
        with self.assertRaisesRegex(SystemExit,'incomplete'):
            swap.main(True)
        self.assertTrue(swap.PATH.exists())

    def test_preexisting_lock_symlink_is_refused(self):
        swap.LOCK_PATH.symlink_to(self.root/'victim')
        with self.assertRaises(OSError):swap.main(True)
        self.assertFalse((self.root/'victim').exists())


if __name__=='__main__':unittest.main()
