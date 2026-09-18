"""Offline diagnostics tests; all reconciliation broker boundaries are mocked."""
import ast
import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('entry_session_review', ROOT/'tools/review_execution_session.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class EntryDiagnostics(unittest.TestCase):
    def test_saved_controls_and_ledger_are_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db'
            c=sqlite3.connect(path)
            c.execute('CREATE TABLE celestial_config(id INTEGER,payload TEXT)')
            c.execute('INSERT INTO celestial_config VALUES(1,?)',(json.dumps(dict(auto_trade_enabled=False,score_threshold=65,kill_latched=False,api_key='secret')),))
            c.execute('CREATE TABLE execution_orders(side,state,dry_run,filled,created_at)')
            c.execute("INSERT INTO execution_orders VALUES('BUY','pending',0,0,'2026-09-18T10:00:00+05:30')")
            c.commit();c.close()
            before=path.read_bytes()
            out=review.database_summary(path,'2026-09-18')
            self.assertFalse(out['saved_controls']['auto_trade_enabled'])
            self.assertEqual(out['orders_by_side_and_state'],[('BUY','pending',0,1,0)])
            self.assertEqual(out['signals']['status'],'table_or_columns_unavailable')
            self.assertNotIn('secret',json.dumps(out))
            self.assertEqual(path.read_bytes(),before)

    def test_report_explains_failures_before_a_signal_exists(self):
        rows = [
            dict(event='SHIELD_SCORE',index='NIFTY',score=65,components={'private':'secret'}),
            dict(event='CONFIRMATION_GATE',index='NIFTY',passed=False,
                 checks={'score':True,'oi':False,'extra_5m_hold':False,'private':False}),
            dict(event='SHIELD_DISPATCH_BLOCKED',index='NIFTY',reason='four_index_agreement'),
            dict(event='FAST_CANDIDATE',underlying='NIFTY',blocked_reasons=['fast_observation_only']),
            dict(event='gamma_tier_change',index='SENSEX',tier='NONE',score=22),
            dict(event='reconcile_position_quantity_mismatch',response={'secret':'value'}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audit.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in rows)+'\nnull\n[1]\n{bad\n')
            out=review.audit_summary(path)
        self.assertEqual(out['failed_confirmation_checks'],{'oi':1,'extra_5m_hold':1})
        self.assertEqual(out['by_underlying']['NIFTY']['max_shield_score'],65)
        self.assertEqual(out['reason_counts']['fast_observation_only'],1)
        self.assertEqual(out['reason_counts']['four_index_agreement'],1)
        self.assertEqual(out['coverage']['malformed'],3)
        self.assertEqual(out['event_counts']['reconcile_position_quantity_mismatch'],1)
        self.assertNotIn('secret',json.dumps(out))
        self.assertNotIn('private',json.dumps(out))

    def test_legacy_reconciliation_health_matches_existing_entry_block(self):
        # Exercise the actual method with isolated dependencies, never a real broker.
        tree=ast.parse((ROOT/'kadima/auto_executor.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='AutoExecutor')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='reconcile_on_startup')
        fn.decorator_list=[]
        namespace=dict(time=types.SimpleNamespace(monotonic=lambda:100),
            celestial=types.SimpleNamespace(enabled=lambda:False,health=Mock()),
            execution_ledger=types.SimpleNamespace(refresh=Mock(return_value=True),pending=lambda:[],TERMINAL={'complete'}),
            fetch_broker_positions=Mock(return_value=[]),
            angel_orders=types.SimpleNamespace(fetch_order_book=Mock(return_value=[])),audit=types.SimpleNamespace(log=Mock()))
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'reconciliation-fixture','exec'),namespace)
        scenarios=[('broker_books_match',True),('broker_positions_unreadable',False),
                   ('broker_order_book_unreadable',False),('execution_ledger_unresolved',False),
                   ('external_pending_orders',False),('position_quantity_mismatch',False)]
        for reason,ok in scenarios:
            with self.subTest(reason=reason):
                namespace['fetch_broker_positions'].return_value=None if reason=='broker_positions_unreadable' else (
                    [dict(netqty=1,exchange='NFO',symboltoken='T',producttype='INTRADAY')] if reason=='position_quantity_mismatch' else [])
                namespace['angel_orders'].fetch_order_book.return_value=None if reason=='broker_order_book_unreadable' else (
                    [types.SimpleNamespace(status='open',order_id='external',raw={})] if reason=='external_pending_orders' else [])
                namespace['execution_ledger'].refresh.return_value=reason!='execution_ledger_unresolved'
                ex=types.SimpleNamespace(_restored=True,state=types.SimpleNamespace(positions={}),_positions_verified=False)
                namespace['reconcile_on_startup'](ex)
                health=namespace['celestial'].health.call_args.args[1]
                self.assertEqual(health['reason'],reason)
                self.assertEqual(health['ok'],ok)
                self.assertEqual(ex._reconcile_blocked,not ok)


if __name__=='__main__':
    unittest.main()
