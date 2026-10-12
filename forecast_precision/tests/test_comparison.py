import copy
import unittest
from precision_lab.core import EvidenceError, predict
from precision_lab.comparison import baselines, compare
from test_precision import DATA, MODEL

class Comparison(unittest.TestCase):
    def setUp(self):
        self.packet=copy.deepcopy(DATA[-1]['snapshot']); self.f=predict(MODEL,self.packet)
        self.refs=baselines(self.packet,self.f)
    def test_naive_anchor(self):
        self.assertTrue(all(p['forecast']==self.packet['anchor'] for p in self.refs['last_price']['points']))
    def test_same_packet_comparison(self):
        r=compare(self.f,self.refs['causal_trend'],DATA[-1]['actuals'],as_of='2026-01-20T16:00:00+05:30')
        self.assertTrue(r['same_packet']); self.assertFalse(r['promotion_allowed']); self.assertIsNone(r['net_error'])
    def test_other_snapshot_refused(self):
        self.refs['last_price']['snapshot_id']='another'
        with self.assertRaises(EvidenceError): compare(self.f,self.refs['last_price'],{},as_of=self.packet['issued_at'])
    def test_legacy_missing_binding_refused(self):
        del self.refs['last_price']['snapshot_sha256']
        with self.assertRaises(EvidenceError): compare(self.f,self.refs['last_price'],{},as_of=self.packet['issued_at'])
    def test_revision_refused(self):
        self.refs['last_price']['kind']='REVISION'
        with self.assertRaises(EvidenceError): compare(self.f,self.refs['last_price'],{},as_of=self.packet['issued_at'])
    def test_horizon_subset_refused(self):
        self.refs['last_price']['points'].pop()
        with self.assertRaises(EvidenceError): compare(self.f,self.refs['last_price'],{},as_of=self.packet['issued_at'])
    def test_source_packet_mismatch(self):
        self.packet['anchor']+=1
        with self.assertRaises(EvidenceError): baselines(self.packet,self.f)
    def test_baselines_do_not_mutate_candidate(self):
        saved=copy.deepcopy(self.f); baselines(self.packet,self.f); self.assertEqual(saved,self.f)

if __name__=='__main__': unittest.main()
