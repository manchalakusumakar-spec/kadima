import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('quote_install',
    Path(__file__).resolve().parents[1] / 'quote_budget_fix' / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class QuoteBudgetInstallTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'kadima').mkdir()
        self.target = self.root / 'kadima' / 'rate_limiter.py'
        self.old = b'INTERVAL = 1.1\n'
        self.target.write_bytes(self.old)
        self.target.chmod(0o640)
        self.source = self.root / 'replacement.py'
        self.source.write_bytes(b'INTERVAL = 1.1\nWAIT = 12\n')
        p = patch.object(installer, 'BASELINE_SHA256', hashlib.sha256(self.old).hexdigest())
        p.start()
        self.addCleanup(p.stop)

    def test_dry_run_apply_backup_and_idempotence(self):
        installer.install(self.root, self.source)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(list(self.target.parent.glob('*.quote-backup-*')))
        installer.install(self.root, self.source, apply=True)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        backups = list(self.target.parent.glob('*.quote-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old)
        self.assertIn('Already installed', installer.install(self.root, self.source, apply=True))
        self.assertEqual(len(list(self.target.parent.glob('*.quote-backup-*'))), 1)

    def test_unknown_installed_source_is_not_overwritten(self):
        self.target.write_bytes(b'LOCAL_CHANGE = True\n')
        with self.assertRaisesRegex(ValueError, 'reviewed baseline'):
            installer.install(self.root, self.source, apply=True)
        self.assertEqual(self.target.read_bytes(), b'LOCAL_CHANGE = True\n')
        self.assertFalse(list(self.target.parent.glob('*.quote-backup-*')))

    def test_invalid_replacement_is_not_installed(self):
        self.source.write_bytes(b'def invalid(\n')
        with self.assertRaises(SyntaxError):
            installer.install(self.root, self.source, apply=True)
        self.assertEqual(self.target.read_bytes(), self.old)


if __name__ == '__main__':
    unittest.main()
