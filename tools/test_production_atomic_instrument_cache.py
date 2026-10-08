import importlib.util,json,pathlib,tempfile,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
def load():
    spec=importlib.util.spec_from_file_location('fixture_instrument',ROOT/'kadima/instrument_master.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class Tests(unittest.TestCase):
    def test_failed_publication_preserves_last_complete_cache(self):
        m=load()
        with tempfile.TemporaryDirectory() as td:
            m.CACHE_DIR=td;m.COMPACT_CACHE=str(pathlib.Path(td)/'lotsize_map.json');old='{"schema":2,"lot":{"old":1}}';pathlib.Path(m.COMPACT_CACHE).write_text(old)
            source=pathlib.Path(td)/'input.json';source.write_text('[]');m._download_to_temp=lambda:str(source);m._extract_compact=lambda p:({'new':2},{},{},{})
            def failed_dump(value, handle):handle.write('{');raise OSError('fixture disk failure')
            with patch.object(m.json,'dump',side_effect=failed_dump):self.assertFalse(m.refresh_cache())
            self.assertEqual(pathlib.Path(m.COMPACT_CACHE).read_text(),old)
            self.assertFalse(list(pathlib.Path(td).glob('.lotsize-*')))
    def test_downloads_use_distinct_temporary_files(self):
        m=load()
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return b''
        with tempfile.TemporaryDirectory() as td,patch.object(m.urllib.request,'urlopen',return_value=Response()):
            m.CACHE_DIR=td;a=m._download_to_temp();b=m._download_to_temp();self.assertNotEqual(a,b)
    def test_success_preserves_schema_token_spot_and_nfo_precedence(self):
        m=load();lot={};sym={};universe={};spot={}
        for exchange in ['NFO','BFO']:
            m._absorb_row({'exch_seg':exchange,'instrumenttype':'OPTSTK','name':'X','token':exchange,'lotsize':'5','symbol':exchange},lot,sym,universe,spot)
        self.assertEqual(universe['X']['opt_exchange'],'NFO');self.assertEqual(lot,{'NFO':5,'BFO':5});self.assertEqual(m.CACHE_SCHEMA,2)
if __name__=='__main__':unittest.main()
