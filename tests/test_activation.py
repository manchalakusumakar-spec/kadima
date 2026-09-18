"""Guarded activation regressions. No live service, API, login or order calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
import urllib.error
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('activation',ROOT/'tools/activate_execution.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


class ActivationTests(unittest.TestCase):
    def test_config_refuses_kill_unknown_broker_changed_score_and_running_auto(self):
        cfg=dict(kill_latched=False,broker_orders_enabled=True,auto_trade_enabled=False,score_threshold=65)
        a.check_config(cfg)
        for changes in ({'kill_latched':True},{'kill_latched':None},{'broker_orders_enabled':False},
                        {'broker_orders_enabled':'true'},{'score_threshold':70},{'score_threshold':True},
                        {'auto_trade_enabled':True},{'auto_trade_enabled':None}):
            with self.subTest(changes=changes),self.assertRaises(a.Blocked):a.check_config(cfg|changes)

    def test_preflight_failure_before_stop_changes_nothing(self):
        with patch.object(a,'preflight',side_effect=a.Blocked('exposure')),patch.object(a,'command') as run:
            with self.assertRaises(a.Blocked):a.activate_services(Mock(),None,{})
        run.assert_not_called()

    def test_post_stop_exposure_restarts_original_without_writing_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'flags'
            with patch.object(a,'preflight',side_effect=[None,a.Blocked('exposure')]), \
                    patch.object(a,'source_check'),patch.object(a,'command') as run:
                with self.assertRaises(a.Blocked):a.activate_services(Mock(),None,{path:'ON'})
            self.assertFalse(path.exists())
            self.assertIn(['systemctl','start',*a.UNITS],[x.args[0] for x in run.call_args_list])

    def test_failure_before_restart_rolls_back_but_restart_failure_retains_mode(self):
        for stage in ('reload','start','health',None):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'flags';failed=[False]
                def command(args):
                    match={'reload':'daemon-reload','start':'start','health':'is-active'}.get(stage)
                    if len(args)>1 and args[1]==match and not failed[0]:
                        failed[0]=True
                        raise RuntimeError('failure')
                with patch.object(a,'preflight'),patch.object(a,'source_check'),patch.object(a,'command',side_effect=command):
                    if stage:
                        with self.assertRaises(RuntimeError):a.activate_services(Mock(),None,{path:'ON'})
                    else:a.activate_services(Mock(),None,{path:'ON'})
                self.assertEqual(path.exists(),stage!='reload')

    def test_existing_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'ENVFILE',Path(tmp)/'env'), \
                patch.object(a,'SYSTEMD',Path(tmp)/'systemd'):
            files=a.planned_files(True)
            self.assertIn('KADIMA_FAST_MOMENTUM_LIVE=1',files[a.ENVFILE])
            self.assertEqual(len(files),3)
            a.ENVFILE.write_text('USER_DEFINED=1\n')
            with self.assertRaises(a.Blocked):a.planned_files(True)
            self.assertEqual(a.ENVFILE.read_text(),'USER_DEFINED=1\n')

    def test_source_mismatch_blocks_before_process_or_api_access(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'REPO',Path(tmp)):
            with self.assertRaisesRegex(a.Blocked,'source_mismatch'):a.source_check(Mock())

    def test_resume_uses_checked_endpoint_without_order_or_risk_payload(self):
        reporter=types.SimpleNamespace(NoRedirect=lambda:object())
        with patch.object(a.urllib.request,'build_opener') as build:
            build.return_value.open.return_value=io.BytesIO(b'{"auto_trade_enabled":true}')
            a.resume('SECRET',reporter)
            req=build.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url,'http://127.0.0.1:8081/api/kadima/resume')
            self.assertEqual(req.get_method(),'POST')
            self.assertEqual(req.data,b'{}')

    def test_resume_reports_known_reconciliation_block_but_suppresses_unknown_body(self):
        for detail in ('broker_reconciliation_required','SECRET_SENTINEL'):
            error=urllib.error.HTTPError('local',409,'SECRET',{},io.BytesIO(json.dumps({'detail':detail}).encode()))
            with patch.object(a.urllib.request,'build_opener') as build:
                build.return_value.open.side_effect=error
                with self.assertRaises(a.Blocked) as caught:a.resume('TOKEN',Mock())
                self.assertNotIn('SECRET',str(caught.exception))
                if detail.startswith('broker'):self.assertEqual(str(caught.exception),detail)

    def test_failed_broker_output_never_exposes_sdk_secrets(self):
        installer=types.SimpleNamespace(PREFLIGHT='code',failure_summary=lambda e,s:'preflight_failed')
        owner=types.SimpleNamespace(pw_uid=1000,pw_gid=1000)
        with patch.object(a,'command',side_effect=subprocess.CalledProcessError(1,['python'],output='SECRET')):
            with self.assertRaisesRegex(a.Blocked,'preflight_failed') as result:
                a.preflight(installer,('/usr/bin/python3',{},owner))
        self.assertNotIn('SECRET',str(result.exception))

    def test_stale_startup_health_never_calls_resume(self):
        cfg=dict(kill_latched=False,broker_orders_enabled=True,auto_trade_enabled=False,score_threshold=65)
        reporter=Mock()
        reporter.local_json.side_effect=[cfg,{'config':cfg}]
        reporter.api_checks.return_value=dict(rollout_enabled=True,broker_reconciliation_ok=False)
        with patch.object(a,'load_module',side_effect=[Mock(),reporter]),patch.object(a,'source_check'), \
                patch.object(a.Path,'read_text',return_value='{"api_token":"TOKEN"}'), \
                patch.object(a,'process_context'),patch.object(a,'planned_files'), \
                patch.object(a,'activate_services'),patch.object(a,'check_runtime_flags'), \
                patch.object(a.time,'monotonic',side_effect=[0,61]),patch.object(a,'resume') as resume:
            with self.assertRaisesRegex(a.Blocked,'startup_health_not_ready'):a.run_activation(True)
        resume.assert_not_called()

    def test_success_reports_missing_index_coverage_without_inventing_it(self):
        cfg=dict(kill_latched=False,broker_orders_enabled=True,auto_trade_enabled=False,score_threshold=65,
                 rollout_enabled=True)
        reporter=Mock()
        reporter.local_json.side_effect=[cfg,{'config':cfg},{'system':{'news':{'covered_symbols':['SBIN']}}}]
        reporter.api_checks.return_value=dict(rollout_enabled=True,broker_reconciliation_ok=True,
            reconciliation_fresh=True,news_ok=True,news_fresh=True)
        reporter.report.return_value={'checks':{'auto_trade_enabled':True}}
        with patch.object(a,'load_module',side_effect=[Mock(),reporter]),patch.object(a,'source_check'), \
                patch.object(a.Path,'read_text',return_value='{"api_token":"TOKEN"}'), \
                patch.object(a,'process_context'),patch.object(a,'planned_files'), \
                patch.object(a,'activate_services') as activate,patch.object(a,'check_runtime_flags'), \
                patch.object(a,'preflight') as preflight, \
                patch.object(a,'resume') as resume,contextlib.redirect_stdout(io.StringIO()) as out:
            a.run_activation(True)
        resume.assert_called_once()
        activate.assert_not_called()
        preflight.assert_called_once()
        data=json.loads(out.getvalue())
        self.assertEqual(data['index_news_coverage_missing'],list(a.INDICES))
        self.assertNotIn('READY',data['activation'])


if __name__=='__main__':unittest.main()
