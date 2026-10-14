import importlib.util
from pathlib import Path
import tempfile
import unittest
import subprocess

spec=importlib.util.spec_from_file_location('check',Path(__file__).parents[1]/'tools/n50_deploy_check.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def test_overlay_is_not_treated_as_deployable(self):
        self.assertEqual(m.classify(b'newer local',b'base',b'candidate'),'installed_overlay_differs')
        self.assertEqual(m.classify(b'base',b'base',b'candidate'),'base_matches_disk')
        self.assertEqual(m.classify(b'candidate',None,b'candidate'),'candidate_matches_disk')
        self.assertEqual(m.classify(None,None,b'candidate'),'missing')

    def test_failed_check_does_not_leak_stderr_or_change_files(self):
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'keep';marker.write_text('unchanged')
            result=subprocess.run(['python3',str(Path(m.__file__).resolve()),'--repo',d],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertNotIn('fatal:',result.stdout+result.stderr)
            self.assertIn('deployment_performed',result.stdout)
            self.assertEqual(marker.read_text(),'unchanged')

if __name__=='__main__':unittest.main()
