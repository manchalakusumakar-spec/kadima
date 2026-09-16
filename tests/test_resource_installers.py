import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock,MagicMock

ROOT=Path(__file__).resolve().parents[1]

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

swap=load('swap_setup','tools/install_swap_buffer.py')
ops=load('operations_setup','operations/install.py')

class Installers(unittest.TestCase):
    def test_existing_swap_is_preserved_without_commands(self):
        with patch.object(swap,'memory',return_value={'SwapTotal':1024}),patch.object(swap,'run') as run:
            self.assertEqual(swap.check(),'existing_swap');run.assert_not_called()

    def test_unrecognized_existing_swap_file_is_never_reformatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'existing';path.write_text('keep this')
            with patch.object(swap,'PATH',path),patch.object(swap,'memory',return_value={'SwapTotal':0}),patch.object(swap,'run') as run:
                with self.assertRaisesRegex(ValueError,'existing_unverified'):swap.check()
                self.assertEqual(path.read_text(),'keep this');run.assert_not_called()

    def test_low_memory_defers_swap_creation(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(swap,'PATH',Path(tmp)/'swap'), \
             patch.object(swap,'UNIT_PATH',Path(tmp)/'unit'), \
             patch.object(swap,'memory',return_value={'SwapTotal':0,'MemTotal':1000,'MemAvailable':200}), \
             patch.object(swap,'run') as run:
            with self.assertRaisesRegex(ValueError,'memory_recovers'):swap.check()
            run.assert_not_called()

    def test_disk_headroom_preserved_and_unsupported_filesystem_refused(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(swap,'PATH',Path(tmp)/'swap'), \
             patch.object(swap,'UNIT_PATH',Path(tmp)/'unit'), \
             patch.object(swap,'memory',return_value={'SwapTotal':0,'MemTotal':1000,'MemAvailable':600}), \
             patch.object(swap.shutil,'disk_usage',return_value=SimpleNamespace(total=20*swap.SIZE,free=3*swap.SIZE)), \
             patch.object(swap,'run',return_value=SimpleNamespace(stdout='btrfs')) as run:
            with self.assertRaisesRegex(ValueError,'disk_headroom'):swap.check()
            run.assert_not_called()
            swap.shutil.disk_usage.return_value=SimpleNamespace(total=20*swap.SIZE,free=10*swap.SIZE)
            with self.assertRaisesRegex(ValueError,'filesystem_requires'):swap.check()
            self.assertEqual(run.call_count,1)

    def test_unknown_operations_code_stops_before_any_service_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'control.py';path.write_text('unreviewed')
            with patch.object(ops,'TARGETS',{'control.py':path}),patch.object(ops.os,'geteuid',return_value=0),patch.object(ops,'run') as run:
                with self.assertRaisesRegex(SystemExit,'Unreviewed'):ops.main()
                run.assert_not_called();self.assertEqual(path.read_text(),'unreviewed')


if __name__=='__main__':unittest.main()
