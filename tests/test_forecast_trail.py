import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from kadima.forecast_trail import Projection, evaluate
from kadima import forecast_trail

NOW=datetime(2026,9,14,7,tzinfo=timezone.utc)

def projection(price, **kw):
    values=dict(token='T',exchange='NFO',signal_id=1,generated_at=NOW,
                market_data_at=NOW,target_at=NOW+timedelta(minutes=30),
                expected_premium=price,model_id='test-only')
    return Projection(**(values|kw))

def decision(price=129, **kw):
    values=dict(entry=100,peak=140,previous_stop=120,ltp=120,quantity=50,
                giveback_fraction=.5,token='T',exchange='NFO',signal_id=1,
                now=NOW,projection=projection(price))
    return evaluate(**(values|kw))

class Policy(unittest.TestCase):
    def test_both_conditions(self):
        d=decision(); self.assertTrue(d.should_exit)
        self.assertEqual(d.risk_amount,1000); self.assertEqual(d.projected_profit,450)
        self.assertFalse(decision(180).should_exit)
        self.assertFalse(decision(121,ltp=121).should_exit)
        self.assertFalse(decision(140).should_exit)  # equal, not strictly less
    def test_gap_does_not_zero_risk(self):
        d=decision(109,ltp=100)
        self.assertTrue(d.should_exit); self.assertEqual(d.risk_amount,1000)
    def test_ratchet_and_quantity(self):
        d=decision(140,peak=160,previous_stop=135,ltp=135,quantity=25)
        self.assertEqual(d.stop,135); self.assertEqual(d.risk_amount,625)
        self.assertEqual(d.projected_profit,125)
    def test_unknown_is_not_zero(self):
        for p in [None,projection(129,token='OTHER'),projection(129,signal_id=2),
                  projection(129,generated_at=NOW-timedelta(minutes=2)),
                  projection(129,market_data_at=NOW-timedelta(minutes=2)),
                  projection(129,target_at=NOW+timedelta(minutes=5)),
                  projection(float('nan')),projection(129,generated_at=NOW+timedelta(seconds=1))]:
            d=decision(projection=p)
            self.assertFalse(d.should_exit); self.assertIsNone(d.projected_profit)
    def test_invalid_prices(self):
        for x in [float('nan'),float('inf'),0,-1]:
            with self.assertRaises(ValueError):decision(ltp=x)

class ExecutorIntegration(unittest.TestCase):
    def run_cycle(self, *, enabled=True, p=projection(129), daily=False, hour=7, ltp=120):
        tree=ast.parse((Path(__file__).parents[1]/'kadima/auto_executor.py').read_text())
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='AutoExecutor')
        method=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='check_exits')
        calls=[]
        pos=SimpleNamespace(status='open',token='T',exchange='NFO',signal_id=1,
                            peak_price=140,entry_price=100,trail_active=True,
                            remaining_qty=50,swing_level=None,direction='PE')
        cfg=SimpleNamespace(FORECAST_TRAIL_ENABLED=enabled,in_no_auto_exit_window=lambda:False)
        ns=dict(config=cfg,forecast_trail=forecast_trail,now_ist=lambda:NOW.replace(hour=hour),
                _poll_ltp=lambda *a:{'T':ltp},EOD_FORCE_CLOSE_HHMM=(15,38),
                exit_manager=SimpleNamespace(realized_pnl_today=lambda:-1000 if daily else 0),
                DAILY_LOSS_LIMIT_INR=500,GIVEBACK_FLOOR_PCT=10,GIVEBACK_TRAIL_PCT=50,
                FIXED_STOPLOSS_PCT=30,storage=SimpleNamespace(upsert_position=lambda p:None),
                audit=SimpleNamespace(log=lambda *a,**k:None))
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<isolated-exit>','exec'),ns)
        selfobj=SimpleNamespace(_lock=threading.Lock(),state=SimpleNamespace(positions={'1':pos}),
                                _profit_projections={1:p} if p else {},
                                _close_position=lambda pos,ltp,reason:calls.append(reason))
        ns['check_exits'](selfobj)
        return calls
    def test_forecast_controls_trail(self):
        self.assertEqual(self.run_cycle(),['forecast_trailing_exit'])
        self.assertEqual(self.run_cycle(p=projection(180)),[])
        self.assertEqual(self.run_cycle(p=None),[])
    def test_default_and_hard_exits(self):
        self.assertEqual(self.run_cycle(enabled=False),['trail_giveback_50pct_of_peak_gain'])
        self.assertEqual(self.run_cycle(p=projection(180),daily=True),['daily_loss_limit_breached'])
        self.assertEqual(self.run_cycle(p=projection(180),hour=16),['eod_force_close'])

if __name__=='__main__':unittest.main()
