"""Static-file installation tests. No systemd, network, broker or real webroot."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('layout_install_test', ROOT/'dashboard_layout/install.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LayoutInstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = self.root/'dashboard.html'
        self.original = (ROOT/'momentum_recovery/reviewed_dashboard/dashboard.html').read_bytes()
        self.page.write_bytes(self.original); self.page.chmod(0o644)
        self.backups = self.root/'backups'; self.backups.mkdir()

    def run_install(self, apply=True):
        return module.install(self.page, ROOT/'dashboard_layout', self.backups, apply=apply)

    def test_plan_is_read_only(self):
        before = self.page.stat()
        self.run_install(False)
        self.assertEqual(self.page.read_bytes(), self.original)
        self.assertEqual(module.fingerprint(before), module.fingerprint(self.page.stat()))
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_atomic_overlay_backup_metadata_and_idempotence(self):
        info = self.page.stat()
        backup = self.run_install()
        after = self.page.read_bytes()
        self.assertEqual(after, module.build(self.original))
        self.assertIn(b'id="legacy-panels"', after)
        self.assertEqual((backup/'dashboard.before.html').read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((backup/'dashboard.before.html').stat().st_mode), 0o600)
        self.assertEqual((self.page.stat().st_uid, self.page.stat().st_gid, stat.S_IMODE(self.page.stat().st_mode)),
                         (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)))
        self.assertEqual(json.loads((backup/'installed.json').read_text())['state'], 'installed')
        self.assertIsNone(self.run_install())
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_unknown_existing_page_is_preserved(self):
        self.page.write_bytes(self.original+b'<!-- operator change -->')
        with self.assertRaisesRegex(module.Blocked, 'unreviewed_page_preserved'):
            self.run_install()
        self.assertTrue(self.page.read_bytes().endswith(b'<!-- operator change -->'))
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_unreviewed_asset_is_rejected(self):
        assets = self.root/'assets'; assets.mkdir()
        (assets/'layout.css').write_text('body{display:none}')
        with self.assertRaisesRegex(module.Blocked, 'layout_asset_changed'):
            module.install(self.page, assets, self.backups, apply=True)
        self.assertEqual(self.page.read_bytes(), self.original)

    def test_changed_page_before_swap_is_preserved(self):
        real = module.build
        def build(raw, assets):
            result = real(raw, assets)
            if not list(self.backups.iterdir()):
                self.page.write_bytes(self.original+b'<!-- concurrent change -->')
            return result
        with patch.object(module, 'build', side_effect=build):
            with self.assertRaisesRegex(module.Blocked, 'page_changed_before_replace'):
                self.run_install()
        self.assertTrue(self.page.read_bytes().endswith(b'<!-- concurrent change -->'))
        self.assertEqual(list(self.root.glob('.kadima-layout-*')), [])

    def test_linked_and_world_writable_pages_are_rejected(self):
        link = self.root/'link.html'; link.symlink_to(self.page)
        with self.assertRaises(module.Blocked):
            module.install(link, backups=self.backups, apply=True)
        hard = self.root/'hard.html'; os.link(self.page, hard)
        with self.assertRaisesRegex(module.Blocked, 'single_regular_file_required'):
            self.run_install()
        hard.unlink(); self.page.chmod(0o666)
        with self.assertRaisesRegex(module.Blocked, 'unsafe_file_mode'):
            self.run_install()

    def test_replace_failure_retains_original_and_private_backup(self):
        with patch.object(module.os, 'replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.run_install()
        self.assertEqual(self.page.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('.kadima-layout-*')), [])
        self.assertEqual(len(list(self.backups.iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
