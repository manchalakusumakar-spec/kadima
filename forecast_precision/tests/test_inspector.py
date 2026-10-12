import importlib.util
from pathlib import Path
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('inspector',Path(__file__).parents[1]/'inspect_install.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class Inspector(unittest.TestCase):
    def test_reads_but_does_not_execute_source(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'kadima'; path.mkdir()
            f=path/'hourly_forecast.py'
            f.write_text("raise RuntimeError('must not execute')\nHEAVY_WEIGHTS=(('ONE',1.0),)\nSECRET='DO_NOT_PRINT'\nM.make_forecast(x,breadth=b)\n")
            before=f.read_bytes(); report=m.inspect(d)
            self.assertEqual(report['files']['kadima/hourly_forecast.py']['make_forecast_call_keywords'],[['breadth']])
            self.assertNotIn('DO_NOT_PRINT',str(report)); self.assertEqual(f.read_bytes(),before)
    def test_no_outside_symlink(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as outside:
            path=Path(root)/'kadima'; path.mkdir()
            secret=Path(outside)/'secret.py'; secret.write_text('SECRET=1')
            (path/'feed.py').symlink_to(secret)
            self.assertEqual(m.inspect(root)['files']['kadima/feed.py']['status'],'OUTSIDE_ROOT_NOT_READ')

if __name__=='__main__': unittest.main()
