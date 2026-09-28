import os
import unittest
from unittest.mock import patch
try:
    from fastapi.testclient import TestClient
    from kadima import control_api, owned_watchdog, celestial
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'HTTP dependencies are installed in GitHub CI')
class HTTP(unittest.TestCase):
    def test_watchdog_and_feature_status_require_authentication(self):
        with patch.dict(os.environ, KADIMA_API_TOKEN='fixture-only'), TestClient(control_api.app) as client, \
             patch.object(owned_watchdog, 'cycle') as cycle:
            self.assertEqual(client.post('/api/kadima/watchdog', json={}).status_code, 401)
            self.assertEqual(client.get('/api/kadima/priority/status').status_code, 401)
            cycle.assert_not_called()

    def test_watchdog_uses_registered_executor_without_raw_order_arguments(self):
        executor = object()
        with patch.dict(os.environ, KADIMA_API_TOKEN='fixture-only'), TestClient(control_api.app) as client, \
             patch.object(control_api, 'EXECUTOR', executor), patch.object(celestial, 'enabled', return_value=True), \
             patch.object(owned_watchdog, 'cycle', return_value=dict(ok=True, results=[])) as cycle:
            out = client.post('/api/kadima/watchdog', headers={'Authorization':'Bearer fixture-only'},
                              json={'symbol':'UNOWNED', 'quantity':99999})
            self.assertEqual(out.status_code, 422)
            cycle.assert_not_called()
            out = client.post('/api/kadima/watchdog', headers={'Authorization':'Bearer fixture-only'}, json={})
            self.assertEqual(out.status_code, 200)
            cycle.assert_called_once_with(executor)


if __name__ == '__main__': unittest.main()
