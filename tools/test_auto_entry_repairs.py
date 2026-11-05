"""Offline regressions: no broker, network, credentials or live configuration."""
import ast
import importlib.util
from pathlib import Path
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('audit_test_kadima')
pkg.__path__ = [str(ROOT / 'kadima')]
sys.modules[pkg.__name__] = pkg
clock = datetime(2026, 10, 28, 13, tzinfo=ZoneInfo('Asia/Kolkata'))
db = sqlite3.connect(':memory:')
for name, attrs in {'storage': {'conn': lambda: db}, 'utils': {'now_ist': lambda: clock},
                    'config': {'GAMMA_INDICES': {'NIFTY': object(), 'SENSEX': object()}}}.items():
    mod = types.ModuleType(pkg.__name__ + '.' + name)
    mod.__dict__.update(attrs)
    sys.modules[mod.__name__] = mod

def load(name):
    spec = importlib.util.spec_from_file_location(pkg.__name__ + '.' + name, ROOT/'kadima'/f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

celestial = load('celestial')
news = load('news_catalysts')

class NewsCoverageTests(unittest.TestCase):
    def setUp(self):
        celestial.connection()
        db.execute('DELETE FROM celestial_health')
        db.commit()

    def ingest(self, symbols, scope='stocks', observed=None):
        return news.ingest([], source='verified-test-feed', observed_at=observed or clock.isoformat(),
                           covered_symbols=symbols, scope=scope)

    def test_stock_coverage_does_not_claim_index_coverage(self):
        self.ingest(['RELIANCE'])
        self.assertTrue(celestial.news_impact('RELIANCE','CE',clock)['news_available'])
        self.assertFalse(celestial.news_impact('NIFTY','CE',clock)['news_available'])

    def test_stock_refresh_does_not_erase_explicit_index_coverage(self):
        self.ingest(['NIFTY'], 'indices')
        self.ingest(['RELIANCE'])
        self.assertTrue(celestial.news_impact('NIFTY','CE',clock)['news_available'])
        self.assertTrue(celestial.news_impact('RELIANCE','CE',clock)['news_available'])
        self.assertFalse(celestial.news_impact('SENSEX','CE',clock)['news_available'])

    def test_stale_index_coverage_fails_closed(self):
        self.ingest(['NIFTY'], 'indices')
        self.assertFalse(celestial.news_impact('NIFTY','CE',clock+timedelta(seconds=301))['news_available'])

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError): self.ingest(['NIFTY'],'all')

    def test_risk_event_still_blocks_index(self):
        event=dict(id='risk',symbols=['NIFTY'],score=0,block=True,url='https://example.org/event',
                   published_at=(clock-timedelta(minutes=1)).isoformat(),
                   expires_at=(clock+timedelta(hours=1)).isoformat())
        news.ingest([event],source='verified-test-feed',observed_at=clock.isoformat(),
                    covered_symbols=['NIFTY'],scope='indices')
        self.assertTrue(celestial.news_impact('NIFTY','CE',clock)['news_blocked'])

class DispatchTests(unittest.TestCase):
    def test_celestial_uses_only_candidate_route(self):
        # Execute the actual dispatch tail with mocked transport, both modes.
        tree=ast.parse((ROOT/'kadima/engine.py').read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_score_gamma')
        first=next(i for i,n in enumerate(method.body) if isinstance(n,ast.ImportFrom) and any(a.name=='celestial' for a in n.names))
        fn=ast.FunctionDef(name='dispatch',args=method.args,body=method.body[first:],decorator_list=[])
        module=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
        calls=[]
        ns={'IndexState':object, '__package__':pkg.__name__, 'auto_executor':types.SimpleNamespace(EXECUTOR=types.SimpleNamespace(
            evaluate_and_enter=lambda **kw:calls.append(kw))), 'result':types.SimpleNamespace(direction='PE',tier='CONFIRMED',score=90), 'sid':1}
        exec(compile(module,'dispatch','exec'),ns)
        st=types.SimpleNamespace(name='NIFTY',chain=object(),spot=24000,snap=object())
        owner=types.SimpleNamespace(_dte=lambda st:1)
        from unittest.mock import patch
        with patch.object(celestial,'enabled',return_value=True): ns['dispatch'](owner,st)
        self.assertEqual(calls,[])
        with patch.object(celestial,'enabled',return_value=False): ns['dispatch'](owner,st)
        self.assertEqual(len(calls),1)

class ReadinessTests(unittest.TestCase):
    def test_paused_and_missing_coverage_are_reported_without_dispatch(self):
        tree=ast.parse((ROOT/'kadima/auto_executor.py').read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='prepare_celestial_context')
        method.decorator_list=[]
        logs=[]
        ns={'celestial':celestial,'time':types.SimpleNamespace(monotonic=lambda:100),
            'audit':types.SimpleNamespace(log=lambda event,**kw:logs.append((event,kw)))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'readiness','exec'),ns)
        calls=[]
        owner=types.SimpleNamespace(_celestial_contexts={},evaluate_and_enter=lambda **kw:calls.append(kw))
        row=dict(id='NFO:1',underlying='NIFTY',eligible=False,
                 blocked_reasons=['news_coverage_unavailable'])
        from unittest.mock import patch
        with patch.object(celestial,'enabled',return_value=True), patch.object(celestial,'top20',return_value=[row]), patch.object(celestial,'read_config',return_value=dict(auto_trade_enabled=False,kill_latched=False)):
            ns['prepare_celestial_context'](owner,'NIFTY',None,24000,None,1)
            ns['prepare_celestial_context'](owner,'NIFTY',None,24000,None,1)
        self.assertEqual(calls,[])
        self.assertEqual(len(logs),1)
        self.assertEqual(logs[0][1]['controls'],['auto_paused'])
        self.assertEqual(logs[0][1]['candidates']['NFO:1'],['news_coverage_unavailable'])

class PermitTests(unittest.TestCase):
    def test_expired_permit_stays_blocked(self):
        gate=load('market_session_gate')
        permit=dict(allowed=True,day=clock.date().isoformat(),boot_id='boot',
                    issued_mono=100,expires_mono=250,issued_at=clock.isoformat())
        self.assertIsNone(gate.permit_reason(permit,clock,100,'boot'))
        self.assertEqual(gate.permit_reason(permit,clock,250,'boot'),'session_permit_expired')

    def test_auto_trade_default_remains_paused(self):
        self.assertFalse(celestial.DEFAULTS['auto_trade_enabled'])

class NewsEndpointTests(unittest.TestCase):
    def test_authenticated_endpoint_forwards_index_scope(self):
        tree=ast.parse((ROOT/'kadima/control_api.py').read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='celestial_news')
        method.decorator_list=[]
        for arg in method.args.args: arg.annotation=None
        method.args.defaults=[ast.Constant(None),ast.Constant(None)]
        class HTTPException(Exception):
            def __init__(self,status,detail): self.status_code=status; self.detail=detail
        authenticated=[]
        ns={'__package__':pkg.__name__,'HTTPException':HTTPException,
            '_check_auth':lambda value:authenticated.append(value)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'news_endpoint','exec'),ns)
        body=dict(events=[],source='test-index-feed',observed_at=clock.isoformat(),
                  covered_symbols=['SENSEX'],scope='indices')
        result=ns['celestial_news'](body,'test-auth')
        self.assertTrue(result['ok'])
        self.assertEqual(authenticated,['test-auth'])
        self.assertIn('SENSEX',celestial.health('news_index')['covered_symbols'])
        with self.assertRaises(HTTPException): ns['celestial_news'](dict(body,unexpected=True),'test-auth')

if __name__=='__main__': unittest.main()
