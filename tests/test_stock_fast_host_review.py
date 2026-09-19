import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'tools' / 'stock_fast_host_review.py'
spec = importlib.util.spec_from_file_location('stock_fast_host_review_tested', PATH)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
NOW = datetime(2026, 9, 19, 6, 2, tzinfo=timezone.utc)


class HealthTests(unittest.TestCase):
    def test_unreachable_api_is_not_a_missing_health_record(self):
        self.assertEqual(host.api_observation(None,NOW)['reconciliation']['state'],'API_UNAVAILABLE')
        self.assertEqual(host.api_observation({},NOW)['news']['state'],'SYSTEM_SCHEMA_UNAVAILABLE')

    def test_absent_does_not_mean_failed(self):
        record = host.health_record(None, NOW, 120)
        self.assertEqual(record['state'], 'NOT_PUBLISHED')
        self.assertIsNone(record['ok'])
        self.assertIsNone(record['fresh'])

    def test_explicit_false_remains_unhealthy(self):
        record = host.health_record({'ok': False, 'ts': NOW.isoformat()}, NOW, 120)
        self.assertEqual(record['state'], 'UNHEALTHY')
        self.assertFalse(record['ok'])
        self.assertTrue(record['fresh'])

    def test_old_success_never_becomes_fresh(self):
        record = host.health_record({'ok': True, 'ts': (NOW-timedelta(minutes=8)).isoformat()}, NOW, 120)
        self.assertEqual(record['state'], 'HEALTHY_STALE')
        self.assertFalse(record['fresh'])

    def test_future_and_missing_time_are_not_fresh(self):
        for stamp in (None, '2026-09-19T11:30:00', 'bad', (NOW+timedelta(seconds=1)).isoformat()):
            self.assertIsNot(host.health_record({'ok': True, 'ts': stamp}, NOW, 120)['fresh'], True)

    def test_non_boolean_success_cannot_pass(self):
        for value in (1, 'true', [], {}):
            record = host.health_record({'ok': value, 'ts': NOW.isoformat()}, NOW, 120)
            self.assertIsNone(record['ok'])
            self.assertEqual(record['state'], 'INVALID_OK')

    def test_reason_bodies_do_not_leak(self):
        record = host.health_record(dict(ok=False, ts=NOW.isoformat(), reason='mysecretapikey',
            positions=[{'reason': 'external_pending_orders'}, {'reason': {'token': 'LEAK'}}],
            errors={'announcements':'Bearer SECRET', 'private_token':'LEAK'}), NOW, 120)
        self.assertNotIn('SECRET', json.dumps(record))
        self.assertNotIn('LEAK', json.dumps(record))
        self.assertNotIn('mysecretapikey', json.dumps(record))
        self.assertIn('external_pending_orders', record['reasons'])
        self.assertEqual(record['error_categories'], ['announcements'])

    def test_saved_zero_counts_do_not_verify_broker_flat(self):
        observed = host.api_observation({'positions': [], 'config': {}, 'system': {}}, NOW)
        self.assertEqual(observed['api_position_count'], 0)
        self.assertFalse(observed['broker_flat_verified'])
        self.assertEqual(observed['reconciliation']['state'], 'NOT_PUBLISHED')

    def test_null_positions_not_interpreted_as_zero(self):
        self.assertIsNone(host.api_observation({'positions': None}, NOW)['api_position_count'])

    def test_disabled_rollout_does_not_hide_missing_health(self):
        observed = host.api_observation({'config': {'rollout_enabled': False}, 'system': {}}, NOW)
        self.assertFalse(observed['config']['rollout_enabled'])
        self.assertIsNone(observed['config']['auto_trade_enabled'])
        self.assertIsNone(observed['reconciliation']['ok'])


class SourceAndProcessTests(unittest.TestCase):
    def test_unknown_files_keep_unchanged_not_automatically_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'run.py').write_text('custom code')
            result = host.source_inventory(root)
            self.assertEqual(result['run.py']['state'], 'UNRECOGNIZED_KEEP_UNCHANGED')
            self.assertEqual(result['kadima/stock_fast.py']['state'], 'MISSING')
            self.assertEqual((root/'run.py').read_text(), 'custom code')

    def test_known_hash_classification_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'run.py').write_text('pass\n')
            digest = host.source_inventory(root)['run.py']['blob']
            with patch.dict(host.SOURCE_HASHES['run.py'], {digest: 'BASE_ENTRYPOINT'}):
                self.assertEqual(host.source_inventory(root)['run.py']['state'], 'BASE_ENTRYPOINT')

    def test_symlink_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'original').write_text('pass\n')
            (root/'run.py').symlink_to(root/'original')
            self.assertEqual(host.source_inventory(root)['run.py']['state'], 'SYMLINK_REVIEW_REQUIRED')

    def test_command_identity_does_not_print_arguments(self):
        root = Path('/home/ubuntu/kadima-py')
        result = host.script_identity(['/x/python3.11','-u','run.py','--token','SECRET'], root, root)
        self.assertEqual(result, 'run.py')
        self.assertNotIn('SECRET', result)

    def test_module_and_unknown_launchers_require_review(self):
        root = Path('/home/ubuntu/kadima-py')
        self.assertEqual(host.script_identity(['python3','-m','privateSECRET'], root, root), 'MODULE_OR_CUSTOM_ENTRYPOINT')
        self.assertEqual(host.script_identity(['node','bridge.js'], root, root), 'NON_PYTHON_OR_UNKNOWN')
        self.assertEqual(host.script_identity(['python3','/tmp/SECRET.py'], root, root), 'OUTSIDE_TARGET_REVIEW_REQUIRED')

    def test_selected_process_environment_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); proc=root/'proc'; item=proc/'123'; item.mkdir(parents=True)
            (item/'environ').write_bytes(b'ANGEL_SECRET=SECRET\0KADIMA_FAST_MOMENTUM_LIVE=0\0')
            (item/'cmdline').write_bytes(b'python3\0run.py\0--token\0SECRET\0')
            (item/'cwd').symlink_to(root)
            public,_ = host.process_review('123',root,proc)
            self.assertNotIn('SECRET',json.dumps(public))
            self.assertFalse(public['flags']['KADIMA_FAST_MOMENTUM_LIVE'])
            self.assertIsNone(public['flags']['KADIMA_CELESTIAL_ENABLED'])
            self.assertEqual(public['script'],'run.py')

    def test_missing_db_does_not_create_empty_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertEqual(host.local_counts(root)['state'],'UNAVAILABLE')
            self.assertFalse((root/'kadima.db').exists())

    def test_missing_ledger_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with sqlite3.connect(root/'kadima.db') as db:
                db.execute('CREATE TABLE positions(status TEXT)')
            result=host.local_counts(root)
            self.assertEqual(result['open_positions'],0)
            self.assertIsNone(result['pending_intents'])
            self.assertEqual(result['state'],'SCHEMA_INCOMPLETE')


class TransportTests(unittest.TestCase):
    def test_non_allowlisted_url_rejected_before_request(self):
        with patch.object(host.urllib.request,'build_opener') as opener:
            for name in ('https://evil.example', 'order_book', 'resume'):
                with self.assertRaises(ValueError): host.read_json(name,'SECRET')
            opener.assert_not_called()

    def test_only_get_uses_loopback_and_redirects_disabled(self):
        response=Mock(status=200)
        response.read.return_value=b'{"ok":true}'
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        opener=Mock(); opener.open.return_value=response
        with patch.object(host.urllib.request,'build_opener',return_value=opener) as build:
            meta,data=host.read_json('celestial','SECRET')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'GET')
        self.assertEqual(request.full_url,host.URLS['celestial'])
        self.assertIsNone(request.data)
        self.assertNotIn('SECRET',json.dumps(meta))
        self.assertTrue(data['ok'])
        self.assertIsInstance(build.call_args.args[1],host.NoRedirect)
        self.assertIsNone(host.NoRedirect().redirect_request(None,None,302,'',{},'http://evil.example'))

    def test_http_response_error_body_not_printed(self):
        exception=host.urllib.error.HTTPError(host.URLS['stock_fast'],401,'SECRET',{},None)
        with patch.object(host.urllib.request,'build_opener') as build:
            build.return_value.open.side_effect=exception
            meta,data=host.read_json('stock_fast','SECRET')
        self.assertEqual(meta['http'],401); self.assertIsNone(data)
        self.assertNotIn('SECRET',json.dumps(meta))

    def test_complete_collection_does_not_forward_raw_payloads(self):
        units={n:dict(ActiveState='active',SubState='running',MainPID='0') for n in host.UNITS}
        api=dict(ts=NOW.isoformat(),config={'auto_trade_enabled':False,'secret':'PRIVATE'},
                 system={'reconciliation':None,'news':None},positions=[])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=root/'dashboard.json'
            config.write_text(json.dumps({'api_token':'SECRET','other':'PRIVATE'}))
            with patch.object(host,'services',return_value=units), patch.object(host,'read_json') as read:
                read.side_effect=[({'http':200}, {'market_open':False,'secret':'SECRET'}),
                                  ({'http':200},api),({'http':404},None)]
                result=host.collect(root,config)
        self.assertEqual(read.call_count,3)
        self.assertNotIn('SECRET',json.dumps(result)); self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertFalse(result['api']['broker_flat_verified'])
        self.assertFalse(result['live_settings_changed']); self.assertFalse(result['deployment_performed'])
        self.assertIn('NOT_PUBLISHED',host.summary(result))
        self.assertEqual(result['endpoints']['stock_fast']['http'],404)

    def test_missing_auth_prevents_protected_gets(self):
        units={n:dict(MainPID='0') for n in host.UNITS}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(host,'services',return_value=units), patch.object(host,'read_json',return_value=({'http':200},{})) as read:
                result=host.collect(root,root/'missing.json')
        read.assert_called_once_with('health')
        self.assertEqual(result['endpoints']['celestial']['error'],'LOCAL_AUTH_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
