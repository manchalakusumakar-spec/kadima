"""Persistent sampled history: real SQLite, synthetic quotes, no broker calls."""
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kadima import price_history as h

OPEN = datetime(2026,9,24,9,15,tzinfo=h.IST)


def sample(at=OPEN, price=100, **kw):
    return dict(exchange='NFO',token='1',symbol='TEST29SEP26100CE',underlying='TEST',
                asset='OPTION',expiry='29SEP2026',strike=100,side='CE',
                at=at,received_at=at,price=price,volume=100,
                basis='EXCHANGE',opening_price=100,previous_close=95,**kw)


class Prices(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'prices.db'
        self.writer=h.Recorder(self.path)

    def add(self, at=OPEN, price=100, **changes):
        row=sample(at,price);row.update(changes)
        self.assertTrue(self.writer.submit(row,now=at))

    def flush(self, at=OPEN):
        self.assertTrue(self.writer.flush(at))

    def row(self, at=OPEN, day='2026-09-24'):
        return h.report(day,path=self.path,now=at)['rows'][0]

    def test_restart_keeps_morning_open_first_sample_and_extrema(self):
        self.add();self.add(OPEN+timedelta(minutes=1),140);self.flush(OPEN+timedelta(minutes=1))
        self.writer=h.Recorder(self.path)
        noon=OPEN.replace(hour=12)
        self.add(noon,125,opening_price=None,previous_close=None)
        self.flush(noon)
        row=self.row(noon)
        self.assertEqual((row['opening_price'],row['first'],row['high'],row['last']),(100,100,140,125))
        self.assertEqual(row['first_at'],OPEN.isoformat())
        self.assertEqual(row['move_from_open_pct'],25)
        self.assertEqual(row['high_at'],(OPEN+timedelta(minutes=1)).isoformat())
        self.assertEqual(row['minutes_recorded'],3)

    def test_late_start_never_calls_first_sample_open_and_later_reference_is_separate(self):
        noon=OPEN.replace(hour=12)
        self.add(noon,120,basis='RECEIVED')
        self.flush(noon)
        row=self.row(noon)
        self.assertIsNone(row['opening_price'])
        self.assertIsNone(row['move_from_open_pct'])
        self.assertEqual(row['first_at'],noon.isoformat())
        later=noon+timedelta(seconds=1)
        self.add(later,130);self.flush(later)
        row=self.row(later)
        self.assertEqual(row['first'],120)
        self.assertEqual(row['opening_price'],100)
        self.assertEqual(row['move_from_open_pct'],30)
        self.assertAlmostEqual(row['move_from_first_pct'],8.3333)

    def test_conflicting_open_or_previous_close_suppresses_reference_returns(self):
        self.add();self.flush()
        self.add(OPEN+timedelta(seconds=1),120,opening_price=101)
        self.flush(OPEN+timedelta(seconds=1))
        row=self.row()
        self.assertTrue(row['reference_conflict'])
        self.assertEqual(row['opening_price'],100)
        self.assertIsNone(row['move_from_open_pct'])
        self.assertIsNone(row['move_from_previous_close_pct'])
        self.assertEqual(row['move_from_first_pct'],20)

    def test_invalid_stale_future_cross_day_and_out_of_session_are_rejected(self):
        for changes in ({'price':float('nan')},{'price':True},{'price':0},
                        {'at':OPEN-timedelta(seconds=31)}, {'at':OPEN+timedelta(seconds=1)},
                        {'received_at':OPEN-timedelta(days=1)}, {'at':'2026-09-24T09:15:00'},
                        {'token':''},{'exchange':'FAKE'}, {'at':OPEN.replace(hour=8),'received_at':OPEN.replace(hour=8)}):
            with self.subTest(changes=changes):
                row=sample();row.update(changes)
                self.assertFalse(self.writer.submit(row,now=OPEN))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.writer.status()['accepted'],0)

    def test_coalescing_preserves_extrema_and_does_not_touch_disk_in_callback(self):
        for i in range(10000):
            self.add(OPEN+timedelta(microseconds=i),100+i%20)
        self.assertEqual(len(self.writer.pending),1)
        self.assertFalse(self.path.exists())
        self.flush()
        row=self.row()
        self.assertEqual((row['first'],row['high'],row['low'],row['samples']),(100,119,100,10000))

    def test_memory_limit_records_dropped_observations(self):
        self.writer=h.Recorder(self.path,max_pending=2)
        self.add();self.add(token='2')
        self.assertFalse(self.writer.submit(dict(sample(),token='3'),now=OPEN))
        self.assertEqual(self.writer.status()['dropped'],1)
        self.assertEqual(len(self.writer.pending),2)

    def test_two_writers_preserve_old_high_and_new_quote(self):
        other=h.Recorder(self.path)
        newer=OPEN+timedelta(seconds=20)
        other.submit(sample(newer,110),now=newer)
        self.add(OPEN+timedelta(seconds=10),150)
        self.assertTrue(other.flush(newer))
        self.flush(newer)
        row=self.row(newer)
        self.assertEqual((row['first'],row['last'],row['high']),(150,110,150))
        self.assertEqual(row['last_at'],newer.isoformat())

    def test_day_exchange_and_contract_identity_do_not_mix(self):
        self.add();self.add(exchange='BFO');self.add(symbol='TEST29OCT26100CE')
        self.flush()
        tomorrow=OPEN+timedelta(days=1)
        self.add(tomorrow,200,opening_price=200);self.flush(tomorrow)
        self.assertEqual(h.report('2026-09-24',path=self.path)['total'],3)
        self.assertEqual(self.row(tomorrow,'2026-09-25')['opening_price'],200)

    def test_disk_failure_retains_bounded_batch_and_retry_is_idempotent(self):
        self.add()
        with patch.object(h.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            self.assertFalse(self.writer.flush(OPEN))
        self.assertEqual(len(self.writer.pending),1)
        self.assertIsNone(self.writer.last_success)
        self.flush();self.flush()
        self.assertEqual(self.row()['samples'],1)
        self.assertEqual(self.writer.status()['failures'],1)

    def test_storage_budget_is_enforced_without_silent_batch_loss(self):
        self.add()
        with patch.object(h,'MAX_DB_BYTES',0):
            self.assertFalse(self.writer.flush(OPEN))
        self.assertEqual(self.writer.last_error,'history_storage_budget_reached')
        self.assertEqual(len(self.writer.pending),1)

    def test_volume_reset_and_missing_minutes_remain_visible(self):
        self.add(volume=1000)
        self.add(OPEN+timedelta(seconds=20),110,volume=900)
        self.add(OPEN+timedelta(minutes=2),120,volume=950)
        self.flush(OPEN+timedelta(minutes=3))
        result=h.history('2026-09-24',self.row()['key'],path=self.path,now=OPEN+timedelta(minutes=4))
        self.assertEqual(len(result['bars']),2)
        self.assertIsNone(result['bars'][0]['volume_delta'])
        self.assertIsNone(result['sampled_close_returns_pct']['1'])
        self.assertEqual(self.row()['max_gap_seconds'],100)

    def test_sampled_minute_returns_require_contiguous_observations(self):
        for minute in range(4):
            for second in (0,20,40,59):
                self.add(OPEN+timedelta(minutes=minute,seconds=second),100+minute)
        end=OPEN+timedelta(minutes=4)
        self.flush(end)
        result=h.history('2026-09-24',self.row()['key'],path=self.path,now=end)
        self.assertEqual(result['sampled_close_returns_pct']['3'],3)
        self.assertIsNone(result['sampled_close_returns_pct']['5'])
        self.assertEqual(result['returns_asof'],(end-timedelta(seconds=1)).isoformat())
        self.assertEqual(len(result['bars']),4)

    def test_retention_preserves_daily_baseline_after_minutes_expire(self):
        self.add();self.flush()
        later=OPEN+timedelta(days=8)
        self.flush(later)
        result=h.history('2026-09-24',self.row()['key'],path=self.path,now=later)
        self.assertEqual(result['reference']['first'],100)
        self.assertEqual(result['bars'],[])
        self.flush(OPEN+timedelta(days=181))
        self.assertEqual(h.report('2026-09-24',path=self.path)['total'],0)

    def test_read_only_empty_reads_do_not_create_storage_or_worker(self):
        self.assertEqual(h.report('2026-09-24',path=self.path)['total'],0)
        self.assertEqual(h.history('2026-09-24','a'*32,path=self.path)['bars'],[])
        self.assertFalse(self.path.exists())

    def test_pagination_filters_and_csv_formula_neutralization(self):
        for i in range(205):
            self.add(token=str(i),symbol='=TEST'+str(i))
        self.flush()
        result=h.report('2026-09-24',symbol='=TEST',limit=50,offset=200,path=self.path)
        self.assertEqual((result['total'],len(result['rows']),result['has_more']),(205,5,False))
        exported=h.export('2026-09-24',path=self.path)
        self.assertEqual(exported['total'],205)
        rows=list(csv.DictReader(io.StringIO(exported['content'])))
        self.assertTrue(rows[0]['symbol'].startswith("'=TEST"))
        for args in ({'day':'2026-02-31'},{'asset':'BROKER'},{'limit':201},{'offset':-1}):
            with self.assertRaises(ValueError):h.report(path=self.path,**args)

    def test_adapters_use_mapped_identity_and_distinguish_exchange_time(self):
        captured=[]
        with patch.object(h,'capture',side_effect=lambda row,now:captured.append(h.normalize(row,now))):
            quote=dict(symbolToken='1',exchange='NFO',tradingSymbol='WRONG',ltp=110,
                       open=100,close=95,exchFeedTime='24-Sep-2026 09:15:00')
            h.full_quote(quote,exchange='NFO',token='1',symbol='CORRECT',underlying='TEST',asset='OPTION',now=OPEN)
            quote.pop('exchFeedTime')
            h.full_quote(quote,exchange='NFO',token='1',symbol='CORRECT',underlying='TEST',asset='OPTION',now=OPEN)
            h.websocket(dict(token='1',exchange_timestamp=OPEN.timestamp()*1000,last_traded_price=11000,
                             open_price_of_the_day=10000,closed_price=9500),exchange='NSE',token='1',
                        symbol='TEST',underlying='TEST',asset='STOCK',now=OPEN)
        self.assertEqual(captured[0]['symbol'],'CORRECT')
        self.assertEqual(captured[0]['opening_price'],100)
        self.assertIsNone(captured[1]['opening_price'])
        self.assertEqual(captured[2]['first'],110)
        self.assertEqual(captured[2]['previous_close'],95)

    def test_background_shutdown_commits_pending_reference(self):
        with patch.object(h,'now_ist',return_value=OPEN):
            self.writer.start()
            self.add()
            self.writer.close()
        self.assertFalse(self.writer.thread.is_alive())
        self.assertEqual(self.row()['opening_price'],100)

    def test_adapter_failure_does_not_escape_into_the_scanner(self):
        with patch.object(h,'capture',side_effect=RuntimeError('disk failure')):
            self.assertFalse(h.full_quote(dict(symbolToken='1',ltp=100),exchange='NFO',
                token='1',symbol='TEST',underlying='TEST',asset='OPTION',now=OPEN))

    def test_successful_option_poll_records_history_without_gamma_qualification(self):
        from kadima import option_chain as oc, trade_audit
        chain=oc.OptionChain.__new__(oc.OptionChain)
        chain.index=SimpleNamespace(name='TEST',opt_exchange='NFO')
        chain.quotes={'1':oc.OptionQuote('1','TEST29SEP26100CE',100,'CE')}
        chain.velocity=Mock();chain._expiry='29SEP2026'
        full=dict(symbolToken='1',exchange='NFO',ltp=120,open=100,close=95,
                  opnInterest=0,tradeVolume=1,exchFeedTime='24-Sep-2026 09:15:00')
        with patch.object(oc.storage,'insert_oi_snapshot'), \
             patch.object(trade_audit,'safe_call'), \
             patch.object(oc,'datetime',SimpleNamespace(now=lambda zone:OPEN)), \
             patch.object(h,'capture',side_effect=lambda row,now:self.writer.submit(row,now)):
            self.assertTrue(chain.accept_full_sample([full]))
        self.flush()
        row=self.row()
        self.assertEqual((row['opening_price'],row['first'],row['asset']),(100,120,'OPTION'))
        self.assertFalse(chain.quotes['1'].tradeable)


if __name__=='__main__':unittest.main()
