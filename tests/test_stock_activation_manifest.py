"""Offline activation-source checks only: never invoke run_activation or main.

This tests exact source identity, not activation readiness or broker behavior.
No live package imports, sockets, subprocesses or production writes are needed.
"""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def git_blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


class StockActivationManifestTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'isolated_stock_activation_manifest', ROOT / 'tools/activate_execution.py')
        self.activation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.activation)  # Definitions only; __main__ is not entered.
        self.manifest = json.loads((ROOT / 'tools/activation_source.json').read_text())
        self.helper = SimpleNamespace(blob=git_blob)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)
        for relative in self.manifest:
            destination = self.target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        self.activation.REPO = self.target

    def test_all_manifest_hashes_match_the_reviewed_checkout(self):
        for relative, expected in self.manifest.items():
            with self.subTest(path=relative):
                self.assertEqual(git_blob((ROOT / relative).read_bytes()), expected)

    def test_stock_entrypoint_and_preserved_dependencies_are_pinned(self):
        required = {'run.py', 'kadima/full_universe_scanner.py', 'kadima/stock_fast.py',
                    'kadima/stock_fast_api.py', 'kadima/feed.py', 'kadima/rate_limiter.py'}
        self.assertTrue(required.issubset(self.manifest))
        self.assertEqual(self.manifest['kadima/full_universe_scanner.py'],
                         '530c41a3fe4fe1e706dcfde3437f4c16e1a19b69')
        self.assertNotIn('kadima/config.py', self.manifest)  # Deployment configuration is not replaced.

    def test_complete_reviewed_source_is_accepted(self):
        self.assertIsNone(self.activation.source_check(self.helper))

    def test_old_scanner_manifest_does_not_approve_the_stock_release(self):
        package = self.target / 'old-release'
        (package / 'tools').mkdir(parents=True)
        old = dict(self.manifest)
        old['kadima/full_universe_scanner.py'] = 'aca08c5c6af10e7fffbbb316eea3d039aeb41731'
        (package / 'tools/activation_source.json').write_text(json.dumps(old))
        with patch.object(self.activation, 'PACKAGE', package):
            with self.assertRaisesRegex(self.activation.Blocked, 'source_mismatch:kadima/full_universe_scanner.py'):
                self.activation.source_check(self.helper)

    def test_missing_stock_module_is_not_accepted(self):
        (self.target / 'kadima/stock_fast.py').unlink()
        with self.assertRaisesRegex(self.activation.Blocked, 'source_mismatch:kadima/stock_fast.py'):
            self.activation.source_check(self.helper)

    def test_different_stock_api_is_not_accepted(self):
        path = self.target / 'kadima/stock_fast_api.py'
        path.write_bytes(path.read_bytes() + b'\n# unreviewed change\n')
        with self.assertRaisesRegex(self.activation.Blocked, 'source_mismatch:kadima/stock_fast_api.py'):
            self.activation.source_check(self.helper)

    def test_feed_and_fifo_cannot_be_silently_changed(self):
        for relative in ('kadima/feed.py', 'kadima/rate_limiter.py'):
            with self.subTest(path=relative):
                path = self.target / relative
                original = path.read_bytes()
                path.write_bytes(original + b'\n# different deployment\n')
                try:
                    with self.assertRaises(self.activation.Blocked):
                        self.activation.source_check(self.helper)
                finally:
                    path.write_bytes(original)

    def test_source_check_does_not_run_activation_or_modify_source(self):
        before = {relative: (self.target / relative).read_bytes() for relative in self.manifest}
        with ExitStack() as stack:
            forbidden = [stack.enter_context(patch.object(self.activation, name,
                side_effect=AssertionError('No operational calls in source-only test')))
                for name in ('command', 'preflight', 'planned_files', 'activate_services', 'resume')]
            self.activation.source_check(self.helper)
            for method in forbidden:
                method.assert_not_called()
        after = {relative: (self.target / relative).read_bytes() for relative in self.manifest}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
