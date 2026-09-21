"""Parser warnings must not echo source lines from the inspected live files."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import warnings

SCRIPT = Path(__file__).resolve().parents[1] / 'stock_fast_update/review_dependencies.py'
spec = importlib.util.spec_from_file_location('dependency_review_warning_test', SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class SourceWarningPrivacyTest(unittest.TestCase):
    def test_parser_warning_suppressed_without_changing_syntax_result(self):
        original = review.ast.parse
        def warning_parser(*args, **kwargs):
            warnings.warn('never_echo_source_fixture', SyntaxWarning)
            return original(*args, **kwargs)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter('always')
            with patch.object(review.ast, 'parse', side_effect=warning_parser):
                result = review.syntax_tree(b'VALUE = 1\n')
        self.assertIsNotNone(result)
        self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
