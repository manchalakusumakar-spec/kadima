import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from kadima.constituent_radar import IST
from kadima.rest_day_forecast import target_minutes
from kadima.checklist_forecast import Journal, make_report, validate, request_forecast

AT=datetime(2026,10,7,15,15,23,tzinfo=IST)
CLOSE=AT.replace(hour=15,minute=30,second=0)

def inputs(rest=True):
    x=dict(as_of=AT.isoformat(),assets=[dict(name='NIFTY',spot=100,spot_at=AT.isoformat())],availability={})
    if rest:x.update(horizon_kind='rest_of_day',session_close=CLOSE.isoformat())
    return x

def prediction(x):
    points=target_minutes(AT,CLOSE) if x.get('horizon_kind') else range(5,61,5)
    return dict(assets=[dict(name='NIFTY',bias='bullish',heat=1,reason='test',invalidation='test',
        checkpoints=[dict(minute=m,low=100.8,expected=101.,high=101.2) for m in points])])

class RestDayTests(unittest.TestCase):
    def test_exact_close_and_bounded_grid(self):
        start=AT.replace(hour=9,minute=15,second=0)
        grid=target_minutes(start,CLOSE)
        self.assertEqual(len(grid),75)
        self.assertEqual(start+timedelta(minutes=grid[-1]),CLOSE)
        self.assertEqual(AT+timedelta(minutes=target_minutes(AT,CLOSE)[-1]),CLOSE)
        with self.assertRaises(ValueError):target_minutes(CLOSE,CLOSE)

    def test_legacy_and_rest_validation(self):
        for rest in (True,False):
            x=inputs(rest);raw=prediction(x)
            validate(raw,x)
            raw['assets'][0]['checkpoints'].pop()
            with self.assertRaises(ValueError):validate(raw,x)
        x=inputs();raw=prediction(x);raw['assets'][0]['checkpoints'][-1]['minute']=60
        with self.assertRaises(ValueError):validate(raw,x)

    def test_report_windows_and_immutable_journal(self):
        x=inputs();assets=validate(prediction(x),x)
        report=make_report(x,assets,'test',AT+timedelta(seconds=1))
        self.assertEqual(report['expires_at'],CLOSE.isoformat())
        self.assertEqual(report['accuracy_status'],'unverified')
        self.assertEqual(len(report['assets'][0]['movement_windows']),3)
        self.assertNotIn('target_at',assets[0]['checkpoints'][0])
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'test.sqlite');j.record(report,x)
            with self.assertRaises(sqlite3.IntegrityError):j.record(report,x)
            p=report['assets'][0]['checkpoints'][0]
            j.observe({'NIFTY':dict(spot=101,at=p['target_at'])})
            j.observe({'NIFTY':dict(spot=50,at=p['target_at'])})
            stats=j.movement_metrics()
            self.assertEqual(stats['scored_windows'],1)
            self.assertEqual(stats['joint_window_accuracy'],1)
            self.assertEqual(stats['target_status'],'unverified')
            # Skip middle endpoint: final price cannot score an adjacent movement.
            j.observe({'NIFTY':dict(spot=101,at=report['expires_at'])})
            self.assertEqual(j.movement_metrics()['scored_windows'],1)

    def test_late_sample_stays_unscored_and_wide_range_no_joint_hit(self):
        x=inputs();raw=prediction(x)
        raw['assets'][0]['checkpoints'][0].update(low=50,high=150)
        r=make_report(x,validate(raw,x),'test',AT)
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'x');j.record(r,x)
            target=datetime.fromisoformat(r['assets'][0]['checkpoints'][0]['target_at'])
            j.observe({'NIFTY':dict(spot=101,at=(target+timedelta(seconds=31)).isoformat())})
            self.assertEqual(j.movement_metrics()['scored_windows'],0)
            j.observe({'NIFTY':dict(spot=101,at=target.isoformat())})
            self.assertEqual(j.movement_metrics()['joint_window_accuracy'],0)
            self.assertEqual(j.movement_metrics()['endpoint_range_containment'],1)

    def test_provider_payload_uses_exact_grid_without_network(self):
        x=inputs();captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,limit):return json.dumps(dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(prediction(x)))])])).encode()
        def opener(req,timeout):
            captured.append(json.loads(req.data));return Response()
        request_forecast(x,{'api_key':'offline-dummy'},opener=opener)
        self.assertIn('Do not claim 90% accuracy',captured[0]['instructions'])
        self.assertNotIn('return 12 checkpoints',captured[0]['instructions'])

    def test_legacy_report_unchanged_horizon(self):
        x=inputs(False);r=make_report(x,validate(prediction(x),x),'test',AT)
        self.assertEqual(r['expires_at'],(AT+timedelta(hours=1)).isoformat())
        self.assertNotIn('movement_windows',r['assets'][0])

if __name__=='__main__':unittest.main()
