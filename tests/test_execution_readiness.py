import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
