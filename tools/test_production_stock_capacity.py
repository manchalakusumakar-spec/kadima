import ast, pathlib, sys, threading, types, unittest
from types import SimpleNamespace as NS
ROOT=pathlib.Path(__file__).resolve().parents[1]
def method(path, cls, name, ns):
    node=next(x for x in ast.parse(path.read_text()).body if isinstance(x,ast.ClassDef) and x.name==cls)
    fn=next(x for x in node.body if isinstance(x,ast.FunctionDef) and x.name==name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),str(path),'exec'),ns)
    return ns[name]
class Once:
    def __init__(self):self.done=False
    def is_set(self):return self.done
    def wait(self,n):self.done=True
class Tests(unittest.TestCase):
    def cycle(self, positions, pending=(), blocked=False, fill=False):
        package=types.ModuleType('audit_fixture');package.__path__=[]
        ledger=types.ModuleType('audit_fixture.execution_ledger');ledger.pending=lambda:list(pending)
        cap=types.ModuleType('audit_fixture.portfolio_capacity');sys.modules[cap.__name__]=cap
        exec((ROOT/'kadima/portfolio_capacity.py').read_text(),cap.__dict__)
        package.execution_ledger=ledger;package.portfolio_capacity=cap
        sys.modules[package.__name__]=package;sys.modules[ledger.__name__]=ledger;sys.modules[cap.__name__]=cap
        calls=[]
        ex=NS(_lock=threading.RLock(),_reconcile_blocked=blocked,state=NS(positions=dict(positions)))
        worker=NS(executor=ex,_stop=Once(),update_held=lambda:calls.append('protect'))
        def process(row):
            calls.append(row['name'])
            if fill:ex.state.positions[row['name']]=NS(status='open',remaining_qty=1,exchange='NFO',token=row['name'])
        worker.process=process
        ns={'__package__':'audit_fixture','in_market_hours':lambda:True,'stock_handoff':NS(candidates=lambda:[{'name':'A'},{'name':'B'}]),'audit':NS(log=lambda *a,**k:None)}
        method(ROOT/'kadima/stock_execution.py','StockExecutionWorker','run',ns)(worker)
        return calls
    def test_filled_position_does_not_restore_one_position_limit(self):
        p=NS(status='open',remaining_qty=1,exchange='NFO',token='held')
        self.assertEqual(self.cycle({'held':p}),['protect','A','B'])
    def test_new_filled_position_does_not_skip_remaining_candidates(self):
        self.assertEqual(self.cycle({},fill=True),['A','B'])
    def test_pending_order_blocks_preparation(self):self.assertEqual(self.cycle({},[{}]),[])
    def test_unknown_position_fails_closed(self):self.assertEqual(self.cycle({'x':NS(status='open',remaining_qty=0)}),['protect'])
    def test_reconcile_block_preserved(self):self.assertEqual(self.cycle({},blocked=True),[])
if __name__=='__main__':unittest.main()
