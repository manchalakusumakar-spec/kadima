"""Exercise the optional read-only installed-source comparison in a fixture."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


class SourceComparison(unittest.TestCase):
    def test_reviewed_multi_overlay_matches_without_importing_installed_config(self):
        if not (BASE/'kadima/auto_executor.py').exists():
            self.skipTest('Needs complete reviewed source; runs in GitHub CI')
        session = load('_smoke_compare_session_test', BASE/'session_auto/install.py')
        multi = load('_smoke_compare_multi_test', BASE/'multi_position_update/patch.py')
        with tempfile.TemporaryDirectory(prefix='smoke-installed-fixture-') as folder:
            root = Path(folder)
            (root/'kadima').mkdir()
            for name in ('angel_orders.py', 'execution_ledger.py', 'storage.py', 'portfolio_capacity.py'):
                (root/'kadima'/name).write_bytes((BASE/'kadima'/name).read_bytes())
            (root/'kadima/auto_executor.py').write_bytes(multi.patch_executor(
                session.patch_executor((BASE/'kadima/auto_executor.py').read_bytes())))
            (root/'kadima/market_session_gate.py').write_bytes((BASE/'session_auto/gate.py').read_bytes())
            (root/'kadima/config.py').write_text("raise AssertionError('production_config_must_not_be_imported')\n")
            (root/'kadima/session.py').write_text("raise AssertionError('production_session_must_not_be_imported')\n")
            env = {'PATH': '/usr/bin:/bin'}
            if 'LD_LIBRARY_PATH' in os.environ:
                env['LD_LIBRARY_PATH'] = os.environ['LD_LIBRARY_PATH']
            run = subprocess.run([sys.executable, '-I', '-B', '-S',
                str(BASE/'tools/offline_order_smoke.py'), '--compare-installed', str(root)],
                env=env, cwd=BASE, capture_output=True, text=True, timeout=45)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(report['result'], 'OFFLINE_ORDER_TEST_PASSED')
            self.assertEqual(report['installed_source_files_matched'], 6)
            self.assertEqual(report['real_broker_requests'], 0)
            self.assertFalse(report['production_database_accessed'])
            self.assertFalse(report['live_settings_changed'])
            print('OFFLINE REHEARSAL VERIFIED:', report['tests_run'], 'tests; 6 source files; no broker request.')


if __name__ == '__main__':
    unittest.main()
