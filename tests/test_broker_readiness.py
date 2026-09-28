"""Read-only broker diagnostics are independent of mutation admission."""
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('readiness_test', ROOT/'momentum_recovery/check_broker_readiness.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.prep = m.load_reviewed('prepare_maintenance.py')
        self.note = m.load_reviewed('reconcile_contract_note.py')

    def test_helpers_are_exact_pins_and_import_has_no_broker_or_service_actions(self):
        with patch.object(m.subprocess, 'run') as run:
            installer = m.load_reviewed('install.py')
            self.assertIn('run.py', installer.base.FILES)
            run.assert_not_called()

    def test_apply_flag_is_not_accepted(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(['--evidence', '/unused', '--apply'])

    def test_broker_reader_filters_output_and_uses_nonroot_disabled_child(self):
        now = datetime.now(timezone.utc).isoformat()
        payload = dict(flat=True, started_at=now, verified_at=now, position_rows=0,
                       orderBook_rows=0, secret='never print this')
        invoke = Mock(return_value=SimpleNamespace(returncode=0,
                      stdout='SDK secret output\nNOTE_BROKER '+json.dumps(payload)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            receipt = m.read_broker(self.note, '/venv/python', {'ANGEL_CLIENT_ID':'test'},
                                   SimpleNamespace(pw_uid=1000,pw_gid=1000), invoke)
        self.assertNotIn('secret', str(receipt)+out.getvalue())
        args, kw = invoke.call_args
        self.assertEqual(args[0], ['/venv/python','-B','-c',self.note.BROKER_READ])
        self.assertEqual((kw['user'],kw['group'],kw['extra_groups']), (1000,1000,[]))
        self.assertEqual(kw['env']['ORDER_PLACEMENT_ENABLED'], '0')
        self.assertTrue(kw['capture_output'])

    def test_nonzero_broker_position_remains_blocking_and_sanitized(self):
        payload = dict(flat=False, stage='position', reason='position_nonzero', error='ValueError',
                       position=dict(exchange='NFO',tradingsymbol='SYNTHETICCE',netqty=130,password='secret'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(ValueError):
            m.read_broker(self.note, '/venv/python', {}, SimpleNamespace(pw_uid=1000,pw_gid=1000),
                lambda *a,**k:SimpleNamespace(returncode=0,stdout='NOTE_BROKER '+json.dumps(payload)))
        self.assertIn('position_nonzero', out.getvalue())
        self.assertIn('130', out.getvalue())
        self.assertNotIn('secret', out.getvalue())

    def test_archive_contains_only_two_source_files_and_hash_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = {name:root/name for name in ('gateway.py','dashboard.html')}
            for name,path in sources.items():path.write_text('source for '+name)
            (root/'config.json').write_text('secret-outside-scope')
            with patch.object(m, 'UI', sources): result = m.export_ui(self.prep,parent=root)
            archive = Path(result['path'])
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
            with tarfile.open(archive) as f:
                self.assertEqual(set(f.getnames()), {'gateway.py','dashboard.html','manifest.json'})
                for name,path in sources.items():
                    self.assertEqual(f.extractfile(name).read(), path.read_bytes())
                    self.assertEqual(result['blobs'][name], m.blob(path.read_bytes()))

    def test_archive_refuses_linked_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'target').write_text('source');(root/'link').symlink_to(root/'target')
            with patch.object(m,'UI',{'gateway.py':root/'link'}),self.assertRaises(self.prep.Blocked):
                m.export_ui(self.prep,parent=root)

    def test_local_ledger_summary_cannot_modify_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'kadima.db'
            with sqlite3.connect(db) as c:
                c.executescript("CREATE TABLE positions(status TEXT); INSERT INTO positions VALUES ('exit_pending'); "
                                "CREATE TABLE execution_orders(state TEXT); INSERT INTO execution_orders VALUES ('unknown');")
            def attempt_write(connection,evidence):
                connection.execute("DELETE FROM positions")
            result=m.local_state(db,SimpleNamespace(inspect=attempt_write),{})
            self.assertEqual(result,dict(unresolved_positions=1,unresolved_orders=1,
                                         reviewed_historical_case_matches=False))
            with sqlite3.connect(db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM positions').fetchone()[0],1)


class RuntimeGuardsTests(unittest.TestCase):
    def setUp(self):
        self.identity = dict(pid='100',started_at=datetime.now(timezone.utc))
        self.state = dict(auto_trade_enabled=False,stock_live_enabled=False,kill_latched=False,
                          api_position_count=0,broker_flat_verified=False)
        self.env = dict(ANGEL_CLIENT_ID='synthetic',KADIMA_DATA_DIR='/synthetic')
        self.files = {m.TARGET/'kadima/session.py':b'session',m.TARGET/'kadima/config.py':b'config'}
        self.base = SimpleNamespace(ENGINE='kadima.service',HELPER='pin',
            backend_plan=Mock(return_value=([],self.files)),
            process=Mock(side_effect=lambda *a:('100',['/python','run.py'],self.env.copy())),
            check_fingerprints=Mock(),load_helper=Mock(return_value=SimpleNamespace(engine_interpreter=lambda *a:'/python')))
        self.prep = SimpleNamespace(TOKEN_FILE=Path('/synthetic/token'),read_regular=lambda *a:b'{"api_token":"test"}',
            verify_timer_hold=Mock(),snapshot=Mock(side_effect=lambda *a:self.state.copy()))
        self.note = SimpleNamespace(process_identity=Mock(side_effect=lambda *a:self.identity.copy()),
            account_matches=Mock(),reviewed_sources=Mock())
        self.user=SimpleNamespace(pw_uid=1000,pw_gid=1000)
        self.report=dict(broker_flat_observed=False,deployment_authorized=False)
        self.reader=Mock(return_value={'flat':True})
        for name,value in [('local_state',lambda *a:{'unresolved_positions':1}),('read_broker',self.reader)]:
            p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)

    def run_check(self):
        return m.inspect_runtime(self.prep,self.note,self.base,{'account_sha256':'test'},self.user,self.report)

    def test_empty_api_does_not_skip_broker_or_authorize_deployment(self):
        out=self.run_check()
        self.reader.assert_called_once()
        self.assertTrue(out['broker_flat_observed'])
        self.assertFalse(out['deployment_authorized'])
        self.assertFalse(out['controls']['broker_flat_verified'])
        self.assertEqual(out['local']['unresolved_positions'],1)

    def test_wrong_account_blocks_before_login(self):
        self.note.account_matches.side_effect=ValueError('wrong account')
        with self.assertRaises(ValueError):self.run_check()
        self.reader.assert_not_called()

    def test_unknown_sources_block_before_login(self):
        self.base.backend_plan.side_effect=ValueError('unknown source')
        with self.assertRaises(ValueError):self.run_check()
        self.reader.assert_not_called()

    def test_unpaused_controls_block_before_login(self):
        self.state['stock_live_enabled']=True
        with self.assertRaises(m.Blocked):self.run_check()
        self.reader.assert_not_called()

    def test_engine_restart_after_read_does_not_report_flatness(self):
        self.base.process.side_effect=[('100',['/python','run.py'],self.env),
                                      ('100',['/python','run.py'],self.env),
                                      ('101',['/python','run.py'],self.env)]
        with self.assertRaisesRegex(m.Blocked,'engine_changed'):self.run_check()
        self.assertFalse(self.report['broker_flat_observed'])


if __name__=='__main__':unittest.main()
