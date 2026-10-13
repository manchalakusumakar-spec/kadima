import copy
import unittest
from precision_lab.causal import EvidenceError
from precision_lab.panel_feed import panel_snapshot

NOW='2026-10-13T14:00:00+05:30'

def report(error):
    return {'forecast_id':'ORI-A','kind':'ORIGINAL','evaluated_at':NOW,
            'rows':[{'target_at':'2026-10-13T13:45:00+05:30','status':'EVALUATED',
                'forecast':24000.0,'actual':24000.0+error,
                'absolute_error':abs(error),'below_5':abs(error)<5}]}

class PanelFeedTests(unittest.TestCase):
    def snap(self,**kw):
        return panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[],original_scores=[],**kw)
    def test_no_accuracy_claim_without_actuals(self):
        v=self.snap()
        self.assertIsNone(v['accuracy']['below_5_fraction'])
        self.assertEqual(v['source_status'],'RESEARCH_READ_ONLY_NOT_LIVE_ENDPOINT')
    def test_strict_five_and_signed_cancellation(self):
        v=panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[],original_scores=[report(5)])
        self.assertEqual(v['accuracy']['below_5_fraction'],0)
        self.assertIsNone(v['net_error'])
    def test_matched_success(self):
        v=panel_snapshot(as_of=NOW,mode='SHADOW_RUNNING',event_rows=[],original_scores=[report(-4.9)])
        self.assertEqual(v['accuracy']['below_5_fraction'],1)
    def test_duplicate_original_refused(self):
        with self.assertRaises(EvidenceError):
            panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[],original_scores=[report(1),report(-1)])
    def test_unverified_score_refused(self):
        r=report(10);r['rows'][0]['below_5']=True
        with self.assertRaises(EvidenceError):
            panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[],original_scores=[r])
    def test_revised_score_refused(self):
        r=report(1);r['kind']='REVISION'
        with self.assertRaises(EvidenceError):
            panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[],original_scores=[r])
    def test_out_of_order_event_sorted(self):
        a={'event':{'trigger_at':'2026-10-13T13:30:00+05:30'},'receipt':None}
        b={'event':{'trigger_at':'2026-10-13T13:35:00+05:30'},'receipt':None}
        v=panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[a,b],original_scores=[])
        self.assertEqual(v['events'][0]['event']['trigger_at'],b['event']['trigger_at'])
    def test_future_event_hidden(self):
        a={'event':{'trigger_at':'2026-10-13T14:05:00+05:30'},'receipt':None}
        v=panel_snapshot(as_of=NOW,mode='CAPTURE_ONLY',event_rows=[a],original_scores=[])
        self.assertEqual(v['events'],[])

if __name__=='__main__': unittest.main()
