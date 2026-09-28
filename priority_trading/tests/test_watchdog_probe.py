from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import urllib.error

spec = importlib.util.spec_from_file_location('watchdog_probe_test', Path('probe_watchdog.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Diagnostic(unittest.TestCase):
    def test_http_failure_captures_status_without_leaking_body(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('http://localhost', 409, 'hidden', {},
            io.BytesIO(b'{"detail":"watchdog_ownership_or_broker_readiness_unverified"}'))
        with patch.object(probe.urllib.request, 'build_opener', return_value=opener):
            code, data = probe.request('secret', '/api/kadima/watchdog', post=True)
        self.assertEqual(code, 409)
        self.assertEqual(probe.http_summary(code, data)['detail'],
                         'watchdog_ownership_or_broker_readiness_unverified')
        req = opener.open.call_args.args[0]
        self.assertEqual(req.data, b'{}')
        self.assertEqual(req.get_method(), 'POST')

    def test_unknown_error_details_and_results_are_omitted(self):
        for value in ('secret-token', {'secret':'value'}, ['secret']):
            result = probe.http_summary(500, dict(detail=value, results=['secret']))
            self.assertEqual(result, dict(http_status=500, detail='omitted'))

    def test_http_json_shape_and_nonjson_error_are_bounded(self):
        for raw in (b'[]', b'Internal Server Error', b'null', b'\xff'):
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError('http://localhost', 500, 'hidden', {}, io.BytesIO(raw))
            with patch.object(probe.urllib.request, 'build_opener', return_value=opener):
                self.assertEqual(probe.request('secret', '/api/kadima/watchdog', post=True), (500, {}))

    def test_only_expected_route_and_method_are_available(self):
        for path, post in (('/api/kadima/resume', True), ('/api/kadima/watchdog', False),
                           ('/api/kadima/config', True)):
            with self.assertRaises(probe.Blocked):
                probe.request('secret', path, post=post)

    def test_traceback_retains_code_location_and_type_not_values(self):
        lines = [dict(MESSAGE='  File "/home/secret/kadima/owned_watchdog.py", line 64, in cycle'),
                 dict(MESSAGE='TypeError: provider-secret-token'),
                 dict(MESSAGE='password=secret'), dict(MESSAGE={'secret':'value'})]
        self.assertEqual(probe.journal_summary('\n'.join(json.dumps(v) for v in lines)),
            [dict(file='owned_watchdog.py', line=64, function='cycle'), dict(error_type='TypeError')])

    def fixtures(self):
        cfg = dict(auto_trade_enabled=False, kill_latched=False, rollout_enabled=True)
        stamp = datetime.now(timezone.utc).isoformat()
        return {'config':cfg, 'top20':dict(config=cfg, positions=[], ts=stamp),
                'stock-fast':dict(controls=dict(live_enabled=False), global_auto_requested=False, ts=stamp)}

    def test_paused_snapshot_requires_empty_managed_book_and_freshness(self):
        value = self.fixtures()
        get = lambda path: (200, value[path.rsplit('/', 1)[1]])
        probe.paused(get)
        for key, field, bad in (('config','auto_trade_enabled',True),
                ('config','rollout_enabled',False), ('top20','positions',[{'id':1}]),
                ('top20','ts','2020-01-01T00:00:00+00:00')):
            value = self.fixtures(); value[key][field] = bad
            with self.assertRaises(probe.Blocked): probe.paused(get)

    def test_active_or_enabled_timer_prevents_probe(self):
        state = dict(ActiveState='inactive', MainPID='0', UnitFileState='disabled')
        with patch.object(probe, 'observe', return_value=state):
            self.assertEqual(len(probe.held_services()), len(probe.HELD))
            for field, bad in (('ActiveState','active'), ('MainPID','1'), ('UnitFileState','enabled')):
                state[field] = bad
                with self.assertRaises(probe.Blocked): probe.held_services()
                state[field] = {'ActiveState':'inactive', 'MainPID':'0', 'UnitFileState':'disabled'}[field]

    def test_unknown_exceptions_do_not_leak_credentials(self):
        output = io.StringIO()
        with patch.object(probe, 'probe', side_effect=ValueError('secret-token')), \
             patch.object(probe.subprocess, 'run', side_effect=OSError), patch('sys.stdout', output):
            self.assertEqual(probe.main(), 1)
        self.assertNotIn('secret-token', output.getvalue())
        self.assertFalse(json.loads(output.getvalue())['watchdog_endpoint_called'])


if __name__ == '__main__':
    unittest.main()
