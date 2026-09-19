"""Offline source-inventory tests. No AWS, service, market or broker access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'stock_fast_update/review_dependencies.py'
spec = importlib.util.spec_from_file_location('stock_fast_dependency_review_tested', SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.release, self.target = self.base / 'release', self.base / 'live'
        self.release.mkdir(); self.target.mkdir()
        self.sources = {
            'run.py': b'from kadima import engine, stock_fast, stock_fast_api\n',
            'kadima/__init__.py': b'',
            'kadima/engine.py': b'from . import feed, config, rate_limiter\n',
            'kadima/feed.py': b'VALUE = 1\n',
            'kadima/config.py': b'CONFIGURATION = 1\n',
            'kadima/rate_limiter.py': b'FIFO = True\n',
            'kadima/full_universe_scanner.py': b'from . import stock_fast\n',
            'kadima/stock_fast.py': b'from . import feed\n',
            'kadima/stock_fast_api.py': b'from . import stock_fast\n',
        }
        for name, raw in self.sources.items():
            self.write(self.release, name, raw)
            if name not in ('kadima/stock_fast.py', 'kadima/stock_fast_api.py'):
                self.write(self.target, name, raw)
        fifo_patch = patch.object(review, 'FIFO', review.digest(self.sources['kadima/rate_limiter.py']))
        fifo_patch.start(); self.addCleanup(fifo_patch.stop)

    @staticmethod
    def write(root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def run_review(self):
        return review.compare(self.release, self.target)

    def test_matching_dependencies_and_expected_new_files(self):
        result = self.run_review()
        self.assertEqual(result['review_required'], [])
        self.assertEqual(result['static_modules_checked'], 9)
        self.assertEqual(set(result['planned_updates']), {'kadima/stock_fast.py', 'kadima/stock_fast_api.py'})

    def test_all_dependency_mismatches_reported_in_one_pass(self):
        self.write(self.target, 'kadima/feed.py', b'VALUE = 2\n')
        self.write(self.target, 'kadima/engine.py', b'from . import feed, config, rate_limiter\nCHANGED = True\n')
        result = self.run_review()
        self.assertEqual([r['path'] for r in result['review_required']], ['kadima/engine.py', 'kadima/feed.py'])
        self.assertTrue(all('UNREVIEWED_DEPENDENCY' in r['reasons'] for r in result['review_required']))

    def test_real_git_blob_fingerprint_not_worktree_guess(self):
        self.assertEqual(review.digest(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
        installed = b'VALUE = 2\n'
        self.write(self.target, 'kadima/feed.py', installed)
        row = self.run_review()['review_required'][0]
        self.assertEqual(row['installed_blob'], review.digest(installed))
        self.assertEqual(row['release_blob'], review.digest(self.sources['kadima/feed.py']))

    def test_comment_only_difference_does_not_bypass_guard(self):
        self.write(self.target, 'kadima/feed.py', b'# deployed comment\nVALUE = 1\n')
        row = self.run_review()['review_required'][0]
        self.assertTrue(row['same_syntax_tree'])
        self.assertEqual(row['state'], 'REVIEW_REQUIRED')

    def test_config_preserved_without_printing_contents_or_hashes(self):
        self.write(self.target, 'kadima/config.py', b'API_TOKEN = "fixture-secret-value"\n')
        result = self.run_review()
        self.assertEqual(result['review_required'], [])
        row = next(r for r in result['all_observations'] if r['path'] == 'kadima/config.py')
        self.assertIsNone(row['installed_blob']); self.assertIsNone(row['release_blob'])
        self.assertNotIn('fixture-secret-value', json.dumps(result))

    def test_config_syntax_failure_reported_without_source_text(self):
        self.write(self.target, 'kadima/config.py', b'API_TOKEN = "fixture-secret\n')
        result = self.run_review()
        self.assertIn('INSTALLED_SYNTAX_INVALID', result['review_required'][0]['reasons'])
        self.assertNotIn('fixture-secret', review.summary(result))

    def test_invalid_feed_syntax_does_not_hide_other_mismatch(self):
        self.write(self.target, 'kadima/feed.py', b'bad syntax =\n')
        self.write(self.target, 'kadima/engine.py', self.sources['kadima/engine.py'] + b'CHANGED = True\n')
        self.assertEqual(len(self.run_review()['review_required']), 2)

    def test_missing_dependency_is_not_expected_new_file(self):
        (self.target / 'kadima/feed.py').unlink()
        row = self.run_review()['review_required'][0]
        self.assertEqual(row['path'], 'kadima/feed.py')
        self.assertIn('INSTALLED_MISSING', row['reasons'])

    def test_known_base_entrypoint_is_a_planned_upgrade(self):
        raw = b'print("old runner")\n'
        self.write(self.target, 'run.py', raw)
        with patch.dict(review.FILES, {'run.py': review.digest(raw)}):
            result = self.run_review()
        self.assertEqual(result['review_required'], [])
        self.assertIn('run.py', result['planned_updates'])

    def test_unknown_entrypoint_not_overwritten_or_approved(self):
        self.write(self.target, 'run.py', b'print("custom runner")\n')
        row = next(r for r in self.run_review()['review_required'] if r['path'] == 'run.py')
        self.assertIn('UNREVIEWED_TARGET', row['reasons'])

    def test_fifo_mismatch_not_allowed(self):
        self.write(self.target, 'kadima/rate_limiter.py', b'FIFO = False\n')
        row = self.run_review()['review_required'][0]
        self.assertIn('FIFO_HASH_MISMATCH', row['reasons'])

    def test_symlink_not_followed_or_classified_missing_addition(self):
        path = self.target / 'kadima/stock_fast.py'
        secret = self.base / 'outside.py'; secret.write_text('SECRET = "not_printed"')
        path.symlink_to(secret)
        result = self.run_review()
        row = next(r for r in result['review_required'] if r['path'] == 'kadima/stock_fast.py')
        self.assertIn('INSTALLED_SYMLINK_OR_NONCANONICAL', row['reasons'])
        self.assertIsNone(row['installed_blob'])
        self.assertNotIn('not_printed', json.dumps(result))

    def test_nonregular_file_does_not_block_read(self):
        path = self.target / 'kadima/feed.py'; path.unlink(); os.mkfifo(path)
        row = self.run_review()['review_required'][0]
        self.assertIn('INSTALLED_NOT_REGULAR', row['reasons'])

    def test_oversized_source_is_bounded(self):
        self.write(self.target, 'kadima/feed.py', b'x' * (review.MAX_BYTES + 1))
        row = self.run_review()['review_required'][0]
        self.assertIn('INSTALLED_OVERSIZE', row['reasons'])

    def test_import_graph_cycles_terminate(self):
        self.write(self.release, 'kadima/feed.py', b'from . import engine\n')
        self.write(self.target, 'kadima/feed.py', b'from . import engine\n')
        self.assertEqual(self.run_review()['static_modules_checked'], 9)

    def test_installed_only_import_of_a_shipped_module_is_checked(self):
        self.write(self.release, 'kadima/additional.py', b'FLAG = 1\n')
        self.write(self.target, 'kadima/additional.py', b'FLAG = 2\n')
        self.write(self.target, 'kadima/feed.py', b'from . import additional\n')
        paths = [r['path'] for r in self.run_review()['review_required']]
        self.assertEqual(paths, ['kadima/additional.py', 'kadima/feed.py'])

    def test_module_level_code_never_runs(self):
        sentinel = self.base / 'NEVER_CREATE'
        source = f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("bad")\n'.encode()
        self.write(self.release, 'kadima/feed.py', source)
        self.write(self.target, 'kadima/feed.py', source)
        self.run_review()
        self.assertFalse(sentinel.exists())
        self.assertEqual(list(self.target.rglob('__pycache__')), [])

    def test_files_stay_identical_and_output_is_not_approval(self):
        before = {str(p): p.read_bytes() for p in self.target.rglob('*.py')}
        result = self.run_review()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.target.rglob('*.py')})
        self.assertFalse(result['live_files_changed']); self.assertFalse(result['broker_called'])
        self.assertEqual(result['verdict'], 'SOURCE_INVENTORY_ONLY_NOT_INSTALL_APPROVAL')

    def test_same_directory_rejected(self):
        with self.assertRaises(ValueError):
            review.compare(self.release, self.release)

    def test_module_budget_is_not_silently_truncated(self):
        with patch.object(review, 'MAX_MODULES', 1):
            with self.assertRaises(ValueError): self.run_review()

    def test_command_rejects_apply_option(self):
        result = subprocess.run([sys.executable, '-I', '-B', '-S', str(SCRIPT), '--apply'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments: --apply', result.stderr)

    def test_raw_source_secrets_not_in_json_or_summary(self):
        self.write(self.target, 'kadima/feed.py', b'VALUE = "dont_echo_fixture_secret"\n')
        result = self.run_review()
        self.assertNotIn('dont_echo_fixture_secret', json.dumps(result))
        self.assertNotIn('dont_echo_fixture_secret', review.summary(result))
        self.assertIn('FILE: kadima/feed.py', review.summary(result))

    def test_missing_release_dependency_is_reported(self):
        (self.release / 'kadima/stock_fast_api.py').unlink()
        row = next(r for r in self.run_review()['review_required'] if r['path'] == 'kadima/stock_fast_api.py')
        self.assertIn('RELEASE_MISSING', row['reasons'])


if __name__ == '__main__':
    unittest.main()
