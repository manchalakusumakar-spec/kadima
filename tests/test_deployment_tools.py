"""Deployment boundaries: unknown versions stop before mutation; inventory is read-only."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


class DeploymentTools(unittest.TestCase):
    def test_standalone_report_survives_missing_manifest_and_checkout(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        with tempfile.TemporaryDirectory() as tmp, patch.object(report,'run',return_value=''), \
                patch.object(report,'live_checks',return_value={'celestial_api':False}), \
                patch.object(report,'PACKAGE',Path(tmp)/'standalone'):
            target=Path(tmp)/'missing'
            result=report.report(target)
            self.assertFalse(target.exists())
            self.assertEqual(result['source_comparison'],'unavailable_inventory_only')
            self.assertTrue(all(x['matches_release'] is None for x in result['source'].values()))
            self.assertEqual(result['celestial_control_blockers'],['authenticated_control_status_unavailable'])

    def test_inventory_of_itself_is_not_independent_release_verification(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        with patch.object(report,'run',return_value=''),patch.object(report,'live_checks',return_value={}):
            result=report.report(ROOT)
        self.assertEqual(result['source_comparison'],'unavailable_inventory_only')
        self.assertTrue(all(x['matches_release'] is None for x in result['source'].values()))

    def test_control_flags_require_booleans_and_health_requires_recent_timestamps(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        now=datetime.now(timezone.utc)
        for age,valid in ((10,True),(600,False),(-60,False)):
            stamp=(now-timedelta(seconds=age)).isoformat()
            result=report.api_checks({'config':{'auto_trade_enabled':'true','broker_orders_enabled':1,
                'score_threshold':True},'system':{'news':{'ok':True,'ts':stamp},
                'reconciliation':{'ok':True,'ts':stamp}}})
            self.assertIsNone(result['auto_trade_enabled'])
            self.assertIsNone(result['broker_orders_enabled'])
            self.assertIsNone(result['score_threshold'])
            self.assertEqual(result['news_fresh'],valid)
            self.assertEqual(result['reconciliation_fresh'],valid)
        self.assertIsNone(report.age_seconds('2026-09-18T08:00:00'))

    def test_control_blockers_distinguish_off_unknown_kill_and_stale_health(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        checks=dict(celestial_api=True,rollout_enabled=False,auto_trade_enabled=None,
                    broker_orders_enabled=True,kill_latched=True,broker_reconciliation_ok=True,
                    reconciliation_fresh=False,news_ok=False,news_fresh=False)
        self.assertEqual(report.celestial_blockers(checks),['rollout_enabled_off',
            'auto_trade_enabled_unknown','kill_latched','reconciliation_stale_or_unknown',
            'news_not_healthy','news_stale_or_unknown'])
        checks.update(rollout_enabled=True,auto_trade_enabled=True,kill_latched=False,
                      reconciliation_fresh=True,news_ok=True,news_fresh=True)
        self.assertEqual(report.celestial_blockers(checks),[])

    def test_authenticated_get_failure_does_not_echo_credentials_or_broker_data(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        secret='SECRET_SENTINEL'
        error=urllib.error.HTTPError('local',401,secret,{},None)
        with patch.object(report.Path,'read_text',return_value=json.dumps({'api_token':secret})), \
                patch.object(report,'local_json',side_effect=[{'ok':True},error]) as fetch:
            result=report.live_checks()
        self.assertEqual(result['control_api_http_status'],401)
        self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(fetch.call_args.args,('http://127.0.0.1:8081/api/kadima/top20',secret))

    def test_missing_credentials_do_not_attempt_authenticated_request(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        with patch.object(report.Path,'read_text',side_effect=FileNotFoundError), \
                patch.object(report,'local_json',return_value={'ok':True}) as fetch:
            result=report.live_checks()
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(result['control_api_error'],'dashboard_credentials_unavailable')

    def test_local_reader_only_gets_and_never_follows_auth_redirect(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        with patch.object(report.urllib.request,'build_opener') as build:
            build.return_value.open.return_value=io.BytesIO(b'{"ok":true}')
            self.assertEqual(report.local_json('http://127.0.0.1:8081/api/kadima/top20','SECRET'),{'ok':True})
            request=build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(),'GET')
            self.assertIsNone(request.data)
            self.assertEqual(build.call_args.args[0].proxies,{})
            self.assertIsNone(build.call_args.args[1].redirect_request(request,None,302,'',{},'https://other'))

    def test_installer_ships_report_with_reviewed_previous_hash(self):
        manifest=json.loads((ROOT/'executor_fix/manifest.json').read_text())
        self.assertIn(None,manifest['tools/deployment_report.py'])
        self.assertIn('7db4dcbbccd7c9d709161439715cf1d0a68c24ba',manifest['tools/deployment_report.py'])

    def test_unset_worker_health_does_not_mark_reachable_api_down(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        for system in (None,{}, {'reconciliation':None,'news':None}):
            with self.subTest(system=system):
                result=report.api_checks({'config':{'rollout_enabled':False,'score_threshold':65},'system':system})
                self.assertTrue(result['celestial_api'])
                self.assertFalse(result['broker_reconciliation_ok'])
                self.assertFalse(result['news_ok'])
                self.assertFalse(result['rollout_enabled'])
                self.assertEqual(result['score_threshold'],65)

    def test_snapshot_requires_config_and_explicit_worker_success(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        for invalid in (None,[],{}, {'config':None}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):report.api_checks(invalid)
        result=report.api_checks({'config':{},'system':{'reconciliation':{'ok':True},'news':{'ok':'true'}}})
        self.assertTrue(result['broker_reconciliation_ok'])
        self.assertFalse(result['news_ok'])

    def test_backend_restart_delay_then_ready(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',side_effect=[
                urllib.error.URLError('connection refused'),io.BytesIO(b'{"dashboard_limit":20}')]) as fetch, \
                patch.object(upgrade.time,'sleep'):
            self.assertEqual(upgrade.wait_for_backend('request')['dashboard_limit'],20)
            self.assertEqual(fetch.call_count,2)

    def test_backend_auth_failure_is_not_retried(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',side_effect=
                urllib.error.HTTPError('local',401,'unauthorized',{},None)) as fetch:
            with self.assertRaises(urllib.error.HTTPError):upgrade.wait_for_backend('request')
            self.assertEqual(fetch.call_count,1)

    def test_backend_restart_wait_is_bounded(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',side_effect=urllib.error.URLError('refused')), \
                patch.object(upgrade.time,'monotonic',side_effect=[0,31]),patch.object(upgrade.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'ready'):upgrade.wait_for_backend('request')
            sleep.assert_not_called()

    def test_wrong_backend_schema_refused(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',return_value=io.BytesIO(b'{"ok":true}')):
            with self.assertRaisesRegex(ValueError,'backend required'):upgrade.wait_for_backend('request')

    def test_unknown_dashboard_source_refused_without_overwrite(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'gateway.py';target.write_text('unreviewed live edit')
            with self.assertRaisesRegex(ValueError,'Unreviewed'):
                upgrade.check_versions({'gateway.py':target},{'gateway.py':[]})
            self.assertEqual(target.read_text(),'unreviewed live edit')

    def test_reviewed_dashboard_hash_is_accepted(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'gateway.py';target.write_text('reviewed old source')
            upgrade.check_versions({'gateway.py':target},{'gateway.py':[upgrade.blob(target)]})

    def test_dashboard_symlink_refused(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'actual';source.write_text('old source')
            target=Path(tmp)/'gateway.py';target.symlink_to(source)
            with self.assertRaisesRegex(ValueError,'regular'):
                upgrade.check_versions({'gateway.py':target},{'gateway.py':[upgrade.blob(source)]})

    def test_inventory_does_not_create_missing_live_checkout_or_echo_raw_output(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        with tempfile.TemporaryDirectory() as tmp, patch.object(report,'run',return_value='DO_NOT_PRINT_SECRET\nActiveState=inactive\nMainPID=0\n'):
            target=Path(tmp)/'nonexistent'
            output=report.report(target)
            self.assertFalse(target.exists())
            self.assertTrue(output['read_only'])
            self.assertNotIn('DO_NOT_PRINT_SECRET',json.dumps(output))
            self.assertTrue(all(v['installed'] is None for v in output['source'].values()))


if __name__=='__main__':unittest.main()
