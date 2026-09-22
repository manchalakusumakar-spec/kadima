import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('scalper_source_review', ROOT / 'scalper_update/review.py')
reviewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer)


class SourceReview(unittest.TestCase):
    def test_compatible_source_is_read_only_and_not_runtime_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'engine.py').write_bytes(b'old')
            m = dict(files={'engine.py': dict(before=reviewer.blob(b'old'), after=reviewer.blob(b'new'))}, dependencies={})
            result = reviewer.review(root, m)
            self.assertEqual(result['status'], 'BASELINE_SOURCE_COMPATIBLE')
            self.assertFalse(result['runtime_verified'])
            self.assertEqual((root / 'engine.py').read_bytes(), b'old')

    def test_unknown_mixed_and_symlink_sources_are_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'engine.py').write_bytes(b'local overlay')
            (root / 'new.py').write_bytes(b'new')
            m = dict(files={'engine.py': dict(before=reviewer.blob(b'old'), after=reviewer.blob(b'new')),
                            'new.py': dict(before=None, after=reviewer.blob(b'new'))}, dependencies={})
            self.assertEqual(reviewer.review(root, m)['status'], 'BLOCKED')
            (root / 'engine.py').write_bytes(b'old')
            self.assertIn('mixed_release_files', reviewer.review(root, m)['blockers'])
            (root / 'engine.py').unlink()
            (root / 'engine.py').symlink_to(root / 'new.py')
            self.assertIn('engine.py:symlink', reviewer.review(root, m)['blockers'])


if __name__ == '__main__':
    unittest.main()
