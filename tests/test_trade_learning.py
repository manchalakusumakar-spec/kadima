"""Learning must preserve live execution and distinguish evidence from inference."""
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import test_execution_lifecycle as f
import test_trade_audit as a

l = importlib.import_module(f.PKG+'.trade_learning')


class Learning(a.TradeAudit):
    # Reuse only fixtures; inherited audit tests also exercise the adapters.
    def setUp(self):
        f.Lifecycle.setUp(self)
        with l._lock:
            l._selections.clear(); l._feedback={}; l._forecast={}

    def selected(self):
        q=SimpleNamespace(token='T',symbol='TESTCE',strike=25000,ltp=100,bid=99.8,
                          ask=100.2,delta=.52,iv=20)
        r=SimpleNamespace(quote=q,score=80,rejected=None)
        chain=SimpleNamespace(index=SimpleNamespace(name='INDEX',opt_exchange='NFO'),_last_poll=time.monotonic())
        l.remember_selection(chain,'CE',25000,2,[r])
        return r

    def complete(self,exit_price=110):
        self.selected()
        self.buy()
        self.confirm('BUY',10,102,1)
        with patch.object(l,'now_ist',return_value=f.NOW+timedelta(seconds=30)):
            l.observe('NFO',[('T',120)],f.NOW+timedelta(seconds=30))
        f.ledger.submit(self.pos,'SELL',10,exit_price)
        self.confirm('SELL',10,exit_price,60)
        return l.review(1,f.NOW+timedelta(seconds=61))

    def test_gross_win_not_net_or_causal_and_frozen_decision(self):
        row=self.complete()
        self.assertEqual(row['outcome'],'GROSS_WIN')
        self.assertEqual(row['gross_pnl'],80)
        self.assertIsNone(row['net_pnl'])
        self.assertTrue(row['prospective'])
        self.assertTrue(row['learnable'])
        self.assertEqual(row['cause_status'],'UNCONFIRMED')
        self.assertEqual(row['decision']['plan']['entry'],100)
        self.assertEqual(row['actual_entry'],102)
        self.assertIn('SAMPLED_PROFIT_GIVEBACK',[r['code'] for r in row['should_not']])
        self.assertIn('ENTRY_RR_BELOW_2',[r['code'] for r in row['should_not']])
        self.assertEqual(row['forecast_review']['status'],'NOT_CAPTURED')

    def test_loss_and_breakeven_are_not_suppressed(self):
        row=self.complete(90)
        self.assertEqual((row['outcome'],row['gross_pnl']),('GROSS_LOSS',-120))
        self.assertTrue(row['learnable'])

    def test_partial_fill_and_pending_exit_do_not_train(self):
        self.selected();self.buy()
        self.confirm('BUY',5,100,1,'open')
        row=l.review(1,f.NOW+timedelta(seconds=2))
        self.assertEqual(row['outcome'],'OPEN');self.assertFalse(row['learnable'])
        self.confirm('BUY',10,102,3)
        f.ledger.submit(self.pos,'SELL',10,105)
        self.confirm('SELL',5,105,4,'open')
        row=l.review(1,f.NOW+timedelta(seconds=5))
        self.assertEqual(row['outcome'],'OPEN');self.assertIsNone(row['gross_pnl'])
        self.confirm('SELL',10,110,6)
        self.assertEqual(l.review(1,f.NOW+timedelta(seconds=7))['gross_pnl'],80)

    def test_paper_and_unknown_never_become_losses_or_training(self):
        self.buy(dry=True)
        r=l.review(1,f.NOW)
        self.assertEqual(r['outcome'],'PAPER');self.assertFalse(r['learnable'])
        db=l.connection()
        with db:db.execute("UPDATE execution_orders SET state='unknown'")
        r=l.review(1,f.NOW)
        self.assertEqual(r['outcome'],'UNRESOLVED');self.assertIsNone(r['gross_pnl'])

    def test_rejected_entry_is_unfilled_not_loss(self):
        self.signal();self.pos.remaining_qty=0;self.pos.entry_price=0
        self.place.return_value=f.orders.OrderResult(False,None,'rejected')
        f.ledger.submit(self.pos,'BUY',10,100)
        r=l.review(1,f.NOW)
        self.assertEqual(r['outcome'],'UNFILLED');self.assertFalse(r['learnable'])

    def test_restart_repeat_and_missing_snapshot(self):
        first=self.complete()
        f.storage._local.conn.close();f.storage._local=threading.local()
        second=l.review(1,f.NOW+timedelta(minutes=2))
        self.assertEqual(first['source'],second['source'])
        self.assertEqual(l.connection().execute('SELECT COUNT(*) FROM trade_learning_reviews').fetchone()[0],1)
        with l.connection() as db:db.execute('DELETE FROM trade_learning_decisions')
        second=l.review(1,f.NOW+timedelta(minutes=3))
        self.assertFalse(second['learnable']);self.assertFalse(second['prospective'])
        self.assertEqual(second['gross_pnl'],80)

    def test_stale_future_selection_and_forecast_are_not_evidence(self):
        self.selected()
        with l._lock:
            for value in l._selections.values():value['captured_at']=(f.NOW+timedelta(seconds=1)).isoformat()
            l._forecast=dict(id='future',issued_at=(f.NOW+timedelta(seconds=1)).isoformat(),
                as_of=f.NOW.isoformat(),assets=[{'name':'INDEX'}])
        self.buy()
        evidence=json.loads(l.connection().execute('SELECT evidence FROM trade_learning_decisions').fetchone()[0])
        self.assertIsNone(evidence['selection']);self.assertIsNone(evidence['forecast'])

    def test_fill_hook_failure_cannot_stop_exit(self):
        self.buy();self.confirm('BUY',10,100,1)
        with patch.object(l,'mark_dirty',side_effect=RuntimeError('broken learning')):
            f.ledger.submit(self.pos,'SELL',10,110)
            self.confirm('SELL',10,110,2)
        self.assertEqual(self.pos.status,'closed')
        self.assertEqual(self.pos.remaining_qty,0)

    def test_submission_hook_failure_does_not_prevent_submission(self):
        with patch.object(l,'capture_submission',side_effect=RuntimeError('broken learning')):
            self.buy()
        self.place.assert_called_once()

    def test_owned_samples_exclude_prefill_stale_duplicate_and_postexit(self):
        self.buy()
        l.observe('NFO',[('T',999)],f.NOW,now=f.NOW)
        self.assertEqual(l.connection().execute('SELECT COUNT(*) FROM trade_learning_samples').fetchone()[0],0)
        self.confirm('BUY',10,100,1)
        at=f.NOW+timedelta(seconds=2)
        l.observe('NFO',[('T',110)],at,now=at)
        l.observe('NFO',[('T',999)],at,now=at)
        l.observe('NFO',[('T',999)],at,now=at+timedelta(seconds=31))
        f.ledger.submit(self.pos,'SELL',10,110);self.confirm('SELL',10,110,3)
        l.observe('NFO',[('T',999)],at+timedelta(seconds=4),now=at+timedelta(seconds=4))
        row=l.connection().execute('SELECT * FROM trade_learning_samples').fetchone()
        self.assertEqual((row['high'],row['samples']),(110,1))

    def test_background_recovery_review_and_feedback_no_broker_calls(self):
        self.complete()
        self.place.reset_mock();self.book.reset_mock()
        with l.connection() as db:
            db.execute('DELETE FROM trade_learning_reviews');db.execute('DELETE FROM trade_learning_pending')
        value=l.Learner().step(f.NOW+timedelta(seconds=90))
        self.assertEqual(value['groups'][0]['n'],1)
        self.assertEqual(value['groups'][0]['status'],'COLLECTING')
        self.assertFalse(value['groups'][0]['automatic_rule_change'])
        self.assertEqual(l.feedback(f.NOW+timedelta(seconds=90))['mode'],'ADVISORY')
        self.assertEqual(l.feedback(f.NOW+timedelta(seconds=211))['status'],'STALE_OR_WARMING_UP')
        self.place.assert_not_called();self.book.assert_not_called()

    def test_training_excludes_future_and_uses_strategy_and_contract_profiles(self):
        row=self.complete()
        db=l.connection()
        with db:
            for i in range(2,33):
                r=dict(row,signal_id=i)
                if i==32:r['last_fill_at']=(f.NOW+timedelta(days=1)).isoformat()
                if i==31:r['strategy']='GAMMA'
                if i==30:r['profile']=dict(row['profile'],delta_band='LOW')
                db.execute('INSERT INTO trade_learning_reviews VALUES (?,?,?,?,?,?,?,?)',
                    (i,row['reviewed_at'],'2026-09-14',r['strategy'],'INDEX','TESTCE',r['outcome'],json.dumps(r)))
        groups=l.build_feedback(f.NOW+timedelta(minutes=2))['groups']
        self.assertEqual(sum(g['n'] for g in groups),31)
        self.assertEqual(len(groups),3)
        self.assertTrue(all(g['status']=='COLLECTING' for g in groups))

    def test_learning_guard_does_not_commit_callers_transaction(self):
        db=l.connection();db.execute('BEGIN')
        db.execute("INSERT INTO trade_learning_pending VALUES (999)")
        self.assertIsNone(l.guarded(l.mark_dirty,1000))
        self.assertTrue(db.in_transaction);db.rollback()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM trade_learning_pending').fetchone()[0],0)

    def test_snapshot_features_drop_secrets_and_outcomes(self):
        x=l.features(dict(a.proposal(),api_key='secret',gross_pnl=500,realized_pnl=200,score=float('nan')))
        self.assertNotIn('api_key',x);self.assertNotIn('gross_pnl',x);self.assertIsNone(x['score'])

    def test_forecast_review_matches_run_asset_and_source_sample_time(self):
        journal=Path(self.tmp.name)/'forecast.sqlite'
        with sqlite3.connect(journal) as db:
            db.execute('CREATE TABLE targets (run_id,name,minute,target_at,base,expected,actual,actual_at)')
            t=(f.NOW+timedelta(minutes=5)).isoformat()
            db.executemany('INSERT INTO targets VALUES (?,?,?,?,?,?,?,?)',[
                ('run','INDEX',5,t,100,110,108,t),
                ('run','OTHER',5,t,100,110,50,t),
                ('other-run','INDEX',5,t,100,110,50,t),
                ('run','INDEX',10,(f.NOW+timedelta(minutes=10)).isoformat(),100,115,114,(f.NOW+timedelta(minutes=10)).isoformat()),
                ('run','INDEX',15,t,100,110,109,(f.NOW+timedelta(minutes=6)).isoformat())])
        reference={'id':'run','asset':{'name':'INDEX'}}
        with patch.dict(sys.modules,{f.PKG+'.forecast_observer':SimpleNamespace(JOURNAL=journal)}):
            r=l.forecast_outcomes(reference,f.NOW+timedelta(minutes=6))
        self.assertEqual(r['status'],'SCORED')
        self.assertEqual(len(r['checkpoints']),1)
        self.assertEqual(r['checkpoints'][0]['absolute_error'],2)
        self.assertEqual(r['checkpoints'][0]['unchanged_price_error'],8)

    def test_orphan_and_legacy_without_ledger_do_not_starve_queue(self):
        l.mark_dirty(999)
        self.assertIsNone(l.review(999,f.NOW))
        self.assertEqual(l.connection().execute('SELECT COUNT(*) FROM trade_learning_pending').fetchone()[0],0)
        self.assertEqual(l.review(1,f.NOW)['outcome'],'UNRESOLVED')

    def test_forecast_metrics_deduplicate_and_separate_model_versions(self):
        row=self.complete()
        row['decision']['forecast']={'id':'same-run','model':'test','prompt_version':'v1','asset':{'name':'INDEX'}}
        row['forecast_review']={'checkpoints':[dict(minute=5,actual_at=(f.NOW+timedelta(seconds=60)).isoformat(),
                                                  absolute_error=2,unchanged_price_error=8,direction_match=True)]}
        with l.connection() as db:
            db.execute('UPDATE trade_learning_reviews SET evidence=? WHERE signal_id=1',(json.dumps(row),))
            db.execute('INSERT INTO trade_learning_reviews VALUES (?,?,?,?,?,?,?,?)',
                (2,row['reviewed_at'],'2026-09-14',row['strategy'],'INDEX','TESTCE',row['outcome'],json.dumps(dict(row,signal_id=2))))
        groups=l.build_feedback(f.NOW+timedelta(minutes=2))['forecast_groups']
        self.assertEqual(len(groups),1);self.assertEqual(groups[0]['n'],1)
        self.assertEqual(groups[0]['mae_points'],2);self.assertEqual(groups[0]['status'],'COLLECTING')

    def test_report_validation_and_no_claim_of_worker_readiness(self):
        for kw in ({'limit':201},{'day':'2026-02-31'},{'strategy':'INVALID'},{'offset':-1}):
            with self.assertRaises(ValueError):l.report(**kw)
        r=l.report(day='2026-09-14',now=f.NOW)
        self.assertFalse(r['worker_fresh']);self.assertEqual(r['rows'],[])


if __name__=='__main__':unittest.main()
