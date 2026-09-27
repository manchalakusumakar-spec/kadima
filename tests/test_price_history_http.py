"""Authenticated, bounded historical reads must never request broker data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

import test_celestial_http as backend
import test_celestial_gateway as gateway


@unittest.skipUnless(backend.AVAILABLE,'FastAPI/httpx required')
class PriceHttp(unittest.TestCase):
    def setUp(self):
        backend.HttpControls.setUp(self)
        self.price_dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.price_dir.cleanup)
        patch.object(self.api.price_history,'database_path',return_value=Path(self.price_dir.name)/'history.db').start()

    tearDown=backend.HttpControls.tearDown

    def test_all_routes_require_auth_and_do_not_create_database(self):
        for path in ('/api/kadima/prices','/api/kadima/prices/export',
                     '/api/kadima/prices/minutes?day=2026-09-24&key='+'a'*32):
            self.assertEqual(self.client.get(path).status_code,401)
            response=self.client.get(path,headers=self.auth)
            self.assertEqual(response.status_code,200)
        self.assertFalse((Path(self.price_dir.name)/'history.db').exists())
        self.ex.buy_candidate.assert_not_called()
        self.ex.squareoff.assert_not_called()

    def test_invalid_filters_and_identity_fail_closed(self):
        for path in ('/api/kadima/prices?limit=201','/api/kadima/prices?offset=-1',
                     '/api/kadima/prices?day=2026-02-31','/api/kadima/prices?asset=ALL_ORDERS',
                     '/api/kadima/prices/minutes?day=2026-09-24&key=../secret'):
            self.assertEqual(self.client.get(path,headers=self.auth).status_code,422)

    def test_storage_error_is_redacted(self):
        with patch.object(self.api.price_history,'report',side_effect=RuntimeError('private path')):
            result=self.client.get('/api/kadima/prices',headers=self.auth)
        self.assertEqual(result.status_code,503)
        self.assertEqual(result.json(),{'detail':'price_history_unavailable'})


@unittest.skipUnless(gateway.AVAILABLE,'FastAPI/httpx required')
class PriceGateway(unittest.TestCase):
    setUp=gateway.Gateway.setUp
    tearDown=gateway.Gateway.tearDown

    def test_session_and_fixed_destination(self):
        for route in ('prices','price-export','price-minutes'):
            self.assertEqual(self.client.get('/'+route).status_code,401)
        self.upstream.assert_not_called()
        result=self.client.get('/prices',headers=self.auth,params={'day':'2026-09-24','symbol':'ABC&url=https://bad.test'})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.headers['cache-control'],'no-store')
        port,path=self.upstream.call_args.args
        self.assertEqual(port,8081)
        self.assertEqual(urlsplit(path).path,'/api/kadima/prices')
        self.assertEqual(parse_qs(urlsplit(path).query)['symbol'],['ABC&url=https://bad.test'])

    def test_duplicate_and_unknown_filters_cannot_reach_upstream(self):
        for path in ('/prices?day=a&day=b','/prices?url=x','/price-minutes?asset=STOCK',
                     '/price-export?offset=1','/prices?symbol='+'x'*81):
            self.assertEqual(self.client.get(path,headers=self.auth).status_code,422)
        self.upstream.assert_not_called()


if __name__=='__main__':unittest.main()
