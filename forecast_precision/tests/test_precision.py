import copy
from datetime import timedelta
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from precision_lab.core import (EvidenceError, InsufficientEvidence, PathModel, aggregate_constituents,
                                canonical, digest, encode, fit, instant, predict, score, FEATURES)
from precision_lab.journal import Journal
from precision_lab.evaluate import walk_forward
from precision_lab.__main__ import synthetic_records, save

DATA=synthetic_records()
MODEL=fit(DATA,'2026-01-16')

class Inputs(unittest.TestCase):
    def setUp(self): self.s=copy.deepcopy(DATA[-1]['snapshot'])
    def test_valid(self):
        x,q=encode(self.s); self.assertTrue(q['ret_5m_bps']=='AVAILABLE'); self.assertEqual(len(x),2*len(FEATURES)+1)
    def test_timezone_required(self):
        self.s['issued_at']='2026-01-18T10:25:02'
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_instrument_gate(self):
        self.s['instrument']='SENSEX'
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_stale_anchor(self):
        self.s['anchor_observed_at']=(instant(self.s['issued_at'])-timedelta(seconds=31)).isoformat()
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_future_anchor(self):
        self.s['anchor_available_at']=(instant(self.s['issued_at'])+timedelta(seconds=1)).isoformat()
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_unclosed_bar(self):
        self.s['bar_complete']=False
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_future_bar(self):
        self.s['closed_bar_end']=(instant(self.s['origin_at'])+timedelta(minutes=5)).isoformat()
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_missing_required(self):
        del self.s['features']['rsi_centered']
        with self.assertRaises(InsufficientEvidence): encode(self.s)
    def test_future_feature_is_excluded(self):
        self.s['features']['weighted_ret_5m_bps']['available_at']=(instant(self.s['issued_at'])+timedelta(seconds=1)).isoformat()
        _,q=encode(self.s); self.assertEqual(q['weighted_ret_5m_bps'],'FUTURE')
    def test_required_future_feature_blocks(self):
        self.s['features']['ret_5m_bps']['available_at']=(instant(self.s['issued_at'])+timedelta(seconds=1)).isoformat()
        with self.assertRaises(InsufficientEvidence): encode(self.s)
    def test_zero_is_not_missing(self):
        self.s['features']['weighted_ret_5m_bps']['value']=0
        x,_=encode(self.s); del self.s['features']['weighted_ret_5m_bps']; y,_=encode(self.s)
        self.assertNotEqual(x.tolist(),y.tolist())
    def test_nan_rejected(self):
        self.s['features']['rsi_centered']['value']=float('nan')
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_bool_rejected(self):
        self.s['anchor']=True
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_unknown_feature_rejected(self):
        self.s['features']['secret_future_return']={'value':1}
        with self.assertRaises(EvidenceError): encode(self.s)
    def test_previous_session_feature_excluded(self):
        f=self.s['features']['weighted_ret_5m_bps']; f['observed_at']=(instant(f['observed_at'])-timedelta(days=1)).isoformat()
        _,q=encode(self.s); self.assertEqual(q['weighted_ret_5m_bps'],'WRONG_SESSION')
    def test_rsi_domain(self):
        self.s['features']['rsi_centered']['value']=55
        with self.assertRaises(EvidenceError): encode(self.s)

class Constituents(unittest.TestCase):
    def setUp(self):
        self.now='2026-01-18T10:00:00+05:30'
        self.m={'version':'SYNTHETIC-50','source':'SYNTHETIC','effective_date':'2026-01-01',
                'valid_through':'2026-01-31','available_at':'2026-01-01T08:00:00+05:30',
                'weights':{f'S{i}':.02 for i in range(50)}}
        self.q=[{'symbol':f'S{i}','price':101.0,'previous_price':100.0,
                 'observed_at':self.now,'available_at':self.now,
                 'previous_observed_at':'2026-01-18T09:55:00+05:30',
                 'previous_available_at':'2026-01-18T09:55:00+05:30','source':'SYNTHETIC'} for i in range(50)]
    def test_full_coverage(self):
        a=aggregate_constituents(self.m,self.q,self.now)
        self.assertEqual(a['fresh_count'],50); self.assertAlmostEqual(a['weighted_ret_5m_bps'],100)
    def test_missing_names_not_renormalized(self):
        a=aggregate_constituents(self.m,self.q[:25],self.now)
        self.assertAlmostEqual(a['weighted_ret_5m_bps'],50); self.assertAlmostEqual(a['constituent_weight_coverage'],.5)
    def test_duplicate_rejected(self):
        with self.assertRaises(EvidenceError): aggregate_constituents(self.m,self.q+[self.q[0]],self.now)
    def test_wrong_weight_total(self):
        self.m['weights']['S1']=.04
        with self.assertRaises(EvidenceError): aggregate_constituents(self.m,self.q,self.now)
    def test_requires_50_names(self):
        del self.m['weights']['S1']
        with self.assertRaises(EvidenceError): aggregate_constituents(self.m,self.q,self.now)
    def test_expired_weights(self):
        self.m['valid_through']='2026-01-17'
        with self.assertRaises(EvidenceError): aggregate_constituents(self.m,self.q,self.now)
    def test_future_manifest(self):
        self.m['available_at']='2026-01-19T08:00:00+05:30'
        with self.assertRaises(EvidenceError): aggregate_constituents(self.m,self.q,self.now)
    def test_endpoint_alignment(self):
        self.q[0]['previous_observed_at']='2026-01-18T09:54:00+05:30'
        a=aggregate_constituents(self.m,self.q,self.now); self.assertEqual(a['fresh_count'],49)
    def test_past_receipt_after_past_target_but_before_issue_is_causal(self):
        self.q[0]['previous_available_at']='2026-01-18T09:55:02+05:30'
        a=aggregate_constituents(self.m,self.q,self.now); self.assertEqual(a['fresh_count'],50)
    def test_no_quotes_no_fake_return(self):
        a=aggregate_constituents(self.m,[],self.now)
        self.assertIsNone(a['weighted_ret_5m_bps']); self.assertEqual(a['constituent_weight_coverage'],0)

class Models(unittest.TestCase):
    def test_all_horizons_fit(self): self.assertEqual(len(MODEL.coefficients),12)
    def test_no_validation_leakage(self):
        changed=copy.deepcopy(DATA)
        for row in changed:
            if row['snapshot']['session_date']>='2026-01-16':
                row['snapshot']['features']['ret_5m_bps']['value']=1e8
                for a in row['actuals'].values(): a['price']=1e10
        self.assertEqual(fit(changed,'2026-01-16').to_dict(),MODEL.to_dict())
    def test_model_roundtrip(self):
        m=PathModel.from_dict(json.loads(json.dumps(MODEL.to_dict())))
        self.assertEqual(predict(m,DATA[-1]['snapshot']),predict(MODEL,DATA[-1]['snapshot']))
    def test_no_in_sample_forecast(self):
        with self.assertRaises(EvidenceError): predict(MODEL,DATA[0]['snapshot'])
    def test_enough_sessions_not_only_rows(self):
        with self.assertRaises(InsufficientEvidence): fit(DATA[:12],'2026-01-16',min_samples=2,min_sessions=2)
    def test_duplicate_sample(self):
        with self.assertRaises(EvidenceError): fit(DATA+[DATA[0]],'2026-01-16')
    def test_no_exact_target_guess(self):
        rows=copy.deepcopy(DATA); a=rows[0]['actuals']['5']
        a['observed_at']=(instant(a['target_at'])+timedelta(minutes=1)).isoformat()
        with self.assertRaises(EvidenceError): fit(rows,'2026-01-16')
    def test_delayed_label_excluded_from_training(self):
        rows=copy.deepcopy(DATA)
        for r in rows:
            if r['snapshot']['session_date']<'2026-01-16':
                r['actuals']['60']['available_at']='2026-01-20T16:00:00+05:30'
        model=fit(rows,'2026-01-16'); self.assertNotIn('60',model.coefficients)
    def test_path_is_not_endpoint_ramp(self):
        f=predict(MODEL,DATA[-1]['snapshot']); y=[p['forecast'] for p in f['points']]
        self.assertGreater(abs((y[1]-y[0])-(y[2]-y[1])),1)
    def test_real_lead_time_and_round_timestamps(self):
        f=predict(MODEL,DATA[-1]['snapshot'])
        self.assertEqual(f['points'][0]['lead_seconds_at_issue'],298)
        self.assertEqual(instant(f['points'][0]['target_at']).second,0)
    def test_cut_at_market_close(self):
        s=copy.deepcopy(DATA[-1]['snapshot']); old=instant(s['origin_at']); new=old.replace(hour=15,minute=15)
        for k in ['issued_at','origin_at','anchor_observed_at','anchor_available_at','closed_bar_end']:
            s[k]=(instant(s[k])+(new-old)).isoformat()
        for v in s['features'].values():
            for k in ['observed_at','available_at']: v[k]=(instant(v[k])+(new-old)).isoformat()
        f=predict(MODEL,s); self.assertEqual(len(f['points']),3)
        self.assertEqual(instant(f['points'][-1]['target_at']).strftime('%H:%M'),'15:30')
    def test_walk_forward_session_cutoffs(self):
        report=walk_forward(DATA)
        self.assertTrue(report['evaluated_sessions']>0)
        self.assertTrue(all(k>='2026-01-11' for k in report['models_by_session']))
    def test_training_input_order_is_deterministic(self):
        self.assertEqual(fit(list(reversed(DATA)),'2026-01-16').to_dict(),MODEL.to_dict())
    def test_no_network_calls(self):
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            m=fit(DATA,'2026-01-16'); predict(m,DATA[-1]['snapshot'])

class ScoringAndJournal(unittest.TestCase):
    def setUp(self):
        self.f=predict(MODEL,DATA[-1]['snapshot']); self.a=copy.deepcopy(DATA[-1]['actuals'])
        self.asof='2026-01-18T15:30:00+05:30'
    def overwrite_errors(self, errors):
        for p,e in zip(self.f['points'],errors): self.a[str(p['horizon_minutes'])]['price']=p['forecast']+e
    def test_strict_five(self):
        self.overwrite_errors([5]*12)
        r=score(self.f,self.a,as_of=self.asof); self.assertEqual(r['below_5_fraction'],0)
    def test_below_five(self):
        self.overwrite_errors([4.99]*12)
        r=score(self.f,self.a,as_of=self.asof); self.assertEqual(r['below_5_fraction'],1)
    def test_signed_cancellation_not_success(self):
        self.overwrite_errors([10,-10]*6)
        r=score(self.f,self.a,as_of=self.asof)
        self.assertEqual(r['path_mae_points'],10); self.assertEqual(r['below_5_fraction'],0); self.assertIsNone(r['net_error'])
    def test_missing_not_zero_error(self):
        r=score(self.f,{},as_of=self.asof)
        self.assertIsNone(r['below_5_fraction']); self.assertEqual(r['evaluated_points'],0)
    def test_no_future_actual(self):
        r=score(self.f,self.a,as_of=self.f['issued_at']); self.assertEqual(r['evaluated_points'],0)
    def test_incomplete_actual_not_scored(self):
        self.a['5']['complete']=False
        r=score(self.f,self.a,as_of=self.asof); self.assertEqual(r['evaluated_points'],11)
    def test_target_mismatch_rejected(self):
        self.a['5']['observed_at']=self.a['10']['observed_at']
        with self.assertRaises(EvidenceError): score(self.f,self.a,as_of=self.asof)
    def test_idempotent_immutable_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'test.precision-lab.sqlite3'; j=Journal(path)
            self.assertTrue(j.save(self.f)); self.assertFalse(j.save(self.f))
            edited=copy.deepcopy(self.f); edited['points'][0]['forecast']+=1
            with self.assertRaises(EvidenceError): j.save(edited)
            j.close(); j=Journal(path); self.assertEqual(j.get(self.f['id']),self.f); j.close()
    def test_sql_update_delete_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'test.precision-lab.sqlite3'); j.save(self.f)
            for query in ['UPDATE forecasts SET sha256="new"','DELETE FROM forecasts']:
                with self.assertRaises(sqlite3.IntegrityError): j.db.execute(query)
            j.close()
    def test_revision_preserves_original(self):
        original=predict(MODEL,DATA[-2]['snapshot'])
        revision=predict(MODEL,DATA[-1]['snapshot'],parent_id=original['id'])
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'test.precision-lab.sqlite3'); j.save(original); j.save(revision)
            self.assertEqual(j.get(original['id']),original); self.assertEqual(revision['kind'],'REVISION'); j.close()
    def test_same_snapshot_new_id_cannot_replace_evidence(self):
        changed=copy.deepcopy(self.f); changed['id']='DIFFERENT_ID'; changed['points'][0]['forecast']+=10
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'test.precision-lab.sqlite3'); j.save(self.f)
            with self.assertRaises(EvidenceError): j.save(changed)
            j.close()
    def test_research_db_not_production_db(self):
        with self.assertRaises(EvidenceError): Journal('forecast.db')
    def test_output_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'output.json'; save(path,{'a':1})
            with self.assertRaises(FileExistsError): save(path,{'a':2})

if __name__=='__main__': unittest.main()
