"""Offline source and fixed-route tests for the reviewed AWS UI composition."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
UI = Path(os.environ.get('REVIEWED_DASHBOARD_DIR', ROOT/'momentum_recovery/reviewed_dashboard'))
UPSTREAM = Path(os.environ.get('UPSTREAM_DASHBOARD_DIR', ROOT/'dashboard_live'))


class HTTPError(Exception):
    def __init__(self, status, detail):
        self.status_code, self.detail = status, detail


class ReviewedDashboard(unittest.TestCase):
    def setUp(self):
        self.source = (UI/'gateway.py').read_text()
        self.tree = ast.parse(self.source)
        self.functions = {n.name: n for n in self.tree.body
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.auth, self.upstream = Mock(), Mock(return_value={'ok': True})
        self.scope = dict(Request=object, HTTPException=HTTPError, authenticated=self.auth,
                          upstream=self.upstream, json=json)
        reads = next(n for n in self.tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id=='READS' for t in n.targets))
        self.scope['READS'] = ast.literal_eval(reads.value)
        for name in ('read_data', 'live_news'):
            node = self.functions[name]
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<reviewed-route>', 'exec'), self.scope)

    def request(self, items=()):
        return SimpleNamespace(query_params=SimpleNamespace(multi_items=lambda: list(items)))

    def test_manifest_matches_composed_bytes(self):
        manifest = json.loads((UI/'manifest.json').read_text())
        self.assertEqual(manifest['source_release_commit'], '8837cd6dc04e16123e94d8b78a3a716f83ed022c')
        for name in ('gateway.py', 'dashboard.html'):
            raw = (UI/name).read_bytes()
            blob = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
            self.assertEqual(blob, manifest['files'][name]['composed_blob'])

    def test_auth_sessions_and_writes_match_reviewed_upstream(self):
        upstream = ast.parse((UPSTREAM/'gateway.py').read_text())
        by_name = {n.name: n for n in upstream.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ('origin_check', 'authenticated', 'security_headers', 'login',
                     'session', 'logout', 'upstream', 'manual_exit', 'celestial_write'):
            self.assertEqual(ast.dump(self.functions[name], include_attributes=False),
                             ast.dump(by_name[name], include_attributes=False), name)

    def test_new_reads_authenticate_and_keep_fixed_destinations(self):
        for route in ('learning', 'audit', 'audit-export', 'prices', 'price-minutes', 'price-export'):
            with self.subTest(route=route):
                self.scope['read_data'](route, self.request([('day','2026-09-28')]))
                self.auth.assert_called()
                port, path = self.upstream.call_args.args
                self.assertEqual(port, 8081)
                self.assertEqual(path, self.scope['READS'][route][1]+'?day=2026-09-28')

    def test_duplicate_unknown_and_oversized_filters_are_rejected(self):
        for route in ('learning', 'audit', 'prices'):
            for items in ([('day','a'),('day','b')], [('url','https://bad.test')], [('day','x'*81)]):
                with self.subTest(route=route,items=items), self.assertRaises(HTTPError) as raised:
                    self.scope['read_data'](route, self.request(items))
                self.assertEqual(raised.exception.status_code, 422)
        self.upstream.assert_not_called()

    def test_auth_failure_prevents_upstream_and_news_file_reads(self):
        self.auth.side_effect = HTTPError(401, 'sign_in_required')
        with patch('builtins.open') as opened:
            for name, args in (('read_data', ('learning', self.request())), ('live_news', (self.request(),))):
                with self.assertRaises(HTTPError):
                    self.scope[name](*args)
            opened.assert_not_called()
        self.upstream.assert_not_called()

    def test_news_keeps_exact_local_path_and_bound(self):
        with patch('builtins.open', return_value=io.StringIO('{"articles":[]}')) as opened:
            self.assertEqual(self.scope['live_news'](self.request()), {'articles':[]})
            opened.assert_called_once_with('/var/lib/kadima-live-inputs/news.json')
        with patch('builtins.open', return_value=io.StringIO('x'*2_000_001)):
            with self.assertRaises(HTTPError) as raised:
                self.scope['live_news'](self.request())
            self.assertEqual(raised.exception.status_code, 503)

    def test_preserved_panels_and_restored_mcx_helper(self):
        page = (UI/'dashboard.html').read_text()
        for marker in ('// KADIMA LIVE INPUTS V1', '<!-- KADIMA STOCK FAST PANEL v1 -->',
                       'id="tradeAudit"', 'id="tradeLearning"', 'id="priceMemory"',
                       'function mcxGamma('):
            self.assertEqual(page.count(marker), 1)
        for marker in ('id="legacy-fastMomentum"', 'id="legacy-executionDiagnostics"', 'id="legacy-universe"'):
            self.assertNotIn(marker, page)
        upstream = (UPSTREAM/'dashboard.html').read_text()
        helper = next(line for line in upstream.splitlines() if line.startswith('function mcxGamma('))
        self.assertIn(helper, page)
        self.assertNotIn('Bearer ', page)
        self.assertNotIn('API_TOKEN', page)


if __name__ == '__main__':
    unittest.main()
