import ast, unittest, types
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class Tests(unittest.TestCase):
    def run_poll(self, age_after_response=0):
        source=ROOT/'kadima/commodity_watch.py'
        tree=ast.parse(source.read_text())
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CommodityWatch')
        poll=next(n for n in node.body if isinstance(n,ast.FunctionDef) and n.name=='poll')
        started=datetime(2026,10,8,9,19,16,tzinfo=timezone(timedelta(hours=5,minutes=30)))
        mono=[0.0]; captured=[]
        contract={'token':'569900','instrumenttype':'FUTCOM'}
        def fetch(tokens):
            mono[0]+=2.0
            return {'quote_at':started+timedelta(seconds=2-age_after_response)}
        def quotes(contracts,response,received):
            captured.append(received)
            age=(received-response['quote_at']).total_seconds()
            return [contract|{'fresh':0<=age<=90}]
        ns={'now_ist':lambda:started,'monitoring_window':lambda n:True,
            'catalog':lambda rows,day:[contract],'select_futures':lambda c:c,
            'select_options':lambda c,r:[], 'quote_rows':quotes,
            'number':lambda n:n,'PremiumWindow':object,'timedelta':timedelta,
            'monotonic_clock':types.SimpleNamespace(monotonic=lambda:mono[0])}
        exec(compile(ast.Module(body=[poll],type_ignores=[]),str(source),'exec'),ns)
        obj=types.SimpleNamespace(rows=[],windows={})
        ns['poll'](obj,[],fetch,started)
        self.assertTrue(obj.ok)
        return obj,captured,started
    def test_quote_arriving_during_request_is_fresh(self):
        obj,captured,started=self.run_poll()
        self.assertTrue(obj.rows[0]['fresh'])
        self.assertEqual(captured[0],started+timedelta(seconds=2))
        self.assertEqual(obj.checked_at,captured[0].isoformat())
    def test_stale_quote_stays_blocked(self):
        obj,_,_=self.run_poll(91)
        self.assertFalse(obj.rows[0]['fresh'])
    def test_actual_future_quote_stays_blocked(self):
        obj,_,_=self.run_poll(-2)
        self.assertFalse(obj.rows[0]['fresh'])
if __name__=='__main__':unittest.main()
