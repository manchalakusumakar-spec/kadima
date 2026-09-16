import io
import json
import os
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from test_shield_confirmation import load_engine
from kadima import scrip_master as master


class EntryQueue(unittest.TestCase):
    def setUp(self):
        self.mod=load_engine();self.engine=self.mod.Engine.__new__(self.mod.Engine)
        self.engine._entry_jobs={};self.engine._entry_jobs_lock=threading.Lock()
        self.engine._entry_jobs_ready=threading.Event();self.engine._stop=threading.Event()
        self.engine._dte=lambda st:1
        self.chain=SimpleNamespace(quotes={'T':SimpleNamespace(ltp=100)},_last_poll=time.monotonic(),_expiry='24SEP2026',
            velocity=SimpleNamespace(samples=deque([1],maxlen=200)))
        self.state=SimpleNamespace(name='NIFTY',chain=self.chain,spot=100,snap=None,gamma_result=SimpleNamespace(direction='CE',tier='CONFIRMED',score=80))

    def test_latest_per_index_is_bounded_and_quotes_are_frozen(self):
        self.engine._queue_entry_work(self.state,1)
        first=self.engine._entry_jobs['NIFTY']
        self.chain.quotes['T'].ltp=110;self.chain.velocity.samples.append(2)
        self.assertEqual(first['chain'].quotes['T'].ltp,100)
        self.assertEqual(list(first['chain'].velocity.samples),[1])
        self.engine._queue_entry_work(self.state,2)
        self.assertEqual(len(self.engine._entry_jobs),1)
        self.assertEqual(self.engine._entry_jobs['NIFTY']['sid'],2)
        self.chain._last_poll+=1
        self.assertFalse(first['chain']._entry_guard())

    def test_detector_queues_without_calling_executor_and_worker_consumes(self):
        with patch.object(self.mod.auto_executor.EXECUTOR,'evaluate_and_enter') as entry:
            self.engine._queue_entry_work(self.state,1);entry.assert_not_called()
            entry.side_effect=lambda **kw:self.engine._stop.set()
            self.engine._entry_work_loop();entry.assert_called_once()
            self.assertEqual(entry.call_args.kwargs['source_signal_id'],1)

    def test_shutdown_does_not_submit_a_waiting_job(self):
        self.engine._queue_entry_work(self.state,1);self.engine._stop.set()
        with patch.object(self.mod.auto_executor.EXECUTOR,'evaluate_and_enter') as entry:
            self.engine._entry_work_loop();entry.assert_not_called()


class MasterMemory(unittest.TestCase):
    def test_download_is_bounded_and_parsed_only_once(self):
        raw=json.dumps([{'token':'T','instrumenttype':'OPTIDX','name':'NIFTY','exch_seg':'NFO'}]).encode()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock()
        response.read=Mock(side_effect=[raw,b''])
        with tempfile.TemporaryDirectory() as tmp,patch.object(master,'DATA_DIR',tmp),patch.object(master,'CACHE',tmp+'/master.json'), \
             patch.object(master.urllib.request,'urlopen',return_value=response),patch.object(master.audit,'log'), \
             patch.object(master.json,'load',wraps=json.load) as parse:
            result=master.ScripMaster.load()
            self.assertEqual(len(result.rows),1);self.assertEqual(parse.call_count,1)
            self.assertTrue(all(call.args==(1024*1024,) for call in response.read.call_args_list))
            self.assertEqual(json.loads(Path(tmp+'/master.json').read_text())[0]['token'],'T')
            self.assertEqual(len(list(Path(tmp).iterdir())),1)

    def test_failed_refresh_does_not_overwrite_good_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'master.json';path.write_text('[{"token":"OLD"}]');os.utime(path,(1,1))
            with patch.object(master,'DATA_DIR',tmp),patch.object(master,'CACHE',str(path)), \
                 patch.object(master.urllib.request,'urlopen',side_effect=OSError('private')),patch.object(master.audit,'log'), \
                 patch.dict(os.environ,{'KADIMA_CELESTIAL_ENABLED':'1'}):
                with self.assertRaises(RuntimeError):master.ScripMaster.load()
                self.assertEqual(path.read_text(),'[{"token":"OLD"}]')
                self.assertEqual(len(list(Path(tmp).iterdir())),1)


if __name__=='__main__':unittest.main()
