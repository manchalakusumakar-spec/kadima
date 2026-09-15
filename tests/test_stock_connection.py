"""Offline stock handoff and forecast adapter tests; no broker orders/network."""
import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
import test_execution_lifecycle as base

PKG='_kadima_stock_test'
pkg=types.ModuleType(PKG);pkg.__path__=[str(base.ROOT/'kadima')];sys.modules[PKG]=pkg

def stub(name,mod=None,**kw):
    if mod is None:
        mod=types.ModuleType(PKG+'.'+name);mod.__dict__.update(kw)
    sys.modules[PKG+'.'+name]=mod;setattr(pkg,name,mod)
    return mod

NOW=datetime(2026,9,14,12,tzinfo=timezone.utc)
cfg=importlib.import_module('kadima.config')
stub('config',cfg)
stub('storage',base.storage)
stub('audit',log=Mock())
stub('utils',now_ist=lambda:NOW,in_market_hours=lambda:True)
stub('session',SESSION=types.SimpleNamespace(ensure=Mock(),api=Mock()))
handoff=importlib.import_module(PKG+'.stock_handoff')
worker=importlib.import_module(PKG+'.stock_execution')
forecast=importlib.import_module(PKG+'.forecast_connection')

class Database(unittest.TestCase):
    setUp=base.Lifecycle.setUp
    tearDown=base.Lifecycle.tearDown
    def test_episode_survives_republish_transient_failure_and_restart(self):
        sid=handoff.publish('STOCK','CE','CONFIRMED',70,{'score':70})
        self.assertEqual(sid,handoff.publish('STOCK','CE','CONFIRMED',80,{'score':80}))
        handoff.invalidate('STOCK')
        self.assertEqual(sid,handoff.publish('STOCK','CE','CONFIRMED',80,{'score':80}))
        handoff.publish('STOCK','CE','ACTIVE',60,{'score':60})
        self.assertNotEqual(sid,handoff.publish('STOCK','CE','CONFIRMED',75,{'score':75}))
    def test_wrong_tier_and_expired_signal_not_consumable(self):
        handoff.publish('STOCK','CE','ACTIVE',90,{'score':90})
        self.assertEqual(handoff.candidates(),[])
        sid=handoff.publish('STOCK','CE','CONFIRMED',70,{'score':70})
        row=handoff.candidates()[0]
        self.assertTrue(handoff.still_current('STOCK',sid,row['updated_at']))
        with patch.object(handoff,'now_ist',return_value=NOW+timedelta(seconds=31)):
            self.assertEqual(handoff.candidates(),[])
        handoff.invalidate('STOCK')
        self.assertFalse(handoff.still_current('STOCK',sid,row['updated_at']))
    def test_stock_cannot_bypass_shared_slot(self):
        chain=types.SimpleNamespace(_entry_guard=lambda:True)
        self.ex.evaluate_and_enter('STOCK','CE',chain,10000,None,1,'gamma_blast',2,'CONFIRMED',90)
        self.place.assert_not_called()
    def test_invalidation_rechecked_in_central_executor(self):
        self.ex.state.positions.clear()
        chain=types.SimpleNamespace(_entry_guard=lambda:False)
        self.ex.evaluate_and_enter('STOCK','CE',chain,10000,None,1,'gamma_blast',2,'CONFIRMED',90)
        self.place.assert_not_called()
        self.assertEqual(base.audit.log.call_args.kwargs['reason'],'stock_signal_expired_or_replaced')
    def test_handoff_reaches_one_confirmed_fill_executor(self):
        self.ex.state.positions.clear()
        self.pos.status='closed'; base.storage.upsert_position(self.pos)
        index=cfg.Index(name='STOCK',exchange='NSE',token='SPOT',opt_exchange='NFO',
                        opt_symbol_root='STOCK',strike_step=50,weight=0)
        master=types.SimpleNamespace(nearest_expiry=lambda *a:'24SEP2026',strikes_around=lambda *a:[
            dict(token='T',symbol='TESTCE',strike=10000,opt_type='CE')])
        quote=worker.OptionQuote('T','TESTCE',10000,'CE',ltp=100,bid=99.5,ask=100.5,
                                oi=6000,volume=2000,delta=.5,iv=80)
        data=dict(index=asdict(index),spot=10000,spot_at=NOW.isoformat(),chain_at=NOW.isoformat(),
                  session_vwap=9990,quotes=[asdict(quote)],days_to_expiry=1,score=75)
        handoff.publish('STOCK','CE','CONFIRMED',75,data)
        w=worker.StockExecutionWorker(self.ex,master)
        w.history=lambda index:{3:[dict(start_ts=(NOW-timedelta(minutes=3)).isoformat())],5:[]}
        snap=types.SimpleNamespace(vwap=9990,ema_fast=10000,ema_slow=9990,rsi=60)
        def rank(chain,*args):
            q=chain.quotes['T'];q.stop_price=90
            return [types.SimpleNamespace(rejected=False,quote=q)]
        base.config.STRIKES_EACH_SIDE=5
        with patch.object(worker.indicators,'compute',return_value=snap), \
             patch.object(self.ex,'_structure',return_value=(9980,1)), \
             patch.object(base.pkg.strike_selector,'rank',rank,create=True), \
             patch.object(base.pkg.risk_budget,'select_within_budget',
                lambda candidates,*a,**k:types.SimpleNamespace(candidate=candidates[0],lots=1),create=True):
            row=handoff.candidates()[0]
            w.process(row);w.process(row)
        self.assertEqual(self.place.call_count,1)
        pos=next(iter(self.ex.state.positions.values()))
        self.assertEqual(pos.status,'entry_pending')
        self.assertEqual(pos.remaining_qty,0)  # acknowledgement is not a fill

    def test_forecast_file_validation_and_clearing(self):
        p=Path(self.tmp.name)/'forecast.json'
        data=dict(schema_version=1,projections=[dict(token='T',exchange='NFO',signal_id=1,
            generated_at=NOW.isoformat(),market_data_at=NOW.isoformat(),
            target_at=(NOW+timedelta(minutes=30)).isoformat(),expected_premium=130,model_id='fixture')])
        self.pos.effective_stop=90
        p.write_text(json.dumps(data))
        self.assertEqual(forecast.refresh(self.ex,p)['accepted'],1)
        data['projections'][0]['token']='OTHER';p.write_text(json.dumps(data))
        self.assertEqual(forecast.refresh(self.ex,p)['accepted'],0)
        self.assertEqual(self.ex._profit_projections,{})
        p.write_text('{bad')
        self.assertEqual(forecast.refresh(self.ex,p)['status'],'invalid_output')
        p.unlink()
        self.assertEqual(forecast.refresh(self.ex,p)['status'],'producer_output_missing')
    def test_stale_forecast_and_wrong_horizon_rejected(self):
        p=Path(self.tmp.name)/'forecast.json'
        for generated,target in [(NOW-timedelta(minutes=2),NOW+timedelta(minutes=28)),
                                 (NOW,NOW+timedelta(minutes=5))]:
            p.write_text(json.dumps(dict(schema_version=1,projections=[dict(token='T',exchange='NFO',signal_id=1,
                generated_at=generated.isoformat(),market_data_at=generated.isoformat(),
                target_at=target.isoformat(),expected_premium=130,model_id='fixture')])))
            self.assertEqual(forecast.refresh(self.ex,p)['accepted'],0)

class History(unittest.TestCase):
    def bars(self):
        return [[(NOW-timedelta(minutes=15-i*5)).isoformat(),100,103,99,101,200] for i in range(3)]
    def test_closed_bars_only(self):
        rows=self.bars()+[[NOW.isoformat(),100,103,99,101,200]]
        self.assertEqual(len(worker.parse_candles(rows,5,NOW)),3)
    def test_missing_stale_invalid_and_naive_candles(self):
        broken=self.bars();broken[1][2]=90
        naive=self.bars();naive[0][0]=NOW.replace(tzinfo=None).isoformat()
        for rows,now in [(self.bars()[::2],NOW),(self.bars(),NOW+timedelta(minutes=8)),
                         (broken,NOW),(naive,NOW),(self.bars()+self.bars(),NOW)]:
            with self.assertRaises(ValueError): worker.parse_candles(rows,5,now)
    def test_chain_preserves_sample_age_and_rejects_metadata_change(self):
        index=cfg.Index(name='STOCK',exchange='NSE',token='SPOT',opt_exchange='NFO',opt_symbol_root='STOCK',strike_step=10,weight=0)
        master=types.SimpleNamespace(nearest_expiry=lambda *a:'24SEP2026',strikes_around=lambda *a:[
            dict(token='T',symbol='STOCK100CE',strike=100,opt_type='CE')])
        w=worker.StockExecutionWorker(Mock(),master)
        q=worker.OptionQuote('T','STOCK100CE',100,'CE')
        data=dict(index=asdict(index),spot=100,chain_at=(NOW-timedelta(seconds=12)).isoformat(),
                  spot_at=NOW.isoformat(),quotes=[asdict(q)])
        chain=w.restore_chain(data)
        self.assertAlmostEqual(worker.time.monotonic()-chain._last_poll,12,places=2)
        data['quotes'][0]['strike']=110
        with self.assertRaises(ValueError):w.restore_chain(data)
        data['chain_at']=(NOW-timedelta(seconds=31)).isoformat()
        with self.assertRaises(ValueError):w.restore_chain(data)
