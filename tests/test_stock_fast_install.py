"""Deployment fixtures only. No AWS, broker, service or network operations."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1]/'stock_fast_update/install.py'
spec = importlib.util.spec_from_file_location('stock_fast_installer_tests', PATH)
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.owner = os.getuid(), os.getgid()

    def file(self, name, data):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class SourceTests(Fixture):
    def test_git_blob_hash(self):
        self.assertEqual(installer.digest(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')

    def test_symlink_is_rejected(self):
        target = self.file('original', b'x')
        link = self.root/'link'
        link.symlink_to(target)
        with self.assertRaises(installer.Blocked): installer.read_file(link)

    def test_symlink_parent_is_rejected(self):
        target = self.file('original/x', b'x')
        (self.root/'link').symlink_to(target.parent, target_is_directory=True)
        with self.assertRaises(installer.Blocked): installer.read_file(self.root/'link/x')

    def test_missing_is_not_empty(self):
        self.assertIsNone(installer.read_file(self.root/'missing', missing=True))
        with self.assertRaises(installer.Blocked): installer.read_file(self.root/'missing')

    def test_oversize_is_rejected(self):
        with self.assertRaises(installer.Blocked): installer.read_file(self.file('large', b'x'*2_000_001))

    def test_fingerprint_detects_concurrent_change(self):
        path = self.file('run.py', b'old')
        path.write_bytes(b'new')
        with self.assertRaises(installer.Blocked): installer.check_fingerprints({path:b'old'})

    def test_unknown_helper_not_executed(self):
        path = self.file('helper.py', b'raise AssertionError("must not execute")')
        with self.assertRaises(installer.Blocked): installer.load_helper(path, 'bad')

    def test_recursive_import_detection(self):
        self.file('pkg/kadima/a.py', b'')
        self.file('pkg/kadima/b.py', b'')
        self.file('pkg/kadima/c.py', b'')
        source = b'from . import a\nfrom kadima.b import Type\ndef f():\n import kadima.c\nimport os\n'
        self.assertEqual(installer.local_modules(source,self.root/'pkg'), {'kadima/a.py','kadima/b.py','kadima/c.py'})

    def test_atomic_replacement_preserves_mode_and_owner(self):
        path = self.file('run.py', b'old'); path.chmod(0o640)
        item = installer.change_for(path, b'new', self.owner)
        installer.write_atomic(item, item.replacement)
        self.assertEqual(path.read_bytes(), b'new')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(path.stat().st_uid, self.owner[0])
        self.assertEqual(list(self.root.glob('.stock-fast-*')), [])


class PlanTests(Fixture):
    def setUp(self):
        super().setUp()
        self.package, self.target = self.root/'release', self.root/'live'
        self.base = b'pass\n'; self.incoming = b'from kadima import dependency, config\n'
        self.limiter = b'# exact FIFO fixture\n'
        for root in ('release', 'live'):
            self.file(root+'/kadima/dependency.py', b'import os\n')
            self.file(root+'/kadima/config.py', b'VALUE=1\n')
            self.file(root+'/kadima/rate_limiter.py', self.limiter)
        self.file('release/run.py', self.incoming)
        self.file('live/run.py', self.base)
        self.file('release/kadima/new.py', b'pass\n')
        self.stack.enter_context(patch.object(installer,'FILES',{'run.py':installer.digest(self.base),'kadima/new.py':None}))
        self.stack.enter_context(patch.object(installer,'FIFO',installer.digest(self.limiter)))

    def plan(self):
        return installer.backend_plan(self.package,self.target,self.owner)

    def test_plan_changes_only_declared_files(self):
        changes, pins = self.plan()
        self.assertEqual({x.path.relative_to(self.target).as_posix() for x in changes}, {'run.py','kadima/new.py'})
        self.assertIn(self.target/'kadima/dependency.py',pins)
        self.assertIn(self.target/'kadima/rate_limiter.py',pins)
        self.assertEqual((self.target/'run.py').read_bytes(),self.base)
        self.assertFalse((self.target/'kadima/new.py').exists())

    def test_unknown_base_rejected(self):
        (self.target/'run.py').write_bytes(b'custom=1\n')
        with self.assertRaisesRegex(installer.Blocked,'unreviewed_target'): self.plan()

    def test_unknown_dependency_rejected_not_replaced(self):
        p=self.target/'kadima/dependency.py'; p.write_bytes(b'custom=2\n')
        with self.assertRaisesRegex(installer.Blocked,'unreviewed_dependency'): self.plan()
        self.assertEqual(p.read_bytes(),b'custom=2\n')

    def test_deployed_config_is_preserved_and_pinned(self):
        p=self.target/'kadima/config.py'; p.write_bytes(b'VALUE=999\n')
        changes,pins=self.plan()
        self.assertNotIn(p,[c.path for c in changes]); self.assertEqual(pins[p],b'VALUE=999\n')

    def test_idempotent_when_known_files_match(self):
        for path in ('run.py','kadima/new.py'):
            (self.target/path).write_bytes((self.package/path).read_bytes())
        self.assertEqual(self.plan()[0],[])

    def test_fifo_must_not_be_downgraded(self):
        (self.target/'kadima/rate_limiter.py').write_bytes(b'# old limiter\n')
        with self.assertRaisesRegex(installer.Blocked,'fifo'): self.plan()

    def test_same_release_directory_rejected(self):
        with self.assertRaises(installer.Blocked): installer.backend_plan(self.target,self.target,self.owner)


class ExposureTests(Fixture):
    def db(self, tables=True):
        path=self.root/'local.db'
        with sqlite3.connect(path) as c:
            if tables:
                c.execute('CREATE TABLE positions(status TEXT)')
                c.execute('CREATE TABLE execution_orders(state TEXT)')
        return path

    def test_missing_database_not_created(self):
        path=self.root/'missing.db'
        with self.assertRaises(installer.Blocked): installer.local_flat(path)
        self.assertFalse(path.exists())

    def test_missing_table_is_unknown_not_flat(self):
        with self.assertRaisesRegex(installer.Blocked,'schema_unknown'): installer.local_flat(self.db(False))

    def test_empty_confirmed_tables_pass(self):
        installer.local_flat(self.db())

    def test_pending_or_null_status_blocks(self):
        for table, column in (('positions','status'),('execution_orders','state')):
            for value in ('open',None):
                with self.subTest(table=table,value=value):
                    path=self.root/f'{table}-{value}.db'
                    with sqlite3.connect(path) as c:
                        c.execute('CREATE TABLE positions(status TEXT)')
                        c.execute('CREATE TABLE execution_orders(state TEXT)')
                        c.execute('INSERT INTO '+table+' VALUES (?)',(value,))
                    with self.assertRaises(installer.Blocked): installer.local_flat(path)

    def test_stored_live_intent_blocks(self):
        path=self.db()
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE stock_fast_controls(id INTEGER, payload TEXT)')
            c.execute('INSERT INTO stock_fast_controls VALUES (1,?)',(json.dumps({'live_enabled':True}),))
        with self.assertRaisesRegex(installer.Blocked,'persistent_live'): installer.local_flat(path)

    def status(self):
        return dict(ts=datetime.now(timezone.utc).isoformat(),positions=[],config=dict(
            rollout_enabled=False,auto_trade_enabled=False,kill_latched=False))

    def test_not_published_health_does_not_block_installation_itself(self):
        installer.paused_status(self.status())

    def test_armed_global_or_unknown_config_blocks(self):
        for field in ('rollout_enabled','auto_trade_enabled','kill_latched'):
            for value in (None,True,'false',0):
                value_=self.status(); value_['config'][field]=value
                with self.assertRaises(installer.Blocked): installer.paused_status(value_)

    def test_stale_control_snapshot_blocks(self):
        value=self.status(); value['ts']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        with self.assertRaises(installer.Blocked): installer.paused_status(value)

    def test_local_api_positions_unknown_or_present_blocks(self):
        for positions in (None,{},[{'id':1}]):
            value=self.status(); value['positions']=positions
            with self.assertRaises(installer.Blocked): installer.paused_status(value)

    def test_redirect_and_unknown_endpoint_rejected_without_network(self):
        self.assertIsNone(installer.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com'))
        with self.assertRaises(installer.Blocked): installer.local_api('/api/order/buy','secret')


class TransactionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.path=self.file('run.py',b'old')
        self.other=self.file('config.py',b'keep configuration')
        self.items=[installer.change_for(self.path,b'new',self.owner),
                    installer.change_for(self.root/'new.py',b'new module',self.owner)]
        self.backup=self.root/'backup'; self.backup.mkdir()
        self.calls=[]
        def runner(args,**kwargs):
            self.calls.append(args)
            return SimpleNamespace(stdout='ActiveState=inactive\nMainPID=0\n')
        self.runner=runner
        self.flat=Mock(); self.source=Mock(); self.imports=Mock(); self.post=Mock()
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def apply(self,runner=None):
        installer.install_transaction(self.items,['scanner','engine'],self.backup,self.flat,
            self.source,self.imports,self.post,runner or self.runner)

    def test_success_is_additive_and_backs_up(self):
        self.apply()
        self.assertEqual(self.path.read_bytes(),b'new')
        self.assertEqual(self.other.read_bytes(),b'keep configuration')
        self.assertEqual((self.backup/'0').read_bytes(),b'old')
        self.assertEqual(self.calls[0],['systemctl','stop','scanner','engine'])
        self.assertEqual(self.calls[-1],['systemctl','start','engine','scanner'])
        self.flat.assert_called_once(); self.imports.assert_called_once(); self.post.assert_called_once()

    def test_failed_second_broker_check_changes_no_source(self):
        self.flat.side_effect=installer.Blocked('broker_positions_not_flat')
        with self.assertRaises(installer.Blocked): self.apply()
        self.assertEqual(self.path.read_bytes(),b'old')
        self.assertFalse((self.root/'new.py').exists()); self.imports.assert_not_called()

    def test_import_failure_rolls_back_before_restart(self):
        self.imports.side_effect=RuntimeError('fixture')
        with self.assertRaises(RuntimeError): self.apply()
        self.assertEqual(self.path.read_bytes(),b'old')
        self.assertFalse((self.root/'new.py').exists()); self.post.assert_not_called()

    def test_restart_failure_never_rolls_back_live_source(self):
        def runner(args,**kwargs):
            result=self.runner(args,**kwargs)
            if args[1]=='start': raise RuntimeError('fixture')
            return result
        with self.assertRaises(RuntimeError): self.apply(runner)
        self.assertEqual(self.path.read_bytes(),b'new')
        self.assertTrue((self.root/'new.py').exists())

    def test_post_check_failure_retains_source(self):
        self.post.side_effect=RuntimeError('fixture')
        with self.assertRaises(RuntimeError): self.apply()
        self.assertEqual(self.path.read_bytes(),b'new')

    def test_still_running_service_cannot_be_overwritten(self):
        def runner(args,**kwargs):
            self.calls.append(args)
            return SimpleNamespace(stdout='ActiveState=active\nMainPID=12\n')
        with self.assertRaisesRegex(installer.Blocked,'fully_stopped'): self.apply(runner)
        self.assertEqual(self.path.read_bytes(),b'old'); self.flat.assert_not_called()

    def test_changed_file_after_stop_aborts(self):
        self.source.side_effect=installer.Blocked('changed_source')
        with self.assertRaises(installer.Blocked): self.apply()
        self.assertEqual(self.path.read_bytes(),b'old')

    def test_copy_failure_rolls_back_completed_copies(self):
        original=installer.write_atomic
        def write(item,raw):
            if item.path.name=='new.py': raise OSError('fixture')
            return original(item,raw)
        with patch.object(installer,'write_atomic',side_effect=write):
            with self.assertRaises(OSError): self.apply()
        self.assertEqual(self.path.read_bytes(),b'old')
        self.assertFalse((self.root/'new.py').exists())

    def test_mismatched_original_aborts_before_service_stop(self):
        self.path.write_bytes(b'concurrent change')
        with self.assertRaises(installer.Blocked): self.apply()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.path.read_bytes(),b'concurrent change')



class HelperTests(unittest.TestCase):
    def test_broker_code_requires_data_key(self):
        source = """for name in ('position', 'orderBook'):
        r = {'status':True}
        rows = r.get('data')
"""
        code = installer.broker_preflight_code(source)
        with self.assertRaises(RuntimeError): exec(code, {})

    def test_unknown_preflight_shape_is_rejected(self):
        with self.assertRaises(installer.Blocked): installer.broker_preflight_code('pass')

    def test_gateway_requires_original_auth_handlers(self):
        source = """def authenticated(request): return True
def origin_check(request): return True
def read_data(name,request): authenticated(request)
async def celestial_write(path,request):
 origin_check(request)
 authenticated(request)
"""
        installer.verify_gateway(source+'\ndef other(): return 2\n', source)
        with self.assertRaises(installer.Blocked):
            installer.verify_gateway(source.replace('\n origin_check(request)', '\n pass # skipped'), source)

    def test_duplicate_gateway_handler_is_rejected(self):
        source = 'def authenticated(request): pass\ndef authenticated(request): pass'
        with self.assertRaises(installer.Blocked): installer.verify_gateway(source,source)

if __name__=='__main__':
    unittest.main()
