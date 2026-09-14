"""End-to-end observer isolation and freshness tests; no external requests."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch,Mock
from zoneinfo import ZoneInfo
from kadima import config,indicators

PKG='_kadima_forecast_test'
pkg=types.ModuleType(PKG);pkg.__path__=[str(Path(__file__).resolve().parents[1]/'kadima')];sys.modules[PKG]=pkg
NOW=datetime(2026,9,14,10,30,tzinfo=ZoneInfo('Asia/Kolkata'))
for name,mod in [('config',config),('indicators',indicators)]:
    sys.modules[PKG+'.'+name]=mod
utils=types.ModuleType(PKG+'.utils');utils.now_ist=lambda:NOW;utils.in_market_hours=lambda:True
sys.modules[PKG+'.utils']=utils
store=types.ModuleType(PKG+'.storage')
store.recent_candles=lambda *a,**k:[dict(start_ts=(NOW-timedelta(minutes=(60-i)*3)).isoformat(),open=25000,high=25020,low=24980,close=25000,volume=100,vwap=25000) for i in range(60)]
sys.modules[PKG+'.storage']=store
obs=importlib.import_module(PKG+'.forecast_observer')
core=importlib.import_module(PKG+'.checklist_forecast')
connection=types.ModuleType(PKG+'.audit');connection.log=Mock();sys.modules[PKG+'.audit']=connection
adapter=importlib.import_module(PKG+'.forecast_connection')


def state(name):
    from types import SimpleNamespace as NS
    sample=dict(mono=995,expiry='17SEP2026',quotes=[dict(token='123',symbol='EXACTCE',strike=25000,opt_type='CE',ltp=200,oi=1000)])
    return NS(name=name,last_updated=NOW,is_live_tick=True,spot=25000,
        chain=NS(index=NS(token=name),_forecast_sample=sample,_alignment_at=990),
        snap=NS(vwap=25000),tier='ACTIVE',score=60,direction='CE',gamma_result=None)


def model_output(data,settings):
    return [dict(name=a['name'],bias='neutral',heat=0,reason='Offline fixture only.',invalidation='Offline fixture.',
        checkpoints=[dict(minute=m,low=24900,expected=25000,high=25100) for m in range(5,61,5)]) for a in data['assets']]

class ObserverTests(unittest.TestCase):
    def test_stale_prices_candles_and_partial_option_samples(self):
        with patch.object(obs.time,'monotonic',return_value=1000):
            st=state('NIFTY')
            asset,option=obs.collect_asset('NIFTY',st,NOW,1000)
            self.assertEqual(option['as_of'],(NOW-timedelta(seconds=5)).isoformat())
            self.assertEqual(asset['option_chain']['ladder_pcr'],0)
            st.chain._forecast_sample=None
            asset,option=obs.collect_asset('NIFTY',st,NOW,1000)
            self.assertIsNone(option)
            self.assertIn('option chain missing/stale (30s maximum)',asset['unavailable'])
            st.last_updated=NOW-timedelta(seconds=31)
            with self.assertRaises(ValueError):obs.collect_asset('NIFTY',st,NOW,1000)
            st.last_updated=NOW+timedelta(seconds=1)
            with self.assertRaises(ValueError):obs.collect_asset('NIFTY',st,NOW,1000)
            st.last_updated=NOW
            with patch.object(store,'recent_candles',return_value=[]):
                with self.assertRaises(ValueError):obs.collect_asset('NIFTY',st,NOW,1000)

    def test_observer_records_real_input_shape_without_executor_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'settings.json').write_text(json.dumps({'enabled':True,'api_key':'private-test-key','model':'test'}))
            ex=types.SimpleNamespace(_lock=threading.RLock(),state=types.SimpleNamespace(positions={
                '1':types.SimpleNamespace(status='open',index='NIFTY',token='123',symbol='EXACTCE',exchange='NFO')}))
            engine=types.SimpleNamespace(states={n:state(n) for n in config.INDICES},stock_worker=types.SimpleNamespace(forecast_state=None))
            o=obs.ForecastObserver(engine,ex)
            with patch.multiple(obs,SETTINGS=root/'settings.json',STATE=root,REPORT=root/'report.json',STATUS=root/'status.json',JOURNAL=root/'journal.sqlite'),patch.object(obs.time,'monotonic',return_value=1000),patch.object(obs,'request_forecast',side_effect=model_output) as request:
                o.step()
                report=json.loads((root/'report.json').read_text())
                self.assertEqual(len(report['assets']),4)
                self.assertEqual(report['premium_scenarios'][0]['symbol'],'EXACTCE')
                self.assertEqual(report['premium_scenarios'][0]['status'],'scenario_only')
                self.assertEqual(report['held_contracts_without_scenario'],[])
                self.assertEqual(adapter.read_projections(root/'report.json'),([], 'observation_only'))
                self.assertFalse((root/'kadima_forecasts.json').exists())
                self.assertNotIn('private-test-key',(root/'report.json').read_text())
                self.assertNotIn('api_key',json.dumps(request.call_args.args[0]))
                o.step();self.assertEqual(request.call_count,1)
                self.assertEqual(len(ex.state.positions),1)
                with o.journal.connect() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM runs').fetchone()[0],1)

    def test_no_key_or_closed_window_does_not_call_provider(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ex=types.SimpleNamespace();engine=types.SimpleNamespace()
            with patch.multiple(obs,SETTINGS=root/'settings.json',STATE=root,REPORT=root/'report.json',STATUS=root/'status.json',JOURNAL=root/'journal.sqlite'),patch.object(obs,'request_forecast') as request:
                o=obs.ForecastObserver(engine,ex);o.step()
                self.assertEqual(json.loads((root/'status.json').read_text())['status'],'waiting_for_api_key')
                (root/'settings.json').write_text(json.dumps({'enabled':True,'api_key':'test'}))
                o._next=0
                with patch.object(obs,'in_market_hours',return_value=False):o.step()
                self.assertEqual(json.loads((root/'status.json').read_text())['status'],'outside_one_hour_forecast_window')
                request.assert_not_called()

if __name__=='__main__':unittest.main()
