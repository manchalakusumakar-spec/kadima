import ast
import importlib
import importlib.util
import multiprocessing
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_execution_lifecycle import ROOT, PKG, config, pkg, session, storage, audit

for k,v in dict(INDICES={},MIN_OI=100,MIN_VOLUME=100,OI_VELOCITY_DROP_PCT_BY_INDEX={},
    OI_VELOCITY_WINDOW_MIN=10,STRIKES_EACH_SIDE=2,ANGEL_API_KEY='test',
    ANGEL_CLIENT_ID='test',ANGEL_PASSWORD='test',ANGEL_TOTP_SECRET='test').items():
    setattr(config,k,v)
chain_module=importlib.import_module(PKG+'.option_chain')


def load_source(name, filename):
    spec=importlib.util.spec_from_file_location(PKG+'.'+name,ROOT/'kadima'/filename)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Integrations(unittest.TestCase):
    def chain(self):
        obj=chain_module.OptionChain(types.SimpleNamespace(name='TEST',opt_exchange='NFO'),None)
        obj.quotes={'T':chain_module.OptionQuote('T','TESTCE',100,'CE',ltp=100,delta=.5,iv=20),
                    'U':chain_module.OptionQuote('U','TESTPE',100,'PE',ltp=100)}
        obj.velocity=Mock()
        return obj

    def test_partial_quote_batch_is_not_restamped(self):
        obj=self.chain()
        with patch.object(session.SESSION.api,'getMarketData',return_value={
            'status':True,'data':{'fetched':[{'symbolToken':'T','ltp':110}]}}):
            self.assertFalse(obj.poll())
        self.assertEqual(obj._last_poll,0)
        self.assertEqual(obj.quotes['T'].ltp,100)
        obj.velocity.add.assert_not_called()

    def test_fresh_quote_invalidates_cached_greeks(self):
        obj=self.chain()
        with patch.object(session.SESSION.api,'getMarketData',return_value={
            'status':True,'data':{'fetched':[{'symbolToken':t,'ltp':110,'opnInterest':1000,'tradeVolume':2000} for t in ['T','U']]}}), \
             patch.object(storage,'insert_oi_snapshot'):
            self.assertTrue(obj.poll())
        self.assertIsNone(obj.quotes['T'].delta)
        self.assertIsNone(obj.quotes['T'].iv)
        self.assertGreater(obj._last_poll,0)

    def test_shared_quote_budget_across_processes(self):
        rate=load_source('actual_rate','rate_limiter.py')
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'budget.db')
            self.assertTrue(rate.SharedQuoteLimiter(path,interval=1).wait_and_allow(.2))
            context=multiprocessing.get_context('fork')
            queue=context.Queue()
            def child():
                queue.put(rate.SharedQuoteLimiter(path,interval=1).wait_and_allow(.05))
            process=context.Process(target=child)
            process.start()
            process.join(2)
            self.assertEqual(process.exitcode,0)
            self.assertFalse(queue.get(timeout=1))

    def test_session_retry_does_not_sleep_through_five_attempts(self):
        pkg.rate_limiter.protect_quote_api=lambda api:None
        mod=load_source('actual_session','session.py')
        obj=mod.Session()
        with patch.object(obj,'_login',side_effect=RuntimeError('failed')) as login:
            with self.assertRaises(RuntimeError):obj.ensure()
            with self.assertRaises(RuntimeError):obj.ensure()
            self.assertEqual(login.call_count,1)

    def test_gamma_retries_same_signal_and_detects_direction_change(self):
        tree=ast.parse((ROOT/'kadima/engine.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(
            isinstance(m,ast.FunctionDef) and m.name=='_score_gamma' for m in n.body))
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_score_gamma')
        results=[types.SimpleNamespace(tier='CONFIRMED',direction=d,score=90,
            components=types.SimpleNamespace(as_dict=lambda:{})) for d in ['CE','CE','PE']]
        execution=Mock()
        saves=Mock(side_effect=[101,102])
        ns=dict(IndexState=object,celestial=types.SimpleNamespace(enabled=lambda:False),gamma=types.SimpleNamespace(score=Mock(side_effect=results),
            GammaSnapshot=lambda **kw:None),audit=audit,killswitch=types.SimpleNamespace(engaged=lambda:False),
            in_market_hours=lambda:True,past_cutoff=lambda _:True,GAMMA_SIGNAL_CUTOFF=(15,35),
            storage=types.SimpleNamespace(insert_signal=saves),json=__import__('json'),
            alerts=types.SimpleNamespace(enqueue=Mock()),auto_executor=types.SimpleNamespace(EXECUTOR=execution))
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'gamma','exec'),ns)
        st=types.SimpleNamespace(name='TEST',chain=None,gamma_result=None,gamma_history=None,
            spot=100,snap=types.SimpleNamespace(above_vwap=True,ema_bull=True))
        owner=types.SimpleNamespace(_dte=lambda st:1,_queue_entry_work=Mock())
        for _ in results:ns['_score_gamma'](owner,st)
        self.assertEqual([c.args[1] for c in owner._queue_entry_work.call_args_list],[101,101,102])
        execution.evaluate_and_enter.assert_not_called()
        self.assertEqual(saves.call_count,2)

if __name__=='__main__':unittest.main()
