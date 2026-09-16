"""Host telemetry uses bounded kernel counters and never a broker request."""
import builtins
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def gateway():
    spec = importlib.util.spec_from_file_location('resource_gateway', ROOT/'dashboard_live/gateway.py')
    value = importlib.util.module_from_spec(spec)
    original = builtins.open
    def read(path, *args, **kwargs):
        if str(path).endswith('/config.json'):
            return io.StringIO(json.dumps({'origin': 'https://kadima.test', 'api_token': 'PRIVATE_TOKEN'}))
        return original(path, *args, **kwargs)
    with patch('builtins.open', side_effect=read):
        spec.loader.exec_module(value)
    return value


class Resources(unittest.TestCase):
    def setUp(self):
        self.g = gateway()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.write_memory()
        self.stat = 'cpu 100 0 100 700 40 20 20 20 80 10\n'
        (self.proc/'stat').write_text(self.stat)
        (self.proc/'uptime').write_text('3661.2 100\n')
        self.sampler = self.g.ResourceSampler(self.proc)
        self.clock = 10
        for target, kwargs in (
            ('monotonic', {'side_effect': lambda: self.clock}),
        ):
            p = patch.object(self.g.time, target, **kwargs); p.start(); self.addCleanup(p.stop)
        for owner, name, value in (
            (self.g.os, 'cpu_count', 2),
            (self.g.os, 'getloadavg', (0.2, 0.3, 0.4)),
            (self.g.shutil, 'disk_usage', SimpleNamespace(total=1000, free=600)),
        ):
            p = patch.object(owner, name, return_value=value); p.start(); self.addCleanup(p.stop)

    def write_memory(self, available=400, swap_total=0, swap_free=0):
        (self.proc/'meminfo').write_text(f'MemTotal: 1000 kB\nMemAvailable: {available} kB\nMemFree: 5 kB\nCached: 900 kB\nSwapTotal: {swap_total} kB\nSwapFree: {swap_free} kB\n')

    def advance_cpu(self):
        # Deltas: user 20, system 20, idle 100, wait 20, irq 10,
        # softirq 10, steal 20. Total 200; executing work 60 => 30%.
        # Guest counters are already represented in user/nice and must not add.
        self.clock += 2
        (self.proc/'stat').write_text('cpu 120 0 120 800 60 30 30 40 5000 6000\n')

    def test_memory_uses_available_not_free_or_all_cache(self):
        data = self.sampler.snapshot()
        self.assertEqual(data['memory']['used_percent'], 60)
        self.assertEqual(data['memory']['used_bytes'], 600*1024)
        self.assertEqual(data['memory']['available_bytes'], 400*1024)
        self.assertEqual(data['status'], 'warming_up')
        self.assertIsNone(data['cpu']['busy_percent'])
        self.assertFalse(data['swap']['enabled'])
        self.assertIsNone(data['swap']['used_percent'])

    def test_cpu_deltas_exclude_guest_double_count_and_report_wait_separately(self):
        self.sampler.snapshot(); self.advance_cpu()
        data = self.sampler.snapshot()
        self.assertEqual(data['cpu']['busy_percent'], 30)
        self.assertEqual(data['cpu']['iowait_percent'], 10)
        self.assertEqual(data['cpu']['steal_percent'], 10)
        self.assertEqual(data['cpu']['interval_seconds'], 2)
        self.assertEqual(data['cpu']['load_1m'], 0.2)

    def test_repeated_viewers_share_same_sample_without_resetting_cpu(self):
        first = self.sampler.snapshot()
        with patch.object(self.sampler, 'read', side_effect=AssertionError('extra collection')):
            self.clock += 0.5
            self.assertIs(self.sampler.snapshot(), first)

    def test_missing_or_invalid_memory_cannot_look_healthy(self):
        for text in ('MemTotal: 1000 kB\n', 'MemTotal: 0 kB\nMemAvailable: 0 kB\n',
                     'MemTotal: 1000 kB\nMemAvailable: 2000 kB\n'):
            self.clock += 2
            (self.proc/'meminfo').write_text(text)
            data = self.sampler.snapshot()
            self.assertIsNone(data['memory'])
            self.assertIn('RAM', data['missing'])
            self.assertNotEqual(data['status'], 'healthy')

    def test_memory_thresholds_and_swap_usage(self):
        for available, expected in ((150, 'warning'), (50, 'critical')):
            self.clock += 2
            self.write_memory(available, 1000, 750)
            data = self.sampler.snapshot()
            self.assertEqual(data['status'], expected)
            self.assertEqual(data['swap']['used_percent'], 25)
            self.assertTrue(data['swap']['enabled'])

    def test_cpu_restarts_and_long_sampling_gaps_return_measuring(self):
        self.sampler.snapshot(); self.advance_cpu()
        self.assertIsNotNone(self.sampler.snapshot()['cpu']['busy_percent'])
        self.clock += 2
        (self.proc/'stat').write_text(self.stat)
        self.assertIsNone(self.sampler.snapshot()['cpu']['busy_percent'])
        self.clock += 100
        self.assertIsNone(self.sampler.snapshot()['cpu']['busy_percent'])

    def test_read_failures_are_sanitized_and_old_values_are_not_relabelled(self):
        self.sampler.snapshot(); self.clock += 2
        with patch.object(self.sampler, 'read', side_effect=OSError('PRIVATE_CONTENT')), \
                patch.object(self.g.shutil, 'disk_usage', side_effect=OSError('PRIVATE_DISK')):
            data = self.sampler.snapshot()
        self.assertEqual(data['status'], 'unavailable')
        self.assertIsNone(data['memory'])
        self.assertIsNone(data['cpu'])
        self.assertNotIn('PRIVATE', json.dumps(data, allow_nan=False))

    def test_authenticated_http_route_reads_only_local_resources(self):
        from fastapi.testclient import TestClient
        self.g.RESOURCES = self.sampler
        self.g.SESSIONS['fixture'] = self.g.time.time()+60
        with TestClient(self.g.app) as client, patch.object(self.g, 'upstream') as broker:
            self.assertEqual(client.get('/server_health').status_code, 401)
            response = client.get('/server_health', headers={'Cookie': self.g.COOKIE+'=fixture'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.json()['memory']['used_percent'], 60)
            self.assertNotIn('PRIVATE_TOKEN', response.text)
            self.assertEqual(client.post('/server_health').status_code, 405)
            broker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
