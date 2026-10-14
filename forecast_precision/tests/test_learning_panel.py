"""Exercise browser-independent dashboard DOM module in GitHub's Node runtime."""
from pathlib import Path
import shutil
import subprocess
import unittest


class LearningPanelTests(unittest.TestCase):
    def test_node_renderer_and_xss_regressions(self):
        node=shutil.which('node')
        if node is None:
            self.skipTest('Node binary unavailable; renderer must still pass JS CI before release')
        test=Path(__file__).with_suffix('.cjs')
        completed=subprocess.run([node,'--test',str(test)],check=False,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=15)
        self.assertEqual(completed.returncode,0,completed.stdout[-4500:])


if __name__=='__main__': unittest.main()
