"""Real FastAPI routing/auth tests. No broker call is permitted."""
import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

AVAILABLE=importlib.util.find_spec('fastapi') is not None and importlib.util.find_spec('httpx') is not None


@unittest.skipUnless(AVAILABLE,'Install FastAPI and httpx for HTTP integration tests')
class HttpControls(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from kadima import control_api as api, celestial as c, storage, session, audit
        self.tmp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(storage,'DATA_DIR',self.tmp.name),
            patch.object(storage,'DB_PATH',str(Path(self.tmp.name)/'test.db')),
            patch.object(storage,'_local',threading.local()),
            patch.object(audit,'log',Mock()),
            patch.dict(os.environ,{'KADIMA_CELESTIAL_ENABLED':'1','KADIMA_API_TOKEN':'offline-test-token'}),
            patch.object(session.SESSION,'ensure',side_effect=AssertionError('Broker network forbidden'))]
        for p in self.patches:p.start()
        self.api,self.c=api,c
        self.ex=SimpleNamespace(_execution_lock=threading.RLock(),_reconcile_blocked=False,
            state=SimpleNamespace(positions={}),_celestial_quotes={},
            reconcile_on_startup=Mock(),buy_candidate=Mock(return_value={'ok':True,'reason':'awaiting_broker_fill'}),
            squareoff=Mock(return_value={'ok':True,'reason':'exit_requests_submitted_not_fill_confirmation'}))
        self.patches += [patch.object(api,'EXECUTOR',self.ex)]
        self.patches[-1].start()
        self.client=TestClient(api.app);self.client.__enter__()
        self.auth={'Authorization':'Bearer offline-test-token'}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def post(self,path,payload=None):
        return self.client.post(path,json=payload or {},headers=self.auth)

    def test_all_new_control_routes_require_auth(self):
        for path in ('/api/kadima/config','/api/order/buy','/api/order/sell','/api/order/exit',
                     '/api/order/sl','/api/order/trailing-sl','/api/order/modify-sl','/api/order/cancel',
                     '/api/order/squareoff-all','/api/kadima/pause','/api/kadima/resume',
                     '/api/kadima/kill','/api/kadima/reset-kill','/api/kadima/news'):
            with self.subTest(path=path):self.assertEqual(self.client.post(path,json={}).status_code,401)
        self.assertEqual(self.client.get('/api/kadima/top20').status_code,401)

    def test_config_round_trip_and_invalid_threshold(self):
        response=self.post('/api/kadima/config',{'score_threshold':75,'revision':0})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get('/api/kadima/config',headers=self.auth).json()['score_threshold'],75)
        self.assertEqual(self.post('/api/kadima/config',{'score_threshold':66}).status_code,409)
        self.assertEqual(self.post('/api/kadima/config',{'score_threshold':65,'revision':0}).status_code,409)

    def test_buy_accepts_only_server_candidate_identifier(self):
        self.assertEqual(self.post('/api/order/buy',{'token':'X','quantity':100}).status_code,422)
        self.ex.buy_candidate.assert_not_called()
        r=self.post('/api/order/buy',{'candidate_id':'NFO:X'})
        self.assertEqual(r.status_code,200)
        self.ex.buy_candidate.assert_called_once_with('NFO:X')

    def test_sell_cannot_open_a_short_or_use_invalid_signal(self):
        for sid in (True,0,-1,'10'):
            self.assertEqual(self.post('/api/order/sell',{'signal_id':sid}).status_code,422)

    def test_pause_leaves_monitor_running(self):
        self.assertEqual(self.post('/api/kadima/pause').status_code,200)
        self.assertFalse(self.c.read_config()['auto_trade_enabled'])
        self.ex.squareoff.assert_not_called()

    def test_resume_requires_verified_news(self):
        self.assertEqual(self.post('/api/kadima/resume').status_code,409)
        self.assertFalse(self.c.read_config()['auto_trade_enabled'])

    def test_kill_is_durable_before_exit_request(self):
        self.ex.squareoff.side_effect=lambda reason:dict(ok=self.c.read_config()['kill_latched'])
        self.assertEqual(self.post('/api/kadima/kill').status_code,200)
        self.assertTrue(self.c.read_config()['kill_latched'])

    def test_rollout_gate_blocks_mutations(self):
        with patch.dict(os.environ,{'KADIMA_CELESTIAL_ENABLED':'0'}):
            self.assertEqual(self.post('/api/kadima/pause').status_code,409)
            self.assertEqual(self.post('/api/order/buy',{'candidate_id':'NFO:X'}).status_code,409)

    def test_top20_reports_real_empty_state_without_broker_poll(self):
        r=self.client.get('/api/kadima/top20',headers=self.auth)
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['candidates'],[])
        self.assertEqual(r.json()['positions'],[])
        self.assertIsNone(r.json()['system']['news'])


if __name__=='__main__':unittest.main()
