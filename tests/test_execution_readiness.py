import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('readiness_report',
    Path(__file__).resolve().parents[1] / 'tools' / 'execution_readiness.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)
NOW = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)


class ExecutionReadinessTests(unittest.TestCase):
    def test_flags_exclude_credentials(self):
        data = b'ANGEL_PASSWORD=secret\0KADIMA_CELESTIAL_ENABLED=0\0API_TOKEN=secret\0'
        self.assertEqual(report.process_flags(data), {'KADIMA_CELESTIAL_ENABLED':'0'})

    def test_fast_off_does_not_mean_legacy_disabled(self):
        db = dict(controls={'auto_trade_enabled':False,'kill_latched':False}, news={})
        result = report.lane_controls({'flags':{}}, db)
        self.assertEqual(result['mode'], 'legacy_shield_gamma')
        self.assertIn('auto_paused', result['fast_lane'])
        self.assertEqual(report.lane_controls({'flags':None}, db)['mode'], 'unknown_runtime_flags')

    def test_stock_news_does_not_certify_index_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.db'
            db = sqlite3.connect(path)
            try:
                db.execute('CREATE TABLE celestial_health(name TEXT,ts TEXT,payload TEXT)')
                db.execute('INSERT INTO celestial_health VALUES (?,?,?)', ('news', NOW.isoformat(),
                    json.dumps({'ok':True,'covered_symbols':['RELIANCE']})))
                db.commit()
            finally:
                db.close()
            before = path.read_bytes()
            result = report.database_state(path, NOW)
            self.assertEqual(result['news']['verified_indices'], [])
            self.assertEqual(path.read_bytes(), before)

    def test_no_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing.db'
            result = report.database_state(path, NOW)
            self.assertEqual(result['database_status'], 'unavailable')
            self.assertFalse(path.exists())

    def test_stale_future_and_naive_times_fail_freshness(self):
        self.assertIsNone(report.age((NOW + timedelta(seconds=1)).isoformat(), NOW))
        self.assertIsNone(report.age(NOW.replace(tzinfo=None).isoformat(), NOW))
        self.assertEqual(report.age((NOW - timedelta(seconds=301)).isoformat(), NOW), 301)

    def test_audit_filters_previous_restart_and_hides_raw_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.jsonl'
            rows = [dict(ts=(NOW-timedelta(minutes=20)).isoformat(), event='oi_poll_failed', error='shared quote budget'),
                    dict(ts=NOW.isoformat(), event='oi_poll_failed', error='shared quote budget SECRET'),
                    dict(ts=NOW.isoformat(), event='FAST_CANDIDATE', blocked_reasons=['auto_paused'], token='SECRET')]
            path.write_text('\n'.join(json.dumps(row) for row in rows))
            result = report.audit_state(path, NOW-timedelta(minutes=5), NOW)
            self.assertEqual(result['events']['oi_poll_failed'], 1)
            self.assertEqual(result['reasons']['local_quote_budget_timeout'], 1)
            self.assertNotIn('SECRET', json.dumps(result))

    def test_source_check_does_not_execute_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'kadima').mkdir()
            (root/'kadima'/'config.py').write_text('raise RuntimeError("do not import")\nORDER_PLACEMENT_ENABLED = True\n')
            self.assertIs(report.source_order_setting(root), True)

    def test_latest_broker_failure_supersedes_earlier_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.jsonl'
            success = dict(ts=(NOW-timedelta(seconds=60)).isoformat(), event='startup_reconcile_complete')
            failure = dict(ts=(NOW-timedelta(seconds=10)).isoformat(),
                           event='startup_reconcile_order_book_unreadable', error='SECRET')
            # Use timestamps, including when records are not in file order.
            path.write_text('\n'.join(json.dumps(r) for r in (failure, success)))
            result = report.audit_state(path, NOW-timedelta(minutes=5), NOW)
            self.assertEqual(result['latest_reconciliation']['event'], failure['event'])
            self.assertEqual(result['latest_reconciliation']['age_seconds'], 10)
            self.assertNotIn('SECRET', json.dumps(result))

    def test_runtime_controls_are_allowlisted_and_strictly_typed(self):
        config = dict(broker_orders_enabled=True, rollout_enabled=False,
                      auto_trade_enabled='false', kill_latched=False,
                      score_threshold=65, api_token='SECRET')
        with patch.object(report, 'api_token', return_value='SECRET'), patch.object(report, 'local_get',
                side_effect=[({'status':'ok'}, {'ok':True, 'market_open':False, 'engine_attached':True}),
                             ({'status':'ok'}, config)]):
            result = report.runtime_state(42)
        self.assertTrue(result['controls']['broker_orders_enabled'])
        self.assertFalse(result['controls']['rollout_enabled'])
        self.assertIsNone(result['controls']['auto_trade_enabled'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_local_get_only_allows_fixed_get_endpoints_and_closes(self):
        connection = Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"broker_orders_enabled":true}'
        with patch.object(report.http.client, 'HTTPConnection', return_value=connection) as factory:
            meta, data = report.local_get(8081, '/api/kadima/config', 'SECRET')
            factory.assert_called_once_with('127.0.0.1', 8081, timeout=4)
            connection.request.assert_called_once_with('GET', '/api/kadima/config',
                                                       headers={'Authorization':'Bearer SECRET'})
            self.assertTrue(data['broker_orders_enabled'])
            connection.close.assert_called_once()
            report.local_get(8081, '/api/kadima/resume', 'SECRET')
            report.local_get(8081, '/order_book', 'SECRET')
            self.assertEqual(factory.call_count, 1)

    def test_http_errors_redirects_and_raw_bodies_never_escape(self):
        connection = Mock()
        with patch.object(report.http.client, 'HTTPConnection', return_value=connection):
            for status in (302, 401, 500):
                connection.getresponse.return_value.status = status
                result = report.local_get(8081, '/api/kadima/config', 'SECRET')
                self.assertEqual(result, ({'status':'http_error', 'http_status':status}, None))
            connection.getresponse.return_value.status = 200
            for body in (b'SECRET', b'[]', b'x'*65537):
                connection.getresponse.return_value.read.return_value = body
                meta, data = report.local_get(8081, '/api/kadima/config', 'SECRET')
                self.assertIsNone(data)
                self.assertNotIn('SECRET', json.dumps(meta))
            connection.request.side_effect = OSError('SECRET')
            self.assertEqual(report.local_get(8080, '/health'), ({'status':'unavailable'}, None))

    def test_missing_credential_does_not_make_authenticated_request(self):
        with patch.object(report, 'api_token', return_value=None), patch.object(report, 'local_get',
                return_value=({'status':'ok'}, {'ok':True})) as get:
            result = report.runtime_state(42)
        get.assert_called_once_with(8080, '/health')
        self.assertEqual(result['controls']['status'], 'credential_unavailable')

    def test_credential_reads_only_existing_sources_without_output(self):
        with patch.object(report.Path, 'read_bytes', return_value=b'OTHER=private\0KADIMA_API_TOKEN=SECRET\0'):
            self.assertEqual(report.api_token(42), 'SECRET')
        with patch.object(report.Path, 'read_bytes', side_effect=PermissionError), \
             patch.object(report.Path, 'read_text', return_value='{"api_token":"SECRET"}'):
            self.assertEqual(report.api_token(42), 'SECRET')


if __name__ == '__main__':
    unittest.main()
