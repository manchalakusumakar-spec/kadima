import ast,pathlib,types,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Tests(unittest.TestCase):
    def test_forced_login_failure_is_logged_and_loop_continues(self):
        tree=ast.parse((ROOT/'kadima/feed.py').read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='TickFeed');fn=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='_watchdog_loop')
        events=[];checks=[False,True]
        def ensure(**kw):raise RuntimeError('fixture login failure')
        ns={'time':types.SimpleNamespace(sleep=lambda n:None,monotonic=lambda:100),'in_market_hours':lambda:True,'WS_MAX_RETRIES':0,'WS_HEARTBEAT_SEC':12,'WS_RECONNECT_BASE_SEC':1,'WS_RECONNECT_MAX_SEC':30,'SESSION':types.SimpleNamespace(ensure=ensure),'audit':types.SimpleNamespace(log=lambda e,**k:events.append(e))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'watchdog','exec'),ns)
        worker=types.SimpleNamespace(_stop=types.SimpleNamespace(is_set=lambda:checks.pop(0)),_last_tick_at=0,_ws=None,_retries=2,_teardown=lambda:None,_connect=lambda:self.fail('connect must wait for a valid session'))
        ns['_watchdog_loop'](worker)
        self.assertIn('ws_reconnect_failed',events)
        self.assertEqual(checks,[])
if __name__=='__main__':unittest.main()
