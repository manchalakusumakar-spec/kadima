import ast
import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from kadima import gap_live

IST=timezone(timedelta(hours=5,minutes=30))
NOW=datetime(2026,9,21,9,40,tzinfo=IST)


class HelperTests(unittest.TestCase):
    def test_shield_score_uses_existing_weights(self):
        snap=types.SimpleNamespace(vwap=100,ema_fast=103,ema_slow=101,rsi=60,
            avg_vol=1000,vol=2000,failed_breakdown=False)
        self.assertEqual(gap_live._shield_score(snap,104,True),95)

    def test_nearest_future_is_nearest_active_futstk(self):
        master=types.SimpleNamespace(rows=[
            {'exch_seg':'NFO','instrumenttype':'FUTSTK','name':'ABC',
             'expiry':'29Sep2026','token':'2','symbol':'ABCSEP','permitted_to_trade':True},
            {'exch_seg':'NFO','instrumenttype':'FUTSTK','name':'ABC',
             'expiry':'27Oct2026','token':'3','symbol':'ABCOCT','permitted_to_trade':True},
        ])
        row=gap_live._nearest_future(master,'ABC',NOW.date())
        self.assertEqual(row['token'],'2')

    def test_macro_gate_is_fail_closed_and_blocks_event_day(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'macro.json'
            path.write_text(json.dumps(dict(ts=NOW.isoformat(),economic_calendar=[
                {'date':'2026-09-21','end_date':'2026-09-21','name':'TEST'}])))
            with patch.object(gap_live,'MACRO_SNAPSHOT_PATH',str(path)):
                self.assertFalse(gap_live._macro_clear(NOW))

    def test_gap_worker_does_not_import_macro_watch_source(self):
        tree=ast.parse(Path('kadima/gap_live.py').read_text())
        imports=set()
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                imports.add((node.module or '').split('.')[-1])
            elif isinstance(node,ast.Import):
                imports.update(a.name.split('.')[-1] for a in node.names)
        self.assertNotIn('macro_watch',imports)

    def test_live_worker_has_no_direct_order_transport(self):
        tree=ast.parse(Path('kadima/gap_live.py').read_text())
        blocked_imports=set()
        blocked_calls=set()
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                blocked_imports.add((node.module or '').split('.')[-1])
            elif isinstance(node,ast.Import):
                blocked_imports.update(a.name.split('.')[-1] for a in node.names)
            elif isinstance(node,ast.Call):
                fn=node.func
                name=fn.attr if isinstance(fn,ast.Attribute) else fn.id if isinstance(fn,ast.Name) else None
                if name in {'place_limit_order','submit'}:
                    blocked_calls.add(name)
        self.assertFalse(blocked_imports & {'angel_orders','execution_ledger'})
        self.assertFalse(blocked_calls)


class InstallerCompositionTests(unittest.TestCase):
    @staticmethod
    def git_blob(raw):
        return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()

    def test_composed_executor_preserves_gap_session_and_multi_layers(self):
        raw=Path('gap_live_update/auto_executor.py').read_bytes()
        text=raw.decode()
        self.assertEqual(self.git_blob(raw),'6161c04be1266e5e9b010f67a9466c22c0fc8ef0')
        self.assertIn("kind == 'gap_and_go'",text)
        self.assertEqual(text.count('KADIMA SESSION AUTO ENTRY GUARD v1'),2)
        self.assertIn('KADIMA CAPITAL-BASED MULTI-POSITION v1',text)
        self.assertIn('portfolio_capacity.affordable_lots',text)
        self.assertNotIn('min(1, MAX_CONCURRENT_POSITIONS)',text)

    def test_gap_installer_accepts_only_reviewed_deployed_executor_lineage(self):
        spec=importlib.util.spec_from_file_location(
            'gap_live_installer_test',Path('gap_live_update/install.py'))
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.DEPLOYED_SESSION_MULTI_EXECUTOR,
                         '346f780545139367a4152725743c7275a14e8239')
        self.assertEqual(module.COMPOSED_EXECUTOR,
                         '6161c04be1266e5e9b010f67a9466c22c0fc8ef0')
        self.assertEqual(module.base.FILES['kadima/auto_executor.py'],
                         module.DEPLOYED_SESSION_MULTI_EXECUTOR)
        self.assertEqual(module.base.FILES['kadima/gap_and_go.py'],
                         (None,'b26599c469ec9e0af4dcd9503aa6965365bb4647'))
        self.assertIsNone(module.base.FILES['kadima/gap_baseline.py'])
        self.assertIsNone(module.base.FILES['kadima/gap_live.py'])
        self.assertEqual(
            module.base.digest(module.base.read_file(
                module.ROOT/'kadima/auto_executor.py')),
            module.COMPOSED_EXECUTOR)

    def test_gap_installer_accepts_loaded_or_unloaded_rollout_only_while_paused(self):
        spec=importlib.util.spec_from_file_location(
            'gap_live_installer_pause_test',Path('gap_live_update/install.py'))
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fresh=datetime.now(timezone.utc).isoformat()
        for rollout in (False,True):
            with self.subTest(rollout=rollout):
                module.base.paused_status(dict(
                    ts=fresh,positions=[],
                    config=dict(rollout_enabled=rollout,
                                auto_trade_enabled=False,kill_latched=False)))
        bad=[
            dict(rollout_enabled=True,auto_trade_enabled=True,kill_latched=False),
            dict(rollout_enabled=True,auto_trade_enabled=False,kill_latched=True),
            dict(rollout_enabled=None,auto_trade_enabled=False,kill_latched=False),
        ]
        for config in bad:
            with self.subTest(config=config),self.assertRaises(module.base.Blocked):
                module.base.paused_status(dict(ts=fresh,positions=[],config=config))
        with self.assertRaises(module.base.Blocked):
            module.base.paused_status(dict(
                ts=fresh,positions=[{'signal_id':1}],
                config=dict(rollout_enabled=True,
                            auto_trade_enabled=False,kill_latched=False)))

    def test_activation_manifest_pins_composed_executor_and_dependencies(self):
        manifest=json.loads(Path('tools/activation_source.json').read_text())
        self.assertEqual(manifest['kadima/auto_executor.py'],
                         '6161c04be1266e5e9b010f67a9466c22c0fc8ef0')
        self.assertEqual(manifest['kadima/market_session_gate.py'],
                         '5633eba4177f6ade457d6bdd32ec4e154f27e68a')
        self.assertEqual(manifest['kadima/portfolio_capacity.py'],
                         'c5d92290e608d22241afbad88cf5eb6dcd4bf22b')


if __name__=='__main__':
    unittest.main()
