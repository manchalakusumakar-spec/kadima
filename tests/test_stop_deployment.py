"""Offline source composition, settings preservation and deployed-adapter tests."""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
import unittest
from unittest.mock import patch

import test_stop_runtime as runtime_tests
import test_execution_lifecycle as lifecycle

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT/path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


installer = module('_test_stop_installer', 'stop_update/install.py')
deployed = module(lifecycle.PKG+'.deployed_stop_executor', 'stop_update/auto_executor.py')


class ComposedRuntime(runtime_tests.Runtime):
    def setUp(self):
        patch.object(lifecycle, 'executor', deployed).start()
        super().setUp()


class Release(unittest.TestCase):
    @contextmanager
    def configured(self, applying=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'release'
            target = Path(directory)/'target'
            (target/'kadima').mkdir(parents=True)
            original = b'import os\nCAPITAL=12345\n'
            (target/'kadima/config.py').write_bytes(original)
            manifest = json.loads((ROOT/'stop_update/manifest.json').read_text())
            manifest['installed']['kadima/config.py'] = installer.digest(original)
            paths = ['stop_update/config_additions.py','stock_fast_update/install.py']
            paths += ['stop_update/auto_executor.py' if n=='kadima/auto_executor.py' else n
                      for n in manifest['incoming']]
            for name in paths:
                destination = root/name
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes((ROOT/name).read_bytes())
            (root/'stop_update/manifest.json').write_text(json.dumps(manifest))
            with patch.object(installer,'ROOT',root):
                yield installer.configure(target,applying)

    def test_installer_requires_fresh_paused_flat_controls(self):
        with self.configured() as base:
            status = dict(ts=datetime.now(timezone.utc).isoformat(),positions=[],
                          config=dict(rollout_enabled=True,auto_trade_enabled=False,kill_latched=False))
            base.paused_status(status)
            for key,value in (('auto_trade_enabled',True),('kill_latched',True),('rollout_enabled',None)):
                bad = dict(status,config=dict(status['config'],**{key:value}))
                with self.assertRaises(base.Blocked):base.paused_status(bad)
            for bad in (dict(status,positions=[{}]),dict(status,ts='2000-01-01T00:00:00+00:00')):
                with self.assertRaises(base.Blocked):base.paused_status(bad)

    def test_apply_refuses_active_scheduler(self):
        with self.configured(applying=True) as base:
            status = dict(ts=datetime.now(timezone.utc).isoformat(),positions=[],
                          config=dict(rollout_enabled=True,auto_trade_enabled=False,kill_latched=False))
            with patch.object(base,'unit_state',return_value={'ActiveState':'active','MainPID':'0'}):
                with self.assertRaisesRegex(base.Blocked,'pause_session_scheduler'):
                    base.paused_status(status)
            with patch.object(base,'unit_state',return_value={'ActiveState':'inactive','MainPID':'0'}):
                base.paused_status(status)

    def test_release_hashes_match(self):
        manifest = json.loads((ROOT/'stop_update/manifest.json').read_text())
        for name, expected in manifest['incoming'].items():
            path = 'stop_update/auto_executor.py' if name == 'kadima/auto_executor.py' else name
            self.assertEqual(installer.digest((ROOT/path).read_bytes()), expected)
        self.assertEqual(installer.digest((ROOT/'stock_fast_update/install.py').read_bytes()),
                         manifest['installer_helper'])
        self.assertEqual(installer.digest((ROOT/'stop_update/config_additions.py').read_bytes()),
                         manifest['config_additions'])

    def test_config_preserves_bytes_and_is_idempotent(self):
        original = b'import os\nLIVE_ORDERS = True\nCAPITAL = 12345\n'
        additions = (ROOT/'stop_update/config_additions.py').read_bytes()
        result = installer.config_patch(original, additions, installer.digest(original))
        self.assertTrue(result.startswith(original))
        scope = {}
        with patch.dict('os.environ', {}, clear=True):
            exec(result, scope)
        self.assertTrue(scope['LIVE_ORDERS'])
        self.assertEqual(scope['CAPITAL'], 12345)
        self.assertFalse(scope['REGIME_STOPS_ENABLED'])
        self.assertEqual(scope['STRUCTURE_GUARD_VALIDATIONS'], {})
        self.assertEqual(installer.config_patch(result, additions, installer.digest(original)),result)

    def test_config_refuses_unknown_original_and_conflicts(self):
        for original, expected in ((b'X=1\n','unknown'),
                                   (b'X=1\n',installer.digest(b'X=1\n'))):
            with self.assertRaises(ValueError):
                installer.config_patch(original,b'X=2\n',expected)

    def test_config_refuses_modified_installed_additions(self):
        original = b'X=1\n'
        result = installer.config_patch(original,b'Y=2\n',installer.digest(original))
        with self.assertRaises(ValueError):
            installer.config_patch(result+b'Z=3\n',b'Y=2\n',installer.digest(original))

    def test_mcx_entry_and_other_untouched_methods_preserved(self):
        def methods(path):
            tree = ast.parse((ROOT/path).read_text())
            cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='AutoExecutor')
            return {n.name:ast.dump(n,include_attributes=False) for n in cls.body
                    if isinstance(n,ast.FunctionDef)}
        before = methods('mcx_live_update/auto_executor.py')
        after = methods('stop_update/auto_executor.py')
        changed = {'__init__','tighten_stop','update_structure','check_exits','_close_position','_monitor_loop'}
        for name in before.keys()-changed:
            self.assertEqual(before[name],after[name],name)
        source = (ROOT/'stop_update/auto_executor.py').read_text()
        self.assertEqual(source.count('market_session_gate.entry_block'),2)
        self.assertIn('portfolio_capacity.affordable_lots',source)
        self.assertIn('tokens[offset:offset + 50]',source)
        self.assertIn("(23,25)",source)
        self.assertIn('and not is_mcx',source)

    def test_composed_exit_matches_tested_adapter(self):
        def method(path):
            tree = ast.parse((ROOT/path).read_text())
            return next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='check_exits')
        # MCX-specific exclusions must remain; the baseline already contains them.
        self.assertEqual(ast.dump(method('kadima/auto_executor.py')),
                         ast.dump(method('stop_update/auto_executor.py')))
