import copy
from pathlib import Path
import sqlite3
import tempfile
from threading import Lock
import time
import unittest
from unittest.mock import patch
from precision_lab.host_bridge import CandleReader, frozen_engine, map_enrichments, ObservationHook
from precision_lab.core import EvidenceError, InsufficientEvidence, encode
from test_cache_adapter import fixture, NOW, ORIGIN


class HostBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'native.sqlite3'
        self.engine, self.manifest, self.bars = fixture()
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE candles(token TEXT,interval INTEGER,start_ts TEXT,open REAL,high REAL,low REAL,close REAL,closed INTEGER,PRIMARY KEY(token,interval,start_ts))')
        for row in self.bars:
            db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)', ('N',row['interval'],row['start_ts'],row['open'],row['high'],row['low'],row['close'],row['closed']))
        db.commit(); db.close()

    def test_bounded_completed_reader_no_mutation(self):
        before = self.path.read_bytes()
        rows = CandleReader(self.path)('N', 10, ORIGIN)
        self.assertEqual(len(rows),10)
        self.assertTrue(all(r['closed']==1 for r in rows))
        self.assertEqual(before,self.path.read_bytes())

    def test_reader_missing_never_creates_database(self):
        missing = self.path.parent/'missing'
        with self.assertRaises(FileNotFoundError): CandleReader(missing)
        self.assertFalse(missing.exists())

    def test_invalid_limit(self):
        for n in (0,181,100000):
            with self.assertRaises(EvidenceError): CandleReader(self.path)('N',n,ORIGIN)

    def test_lock_required_and_contended_bounded(self):
        with self.assertRaises(EvidenceError): frozen_engine(self.engine,None)
        lock=Lock(); lock.acquire()
        start=time.monotonic()
        with self.assertRaises(EvidenceError): frozen_engine(self.engine,lock)
        self.assertLess(time.monotonic()-start,.1)
        lock.release()

    def test_detached_spot_snapshot(self):
        frozen=frozen_engine(self.engine,Lock())
        self.engine.states['NIFTY'].spot=1
        self.assertEqual(frozen.states['NIFTY'].spot,24052.)

    def test_independent_oi_greeks_absent(self):
        mapped=map_enrichments({'_last_poll':time.monotonic()})
        self.assertTrue(all(x['status']=='MISSING' for x in mapped.values()))

    def test_future_feature_receipt_rejected(self):
        row={'status':'AVAILABLE','observed_at':NOW.isoformat(),
             'available_at':ORIGIN.isoformat(),'contract_scope':'expiry/CE/24000'}
        with self.assertRaises(EvidenceError): map_enrichments({'atm_iv_pct':row})

    def test_capture_only_restart_and_no_network(self):
        packets=[]
        for _ in range(2):
            hook=ObservationHook()
            self.assertTrue(hook.observe(self.engine,Lock()))
            with patch('socket.socket',side_effect=AssertionError('broker forbidden')):
                result=hook.consume(CandleReader(self.path),self.manifest,
                    source_id='SYNTHETIC-TEST',config_id='SYNTHETIC-TEST',
                    clock=lambda:NOW,journal=None,save_packet=packets.append)
            self.assertEqual(result['state'],'CAPTURE_ONLY')
            encode(packets[-1])
        self.assertEqual(packets[0],packets[1])

    def test_queue_bounded_no_trading_mutation(self):
        before=copy.deepcopy(self.engine.states)
        hook=ObservationHook()
        self.assertTrue(hook.observe(self.engine,Lock()))
        for _ in range(1000): self.assertFalse(hook.observe(self.engine,None))
        self.assertEqual(hook.queue.qsize(),1)
        self.assertEqual(hook.dropped,1000)
        self.assertEqual(before,self.engine.states)

    def test_actual_host_missing_radar_blocks(self):
        self.engine.constituent_radar=None
        hook=ObservationHook(); hook.observe(self.engine,Lock())
        with self.assertRaises(InsufficientEvidence):
            hook.consume(CandleReader(self.path),self.manifest,source_id='HOST',
                config_id='HASH',clock=lambda:NOW,journal=None,save_packet=lambda p:None)

    def test_detached_radar_and_bounded_contention(self):
        frozen=frozen_engine(self.engine,Lock())
        self.engine.constituent_radar.states['0']['price']=1
        self.assertEqual(frozen.constituent_radar.states['0']['price'],101.)
        self.engine.constituent_radar.lock.acquire()
        start=time.monotonic()
        with self.assertRaises(EvidenceError):frozen_engine(self.engine,Lock())
        self.assertLess(time.monotonic()-start,.1)
        self.engine.constituent_radar.lock.release()

    def test_enrichment_nested_or_oversized_rejected(self):
        for row in ({'value':[]},{'source':'x'*513}):
            with self.assertRaises(EvidenceError):map_enrichments({'atm_iv_pct':row})
