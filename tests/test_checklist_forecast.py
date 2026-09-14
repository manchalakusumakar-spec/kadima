import copy
from datetime import datetime,timedelta,timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kadima import checklist_forecast as f

NOW=datetime(2026,9,14,5,0,tzinfo=timezone.utc)

def inputs():
    return {'as_of':NOW.isoformat(),'assets':[{'name':'NIFTY','spot':25000}], 'availability':{'global':'missing'}}

def output():
    return {'assets':[dict(name='NIFTY',bias='bullish',heat=2,reason='Offline test fixture; not a real forecast.',invalidation='Below supplied support.',
        checkpoints=[dict(minute=m,low=24950,expected=25020,high=25060) for m in range(5,61,5)])]}

class ForecastTests(unittest.TestCase):
    def test_valid_response_and_horizon_timestamps(self):
        a=f.validate(output(),inputs())
        report=f.make_report(inputs(),a,'test',NOW+timedelta(seconds=20))
        self.assertEqual(report['mode'],'observation')
        self.assertEqual(f.timestamp(report['assets'][0]['checkpoints'][5]['target_at']),NOW+timedelta(minutes=30))
        self.assertNotIn('projections',report)
        with self.assertRaises(ValueError):
            f.make_report(inputs(),a,'test',NOW+timedelta(seconds=120))

    def test_rejects_bad_or_partial_model_results(self):
        alterations=[lambda d:d['assets'].append(copy.deepcopy(d['assets'][0])),
          lambda d:d['assets'][0].update(name='WRONG'),
          lambda d:d['assets'][0].update(heat=True),
          lambda d:d['assets'][0]['checkpoints'].pop(),
          lambda d:d['assets'][0]['checkpoints'][0].update(expected=float('nan')),
          lambda d:d['assets'][0]['checkpoints'][0].update(low=26000),
          lambda d:d['assets'][0]['checkpoints'][0].update(minute=30),
          lambda d:d['assets'][0]['checkpoints'][0].update(high=50000),
          lambda d:d['assets'][0].update(order='BUY')]
        for change in alterations:
            with self.subTest(change=change):
                d=output();change(d)
                with self.assertRaises(ValueError):f.validate(d,inputs())

    def test_model_wire_format_and_no_credential_in_input(self):
        seen=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(output())}]}]}).encode()
        def opener(req,timeout):
            seen.append(json.loads(req.data));self.assertEqual(timeout,45)
            self.assertEqual(req.full_url,'https://api.openai.com/v1/responses')
            return Response()
        result=f.request_forecast(inputs(),{'api_key':'test-secret','model':'gpt-5-mini'},opener)
        self.assertEqual(result[0]['name'],'NIFTY')
        self.assertFalse(seen[0]['store'])
        self.assertTrue(seen[0]['text']['format']['strict'])
        self.assertNotIn('test-secret',json.dumps(seen))
        self.assertNotIn('tools',seen[0])

    def test_no_key_no_request(self):
        with self.assertRaisesRegex(ValueError,'waiting_for_api_key'):
            f.request_forecast(inputs(),{},lambda *a: self.fail('Network must not run'))

    def test_refusal_and_incomplete_are_not_forecasts(self):
        for response in ({'status':'incomplete'}, {'status':'completed','output':[{'type':'message','content':[{'type':'refusal'}]}]}):
            class R:
                def __enter__(self):return self
                def __exit__(self,*a):pass
                def read(self,n):return json.dumps(response).encode()
            with self.assertRaises(ValueError):
                f.request_forecast(inputs(),{'api_key':'test'},lambda *a,**k:R())

    def contract(self,opt='CE'):
        return dict(name='NIFTY',token='123',exchange='NFO',symbol='EXACT'+opt,opt_type=opt,spot=25000,
            spot_at=NOW.isoformat(),quote_at=NOW.isoformat(),strike=25000,
            premium=f.bs_price(25000,25000,2,.2,opt),expiry_at=(NOW+timedelta(days=2)).isoformat())

    def test_exact_contract_ce_pe_and_iv_sensitivity(self):
        for opt in ('CE','PE'):
            c=self.contract(opt);r=f.premium_scenario(c,output()['assets'][0],NOW)
            self.assertEqual(r['status'],'scenario_only');self.assertEqual(r['token'],'123')
            self.assertLess(r['iv_down_20pct_premium'],r['expected_premium'])
            self.assertLess(r['expected_premium'],r['iv_up_20pct_premium'])
            c['quote_at']=(NOW-timedelta(seconds=31)).isoformat()
            self.assertEqual(f.premium_scenario(c,output()['assets'][0],NOW)['status'],'stale_contract_quote')
        c=self.contract();c['expiry_at']=(NOW+timedelta(minutes=20)).isoformat()
        self.assertEqual(f.premium_scenario(c,output()['assets'][0],NOW)['status'],'expiry_inside_horizon')
        c=self.contract();c['premium']=100000
        self.assertEqual(f.premium_scenario(c,output()['assets'][0],NOW)['status'],'iv_fit_failed')

    def test_outcome_is_future_first_sample_only_and_budget_durable(self):
        with tempfile.TemporaryDirectory() as td:
            j=f.Journal(Path(td)/'journal.db')
            report=f.make_report(inputs(),f.validate(output(),inputs()),'test',NOW+timedelta(seconds=10))
            j.record(report,inputs())
            with self.assertRaises(Exception):j.record(report,inputs())
            target=NOW+timedelta(minutes=5)
            for delay,price in [(-1,24900),(10,25010),(20,25050)]:
                j.observe({'NIFTY':{'at':(target+timedelta(seconds=delay)).isoformat(),'spot':price}})
            m=j.metrics();self.assertEqual(m['scored_checkpoints'],1)
            self.assertEqual(m['by_asset_horizon'][0]['mae_points'],10)
            self.assertEqual(m['by_asset_horizon'][0]['unchanged_price_mae_points'],10)
            j.observe({'NIFTY':{'at':(NOW+timedelta(minutes=10,seconds=31)).isoformat(),'spot':25050}})
            self.assertEqual(j.metrics()['scored_checkpoints'],1)
            self.assertTrue(j.reserve(NOW,1))
            self.assertFalse(f.Journal(Path(td)/'journal.db').reserve(NOW+timedelta(minutes=5),1))

    def test_atomic_private_report(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'report.json';f.write_private(p,{'mode':'observation'})
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertEqual(f.read_json(p)['mode'],'observation')
            with self.assertRaises(ValueError):f.write_private(p,{'bad':float('nan')})
            self.assertEqual(f.read_json(p)['mode'],'observation')

if __name__=='__main__':unittest.main()
