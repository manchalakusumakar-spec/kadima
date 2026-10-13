"""Session-separated learning, never live code/risk changes."""
import copy
from datetime import date,datetime,timedelta,timezone
import unittest

from precision_lab.core import EvidenceError,instant
from precision_lab.self_learning import (LearningPolicy,learn_next_forecast_bias,
                                          propose_next_shadow_path)

IST=timezone(timedelta(hours=5,minutes=30))
CUTOFF='2026-10-13T08:00:00+05:30'

def records(*,bias=10.0,days=16,perday=6):
    out=[]
    for n in range(days):
        day=date(2026,9,15)+timedelta(days=n)
        for j in range(perday):
            origin=datetime(day.year,day.month,day.day,11,0,tzinfo=IST)+timedelta(minutes=5*j)
            issue=origin+timedelta(seconds=2)
            for h in (5,10,15):
                target=origin+timedelta(minutes=h)
                price=24000.+j
                out.append({'kind':'ORIGINAL','forecast_id':f'ORIG-{n}-{j}',
                  'horizon_minutes':h,'issued_at':issue.isoformat(),
                  'origin_at':origin.isoformat(),'target_at':target.isoformat(),
                  'actual_available_at':(target+timedelta(seconds=2)).isoformat(),
                  'forecast':price,'actual':price+bias+(j%3-1)*.4,
                  'source_verified':True,'complete':True})
    return out

class LearningTests(unittest.TestCase):
    def test_bias_learning_improves_later_sessions(self):
        r=learn_next_forecast_bias(records(),train_before=CUTOFF)
        self.assertEqual(r.model_status,'SHADOW_ELIGIBLE')
        self.assertIn('5',r.corrections)
        self.assertGreater(r.outcomes['5']['before_mae'],r.outcomes['5']['after_mae'])
        self.assertEqual(r.evidence,'HISTORICAL_HOLDOUT_RESEARCH_NOT_LIVE_ACCURACY')
    def test_more_data_does_not_promote_live(self):
        r=learn_next_forecast_bias(records(),train_before=CUTOFF)
        self.assertNotIn('LIVE',r.model_status)
        self.assertNotIn('trading',r.to_dict())
    def test_insufficient_sessions(self):
        r=learn_next_forecast_bias(records(days=5),train_before=CUTOFF)
        self.assertEqual(r.model_status,'INSUFFICIENT_DATA')
    def test_unbiased_samples_do_not_get_auto_corrected(self):
        r=learn_next_forecast_bias(records(bias=0),train_before=CUTOFF)
        self.assertEqual(r.model_status,'NO_VALIDATED_IMPROVEMENT')
    def test_future_sessions_excluded(self):
        original=records()
        changed=copy.deepcopy(original)
        changed.extend(records(days=18)[len(original):])
        # The added records occur after the pre-cutoff dates, so no effect.
        for extra in changed[len(original):]:
            extra['actual']=extra['forecast']+999
        cutoff='2026-10-01T08:00:00+05:30'
        self.assertEqual(learn_next_forecast_bias(original,train_before=cutoff).to_dict(),
                         learn_next_forecast_bias(changed,train_before=cutoff).to_dict())
    def test_revisions_not_training_labels(self):
        r=records(); new=copy.deepcopy(r[0]);new['kind']='REVISION';new['actual']=1e9
        self.assertEqual(learn_next_forecast_bias(r,train_before=CUTOFF).to_dict(),
                         learn_next_forecast_bias(r+[new],train_before=CUTOFF).to_dict())
    def test_duplicate_original_blocks(self):
        r=records()
        with self.assertRaises(EvidenceError):
            learn_next_forecast_bias(r+[copy.deepcopy(r[0])],train_before=CUTOFF)
    def test_future_label_blocks(self):
        r=records();r[0]['actual_available_at']='2026-10-20T00:00:00+05:30'
        with self.assertRaises(EvidenceError): learn_next_forecast_bias(r,train_before=CUTOFF)
    def test_not_source_verified_excluded(self):
        r=records();r[0]['source_verified']=False
        result=learn_next_forecast_bias(r,train_before=CUTOFF)
        self.assertEqual(result.outcomes['5']['training_points'],len([x for x in r if x['horizon_minutes']==5])-1-4*6)
    def test_same_day_fitting_not_allowed(self):
        r=records(days=16);cutoff='2026-09-20T12:00:00+05:30'
        result=learn_next_forecast_bias(r,train_before=cutoff)
        self.assertEqual(result.model_status,'INSUFFICIENT_DATA')
    def test_corrected_shadow_path_does_not_touch_original(self):
        learning=learn_next_forecast_bias(records(),train_before=CUTOFF)
        now='2026-10-13T13:31:00+05:30'
        original={'id':'ORIGINAL-NEW','kind':'ORIGINAL','issued_at':'2026-10-13T13:30:05+05:30',
                  'points':[{'horizon_minutes':5,'target_at':'2026-10-13T13:35:00+05:30','forecast':24000.},
                            {'horizon_minutes':10,'target_at':'2026-10-13T13:40:00+05:30','forecast':24002.}]}
        baseline=copy.deepcopy(original)
        alt=propose_next_shadow_path(original,learning,at=now)
        self.assertEqual(original,baseline)
        self.assertEqual(alt['mode'],'SHADOW_NOT_PUBLISHED')
        self.assertFalse(alt['active_forecast_replaced'])
        self.assertGreater(alt['points'][0]['forecast'],original['points'][0]['forecast'])
    def test_no_retroactive_shadow_points(self):
        learning=learn_next_forecast_bias(records(),train_before=CUTOFF)
        original={'id':'ORI','kind':'ORIGINAL','issued_at':'2026-10-13T13:30:05+05:30',
                  'points':[{'horizon_minutes':5,'target_at':'2026-10-13T13:35:00+05:30','forecast':24000.},
                            {'horizon_minutes':10,'target_at':'2026-10-13T13:40:00+05:30','forecast':24003.}]}
        alt=propose_next_shadow_path(original,learning,at='2026-10-13T13:37:00+05:30')
        self.assertEqual(len(alt['points']),1)
        self.assertEqual(alt['points'][0]['horizon_minutes'],10)
    def test_no_same_session_training_leakage(self):
        learning=learn_next_forecast_bias(records(),train_before=CUTOFF)
        original={'id':'ORI','kind':'ORIGINAL','issued_at':'2026-09-20T13:30:05+05:30',
                  'points':[{'horizon_minutes':5,'target_at':'2026-09-20T13:35:00+05:30','forecast':24000.}]}
        with self.assertRaises(EvidenceError):
            propose_next_shadow_path(original,learning,at='2026-09-20T13:31:00+05:30')
    def test_no_shadow_when_not_validated(self):
        learning=learn_next_forecast_bias(records(bias=0),train_before=CUTOFF)
        with self.assertRaises(EvidenceError):
            propose_next_shadow_path({'kind':'ORIGINAL','issued_at':CUTOFF,'points':[]},learning,at=CUTOFF)

if __name__=='__main__': unittest.main()
