"""Synthetic regressions for volume conservation, callback timing and verification."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from kadima import candles, indicators
from kadima.config import IST
from tools import verify_candles as verify

START = datetime(2026,9,22,9,15,tzinfo=IST)


class CandleQuality(unittest.TestCase):
    def setUp(self):
        self.writes=[]
        self.patch=patch.object(candles.storage,'upsert_candle',side_effect=lambda *r:self.writes.append(r))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.b=candles.CandleBuilder('TEST')

    def tick(self, seconds, price, volume):
        self.b.add_tick(START+timedelta(seconds=seconds),price,volume)

    def test_boundary_volume_and_single_session_accumulator(self):
        for args in [(0,100,100),(30,110,110),(60,130,120),(90,120,130)]:
            self.tick(*args)
        self.assertEqual(self.b.session_vwap(),120)
        self.assertEqual(self.b._sess_vol,30)
        self.assertEqual(self.b.live(1).volume,20)
        self.assertEqual(self.b.live(3).volume,30)
        self.assertEqual(self.b.live(5).volume,30)
        self.assertEqual(len([r for r in self.writes if r[-1]]),1)
        self.assertEqual(self.writes[-1][-1],False)

    def test_zero_cumulative_baseline_and_missing_volume(self):
        self.tick(0,100,0)
        self.tick(1,110,10)
        self.assertEqual(self.b.session_vwap(),110)
        self.tick(2,120,None)
        self.tick(3,130,1000)
        self.assertIsNone(self.b.session_vwap())
        self.assertIsNone(self.b.live(5).vwap)
        self.assertEqual(self.b.live(5).volume,10)

    def test_no_volume_is_not_a_vwap_price(self):
        self.tick(0,100,None)
        self.assertIsNone(self.b.live(5).vwap)
        row=dict(close=100,high=100,low=100,volume=0,vwap=None)
        self.assertIsNone(indicators.compute([row]).vwap)

    def test_late_tick_cannot_change_price_or_reset_session(self):
        self.tick(0,100,0)
        self.tick(90,120,20)
        self.tick(45,50,10)
        self.tick(-86400,30,1000)
        self.assertEqual(self.b.live(1).close,120)
        self.assertEqual(self.b.live(1).low,120)
        self.assertEqual(self.b._sess_vol,20)

    def test_utc_timestamp_normalized_before_session_bucketing(self):
        self.b.add_tick(START.astimezone(timezone.utc),100,0)
        self.assertEqual(self.b.live(5).start,START)

    def test_callbacks_do_not_see_next_bar_volume(self):
        seen=[]
        self.b.on_close=lambda c:seen.append((c.interval,self.b.session_vwap()))
        self.tick(0,100,0)
        self.tick(30,110,10)
        self.tick(300,200,100)
        self.assertEqual(seen,[(1,110),(3,110),(5,110)])
        self.assertEqual(self.b.session_vwap(),191)

    def test_cumulative_reset_does_not_invent_volume(self):
        self.tick(0,100,100)
        self.tick(1,110,110)
        self.tick(2,120,0)
        self.tick(3,120,5)
        self.assertEqual(self.b._sess_vol,15)
        self.assertIsNone(self.b.session_vwap())
        self.tick(86400,200,0)
        self.tick(86401,210,10)
        self.assertEqual(self.b.session_vwap(),210)

    def test_invalid_prices_and_naive_time_ignored(self):
        for price in (None,True,0,-1,float('nan'),float('inf')):
            self.tick(0,price,0)
        self.b.add_tick(START.replace(tzinfo=None),100,0)
        self.assertIsNone(self.b.live(5))


class Verification(unittest.TestCase):
    def setUp(self):
        self.local=[dict(start_ts=START.isoformat(),open=100,high=120,low=90,close=105,volume=50,closed=1)]
        self.broker={'status':True,'data':[[START.isoformat(),100,120,90,105,50]]}

    def compare(self, **kw):
        return verify.compare_candles(self.local,self.broker,START,START+timedelta(minutes=5),now=START+timedelta(minutes=10),**kw)

    def test_all_ohlcv_fields_checked(self):
        self.assertTrue(self.compare()['ok'])
        for key,value in [('open',101),('high',121),('low',89),('close',106),('volume',51)]:
            old=self.local[0][key]
            self.local[0][key]=value
            self.assertIn(':'+key,' '.join(self.compare()['issues']))
            self.local[0][key]=old

    def test_missing_either_side_and_both_empty_fail(self):
        self.local=[]
        self.assertFalse(self.compare()['ok'])
        self.broker['data']=[]
        self.assertFalse(self.compare()['ok'])

    def test_duplicates_unclosed_and_bad_response_fail(self):
        self.local*=2
        self.assertFalse(self.compare()['ok'])
        self.local=self.local[:1]
        self.local[0]['closed']=0
        self.assertFalse(self.compare()['ok'])
        self.local[0]['closed']=1
        self.broker['status']=False
        self.assertFalse(self.compare()['ok'])

    def test_utc_equivalence_and_exclusive_end(self):
        self.broker['data'][0][0]=START.astimezone(timezone.utc).isoformat()
        self.broker['data'].append([(START+timedelta(minutes=5)).isoformat(),100,120,90,105,50])
        self.assertTrue(self.compare()['ok'])

    def test_nonfinite_and_naive_values_fail(self):
        for value in (None,True,float('nan'),float('inf')):
            self.local[0]['open']=value
            self.assertFalse(self.compare()['ok'])
        self.local[0]['open']=100
        self.local[0]['start_ts']=START.replace(tzinfo=None).isoformat()
        self.assertFalse(self.compare()['ok'])

    def test_incomplete_window_rejected(self):
        with self.assertRaises(ValueError):
            verify.compare_candles([],self.broker,START,START+timedelta(minutes=5),now=START)

    def test_date_query_has_no_recent_200_bar_limit_and_is_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'history.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE candles(token,interval,start_ts,closed)')
                db.execute('INSERT INTO candles VALUES (?,?,?,?)',('T',5,START.isoformat(),1))
                db.executemany('INSERT INTO candles VALUES (?,?,?,?)',[
                    ('T',5,(START+timedelta(days=1,minutes=5*i)).isoformat(),1) for i in range(250)])
            before=path.read_bytes()
            self.assertEqual(len(verify.local_window(path,'T',START,START+timedelta(minutes=5))),1)
            self.assertEqual(path.read_bytes(),before)
            missing=Path(folder)/'missing.db'
            with self.assertRaises(sqlite3.OperationalError):
                verify.local_window(missing,'T',START,START+timedelta(minutes=5))
            self.assertFalse(missing.exists())

    def test_cli_failure_returns_nonzero_without_creating_db(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            path=Path(folder)/'missing.db'
            result=verify.main(['NIFTY','2026-09-22','--db',str(path)])
            self.assertEqual(result,1)
            self.assertFalse(path.exists())
