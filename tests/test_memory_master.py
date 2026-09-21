"""Bounded download, single parse and atomic cache preservation fixtures."""
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import test_execution_lifecycle as f

spec=importlib.util.spec_from_file_location(f.PKG+'.memory_master',f.ROOT/'kadima/scrip_master.py')
master=importlib.util.module_from_spec(spec); spec.loader.exec_module(master)


class MasterMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.cache=self.root/'scrip_master.json'
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(master,'DATA_DIR',str(self.root)))
        self.stack.enter_context(patch.object(master,'CACHE',str(self.cache)))
        self.stack.enter_context(patch.dict(master.os.environ,{'KADIMA_CELESTIAL_ENABLED':'1'}))
        self.rows=[dict(instrumenttype='OPTSTK',name='TEST',exch_seg='NFO',token='1')]

    def test_download_uses_bounded_reads_and_parses_once(self):
        response=io.BytesIO(json.dumps(self.rows).encode())
        with patch.object(master.urllib.request,'urlopen',return_value=response), \
             patch.object(response,'read',wraps=response.read) as read, \
             patch.object(master.json,'load',wraps=json.load) as parse:
            result=master.ScripMaster.load()
        self.assertEqual(result.rows,self.rows)
        self.assertEqual(parse.call_count,1)
        self.assertTrue(all(c.args==(1024*1024,) for c in read.call_args_list))
        self.assertEqual(json.loads(self.cache.read_text()),self.rows)
        self.assertEqual(list(self.root.iterdir()),[self.cache])

    def test_fresh_cache_never_downloads(self):
        self.cache.write_text(json.dumps(self.rows))
        with patch.object(master.urllib.request,'urlopen') as fetch:
            self.assertEqual(master.ScripMaster.load().rows,self.rows)
        fetch.assert_not_called()

    def test_invalid_download_preserves_old_cache_and_fails_closed(self):
        self.cache.write_text(json.dumps(self.rows))
        old=self.cache.read_bytes()
        for raw in (b'[]',b'{}',b'[1]',b'broken'):
            with patch.object(master.urllib.request,'urlopen',return_value=io.BytesIO(raw)):
                with self.assertRaises(RuntimeError):master.ScripMaster.load(max_age_hours=0)
            self.assertEqual(self.cache.read_bytes(),old)
            self.assertEqual(list(self.root.iterdir()),[self.cache])

    def test_interrupted_download_preserves_old_cache(self):
        self.cache.write_text(json.dumps(self.rows)); old=self.cache.read_bytes()
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock()
        response.read.side_effect=[b'[',OSError('disconnect')]
        with patch.object(master.urllib.request,'urlopen',return_value=response):
            with self.assertRaises(RuntimeError):master.ScripMaster.load(max_age_hours=0)
        self.assertEqual(self.cache.read_bytes(),old)
        self.assertEqual(list(self.root.iterdir()),[self.cache])

    def test_atomic_replace_failure_preserves_old_cache(self):
        self.cache.write_text(json.dumps(self.rows)); old=self.cache.read_bytes()
        with patch.object(master.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(self.rows).encode())), \
             patch.object(master.os,'replace',side_effect=OSError('disk')):
            with self.assertRaises(RuntimeError):master.ScripMaster.load(max_age_hours=0)
        self.assertEqual(self.cache.read_bytes(),old)
        self.assertEqual(list(self.root.iterdir()),[self.cache])


if __name__=='__main__':unittest.main()
