"""Source-only release checks. Never execute installer main or broker preflight."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AuditInstall(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('isolated_audit_install', ROOT/'trade_audit_update/install.py')
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.base = self.installer.base

    def test_manifest_never_targets_executor_configuration_or_activation(self):
        paths = set(self.base.FILES)
        self.assertIn('kadima/trade_audit.py', paths)
        self.assertIn('kadima/price_history.py', paths)
        self.assertIn('kadima/execution_ledger.py', paths)
        for name in ('kadima/auto_executor.py', 'kadima/config.py', 'kadima/angel_orders.py',
                     'kadima/rate_limiter.py', 'tools/activate_execution.py'):
            self.assertNotIn(name, paths)
        self.assertEqual(self.installer.PREDECESSORS['base_commit'],
                         'bd294a7048749258fbaa2cc786f20a441a2a8073')

    def test_known_stock_panel_and_routes_survive_idempotent_ui_update(self):
        page = (ROOT/'dashboard_live/dashboard.html').read_text()
        page = self.installer.page_with_stock(page)
        self.assertEqual(self.installer.prepare_ui(page, 'page'), page)
        self.assertEqual(page.count(self.installer.ui.MARKER), 1)
        gateway = self.installer.ui.patch_gateway((ROOT/'dashboard_live/gateway.py').read_text())
        self.assertEqual(self.installer.prepare_ui(gateway, 'gateway'), gateway)
        self.installer.verify_gateway(gateway, (ROOT/'dashboard_live/gateway.py').read_bytes())

    def test_unknown_custom_ui_or_auth_is_preserved_by_refusing(self):
        for kind, path in (('page', 'dashboard_live/dashboard.html'), ('gateway', 'dashboard_live/gateway.py')):
            with self.subTest(kind=kind), self.assertRaisesRegex(self.base.Blocked, 'source_preserved'):
                self.installer.prepare_ui((ROOT/path).read_text()+'\nCUSTOM UNREVIEWED SOURCE', kind)

    def test_backend_plan_preserves_executor_and_refuses_changed_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'installed'
            shutil.copytree(ROOT/'kadima', target/'kadima', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT/'mcx_live_update/auto_executor.py', target/'kadima/auto_executor.py')
            # Other local entrypoint imports may be encountered by dependency traversal.
            (target/'kadima/trade_audit.py').unlink()
            (target/'kadima/price_history.py').unlink()
            executor_before = (target/'kadima/auto_executor.py').read_bytes()
            changes, _ = self.base.backend_plan(ROOT, target, (1000, 1000))
            self.assertEqual({c.path.relative_to(target).as_posix() for c in changes},
                             {'kadima/trade_audit.py','kadima/price_history.py'})
            self.assertEqual((target/'kadima/auto_executor.py').read_bytes(), executor_before)
            (target/'kadima/auto_executor.py').write_bytes(executor_before+b'\n# installed custom stop overlay\n')
            with self.assertRaisesRegex(self.base.Blocked, 'unreviewed_dependency:kadima/auto_executor.py'):
                self.base.backend_plan(ROOT, target, (1000, 1000))
            self.assertTrue((target/'kadima/auto_executor.py').read_bytes().endswith(b'# installed custom stop overlay\n'))

    def test_activation_hashes_include_new_capture_dependencies(self):
        manifest = json.loads((ROOT/'tools/activation_source.json').read_text())
        for path in ('kadima/trade_audit.py', 'kadima/price_history.py', 'kadima/option_chain.py', 'kadima/cas_blast_scanner.py'):
            self.assertEqual(manifest[path], self.installer.digest((ROOT/path).read_bytes()))


if __name__ == '__main__':
    unittest.main()
