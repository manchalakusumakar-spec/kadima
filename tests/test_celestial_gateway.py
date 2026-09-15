"""Session/CSRF/allowlist coverage using a synthetic gateway configuration."""
import builtins
import importlib.util
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

AVAILABLE=importlib.util.find_spec('fastapi') is not None and importlib.util.find_spec('httpx') is not None


@unittest.skipUnless(AVAILABLE,'Install FastAPI/httpx for gateway HTTP tests')
class Gateway(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        spec=importlib.util.spec_from_file_location('celestial_test_gateway',Path(__file__).parents[1]/'dashboard_live/gateway.py')
        self.g=importlib.util.module_from_spec(spec)
        cfg=dict(origin='https://kadima.test',api_token='offline-only',salt='00',password_hash='unused')
        original=builtins.open
        def read(file,*a,**kw):
            return io.StringIO(json.dumps(cfg)) if str(file).endswith('/config.json') else original(file,*a,**kw)
        with patch('builtins.open',side_effect=read):spec.loader.exec_module(self.g)
        self.g.SESSIONS['fixture-session']=time.time()+60
        self.auth={'Cookie':self.g.COOKIE+'=fixture-session','Origin':'https://kadima.test'}
        self.upstream=patch.object(self.g,'upstream',return_value={'ok':True}).start()
        self.client=TestClient(self.g.app,base_url='https://kadima.test');self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None);patch.stopall()

    def test_session_is_required_for_reads_and_writes(self):
        self.assertEqual(self.client.get('/celestial').status_code,401)
        self.assertEqual(self.client.post('/celestial/order/buy',json={},headers={'Origin':'https://kadima.test'}).status_code,401)
        self.upstream.assert_not_called()

    def test_cross_origin_order_is_rejected(self):
        r=self.client.post('/celestial/order/buy',json={'candidate_id':'NFO:1'},headers=self.auth|{'Origin':'https://attacker.test'})
        self.assertEqual(r.status_code,403);self.upstream.assert_not_called()

    def test_route_allowlist_excludes_news_ingestion_and_raw_proxying(self):
        for path in ('kadima/news','https://attacker.test','order/arbitrary'):
            self.assertEqual(self.client.post('/celestial/'+path,json={},headers=self.auth).status_code,404)
        self.upstream.assert_not_called()

    def test_authorized_buy_routes_to_internal_backend_only(self):
        r=self.client.post('/celestial/order/buy',json={'candidate_id':'NFO:1'},headers=self.auth)
        self.assertEqual(r.status_code,200)
        self.upstream.assert_called_once_with(8081,'/api/order/buy','POST',{'candidate_id':'NFO:1'})
        self.assertEqual(r.headers['cache-control'],'no-store')

    def test_request_size_and_json_shape(self):
        self.assertEqual(self.client.post('/celestial/order/buy',content='x'*4097,headers=self.auth).status_code,413)
        self.assertEqual(self.client.post('/celestial/order/buy',json=[],headers=self.auth).status_code,400)
        self.upstream.assert_not_called()

    def test_dashboard_contains_no_broker_bearer_token(self):
        html=(Path(__file__).parents[1]/'dashboard_live/dashboard.html').read_text()
        self.assertNotIn('Bearer ',html)
        self.assertNotIn('API_TOKEN',html)
        self.assertIn("credentials:'same-origin'",html)


if __name__=='__main__':unittest.main()
