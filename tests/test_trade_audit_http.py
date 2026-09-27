"""Authenticated audit reads, bounded filters and fixed gateway destinations."""
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_celestial_http as backend_fixture
import test_celestial_gateway as gateway_fixture


@unittest.skipUnless(backend_fixture.AVAILABLE, 'FastAPI/httpx required')
class AuditHttp(unittest.TestCase):
    setUp = backend_fixture.HttpControls.setUp
    tearDown = backend_fixture.HttpControls.tearDown

    def test_both_read_routes_require_token_and_never_submit(self):
        for path in ('/api/kadima/audit', '/api/kadima/audit/export'):
            self.assertEqual(self.client.get(path).status_code, 401)
            response = self.client.get(path, headers=self.auth)
            self.assertEqual(response.status_code, 200)
        self.ex.buy_candidate.assert_not_called()
        self.ex.squareoff.assert_not_called()

    def test_filter_validation_and_empty_results(self):
        for query in ('day=2026-02-31', 'strategy=bad', 'status=bad', 'limit=201', 'offset=-1'):
            self.assertEqual(self.client.get('/api/kadima/audit?'+query, headers=self.auth).status_code, 422)
        data = self.client.get('/api/kadima/audit?day=2026-09-14&strategy=GAMMA', headers=self.auth).json()
        self.assertEqual(data['rows'], [])
        self.assertEqual(data['total'], 0)

    def test_backend_errors_do_not_leak_details(self):
        with patch.object(self.api.trade_audit, 'report', side_effect=RuntimeError('private-database-path')):
            response = self.client.get('/api/kadima/audit', headers=self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail':'audit_unavailable'})

    def test_learning_auth_filters_and_isolation(self):
        self.assertEqual(self.client.get('/api/kadima/learning').status_code,401)
        response=self.client.get('/api/kadima/learning?day=2026-09-14',headers=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['mode'],'ADVISORY')
        self.assertFalse(response.json()['worker_fresh'])
        for q in ('strategy=bad','limit=201','day=2026-02-31','offset=-1'):
            self.assertEqual(self.client.get('/api/kadima/learning?'+q,headers=self.auth).status_code,422)
        self.ex.buy_candidate.assert_not_called();self.ex.squareoff.assert_not_called()


@unittest.skipUnless(gateway_fixture.AVAILABLE, 'FastAPI/httpx required')
class AuditGateway(unittest.TestCase):
    setUp = gateway_fixture.Gateway.setUp
    tearDown = gateway_fixture.Gateway.tearDown

    def test_session_required_and_response_not_cached(self):
        for path in ('/audit', '/audit-export'):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.upstream.assert_not_called()
            result = self.client.get(path, headers=self.auth)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.upstream.reset_mock()

    def test_filter_encoding_cannot_change_destination(self):
        response = self.client.get('/audit', params={'day':'2026-09-14', 'symbol':'ABC&url=https://bad.test',
                                                     'limit':'50', 'offset':'0'}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        port, path = self.upstream.call_args.args
        self.assertEqual(port, 8081)
        parsed = urlsplit(path)
        self.assertEqual(parsed.path, '/api/kadima/audit')
        self.assertEqual(parse_qs(parsed.query)['symbol'], ['ABC&url=https://bad.test'])

    def test_unknown_duplicate_overlong_and_export_paging_rejected(self):
        for path in ('/audit?url=bad', '/audit?day=a&day=b', '/audit?symbol='+'x'*81,
                     '/audit-export?offset=1'):
            self.assertEqual(self.client.get(path, headers=self.auth).status_code, 422)
        self.upstream.assert_not_called()

    def test_learning_read_only_fixed_destination(self):
        self.assertEqual(self.client.get('/learning').status_code,401)
        self.upstream.assert_not_called()
        self.assertEqual(self.client.get('/learning?day=2026-09-14&strategy=SCALP',headers=self.auth).status_code,200)
        self.assertEqual(self.upstream.call_args.args,(8081,'/api/kadima/learning?day=2026-09-14&strategy=SCALP'))
        self.upstream.reset_mock()
        for path in ('/learning?url=https://bad.test','/learning?day=a&day=b','/learning?symbol=ABC'):
            self.assertEqual(self.client.get(path,headers=self.auth).status_code,422)
        self.upstream.assert_not_called()


if __name__ == '__main__':
    unittest.main()
