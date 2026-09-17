"""FETCH integration must expose observations without refreshing stale evidence."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kadima import universe_api as u
from kadima.config import IST

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=IST)


def row(name='STOCK', **overrides):
    return dict(dict(name=name, spot=100, score=65, tier='ACTIVE', direction='CE',
                     last_updated=NOW.isoformat(), best_contract=dict(symbol='STOCK30SEP26C100',
                     strike=100, type='CE', ltp=5, updated_at=NOW.isoformat())), **overrides)


class UniverseRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'snapshot.json'
        self.source = dict(ts=NOW.isoformat(), tracked=[row()],
                           prices={'STOCK': dict(ltp=100, received_at=NOW.isoformat())},
                           coverage=dict(expected=1, priced=1, gamma_limit=20))

    def read(self, now=NOW):
        self.path.write_text(json.dumps(self.source))
        return u.read_universe(self.path, now)

    def test_current_legacy_snapshot_exposes_contract_without_changing_score(self):
        result = self.read()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['score_model'], 'stock_gamma')
        self.assertEqual(result['top20'][0]['best_contract']['symbol'], 'STOCK30SEP26C100')
        self.assertEqual(result['coverage']['fresh_prices'], 1)

    def test_fresh_snapshot_never_refreshes_old_row_or_contract(self):
        self.source['tracked'][0]['last_updated'] = (NOW-timedelta(minutes=10)).isoformat()
        result = self.read()
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(result['top20'][0]['best_contract']['fresh'])
        self.source['tracked'][0]['last_updated'] = NOW.isoformat()
        self.source['tracked'][0]['best_contract']['updated_at'] = (NOW-timedelta(seconds=31)).isoformat()
        result = self.read()
        self.assertTrue(result['top20'][0]['fresh'])
        self.assertFalse(result['top20'][0]['best_contract']['fresh'])

    def test_after_hours_retains_history_but_never_claims_live(self):
        result = self.read(NOW.replace(hour=23, minute=49))
        self.assertEqual(result['status'], 'market_closed')
        self.assertEqual(result['as_of'], NOW.isoformat())
        self.assertEqual(result['tracked_count'], 1)
        self.assertFalse(result['top20'][0]['fresh'])

    def test_partial_coverage_is_explicit(self):
        self.source['coverage']['expected'] = 210
        result = self.read()
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['fresh'])
        self.assertEqual(result['coverage']['priced'], 1)

    def test_full_scanner_is_bounded_and_prefers_recent_rows(self):
        self.source['tracked'] = [row('STOCK'+str(i), score=i % 100) for i in range(210)]
        self.source['tracked'][99]['last_updated'] = '2026-09-15T10:00:00+05:30'
        result = self.read()
        self.assertEqual((result['tracked_count'], result['returned_count']), (210, 20))
        self.assertNotIn('STOCK99', [item['name'] for item in result['top20']])

    def test_invalid_and_future_timestamps_never_appear_recent(self):
        for stamp in (None, 'garbage', NOW.replace(tzinfo=None).isoformat(),
                      (NOW+timedelta(seconds=1)).isoformat()):
            with self.subTest(stamp=stamp):
                self.source['ts'] = stamp
                self.assertEqual(self.read()['status'], 'stale')

    def test_corruption_missing_and_oversize_do_not_break_fetch(self):
        self.assertEqual(u.read_universe(self.path, NOW)['status'], 'missing')
        for raw in ('{', '[]', '{"tracked": [], "prices": []}'):
            self.path.write_text(raw)
            self.assertEqual(u.read_universe(self.path, NOW)['status'], 'unavailable')
        with patch.object(u, 'MAX_BYTES', 5):
            self.assertEqual(self.read()['reason'], 'snapshot_too_large')

    def test_nan_duplicate_and_invalid_rows_are_filtered(self):
        self.source['tracked'] += [row(), row('NAN', score=float('nan')), None]
        self.source['tracked'][0]['best_contract']['ltp'] = float('nan')
        result = self.read()
        self.assertEqual(result['coverage']['invalid_rows'], 3)
        self.assertEqual(result['tracked_count'], 1)
        json.dumps(result, allow_nan=False)

    def test_real_http_state_contains_universe_with_existing_auth(self):
        from fastapi.testclient import TestClient
        from kadima import api, commodity_watch
        self.read()
        with patch.object(api, 'ENGINE', SimpleNamespace(states={})), \
                patch.object(api, 'API_TOKEN', 'test-token'), \
                patch.object(api, 'now_ist', return_value=NOW), \
                patch.object(u, 'SNAPSHOT_PATH', self.path), \
                patch.object(commodity_watch, 'SNAPSHOT_PATH', self.path.parent/'missing'):
            client = TestClient(api.app)
            self.assertIn(client.get('/state').status_code, (401, 403))
            response = client.get('/state', headers={'Authorization': 'Bearer test-token'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['fno_universe']['tracked_count'], 1)
            self.assertFalse(response.json()['commodities']['execution_enabled'])


if __name__ == '__main__':
    unittest.main()
