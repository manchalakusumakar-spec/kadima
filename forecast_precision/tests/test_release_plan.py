import unittest
from release_plan import BASE, check_scope, check_sha, main

class ReleasePlan(unittest.TestCase):
    def test_exact_sha(self): self.assertEqual(check_sha(BASE),BASE)
    def test_no_branch_names(self):
        for s in ('main','HEAD','1'*39,'a'*40+';true'):
            with self.assertRaises(ValueError): check_sha(s)
    def test_additions_only(self): check_scope([('A','forecast_precision/new.py')])
    def test_shared_change_refused(self):
        with self.assertRaises(ValueError): check_scope([('M','kadima/engine.py')])
    def test_deletion_refused(self):
        with self.assertRaises(ValueError): check_scope([('D','forecast_precision/a.py')])
    def test_other_workflow_refused(self):
        with self.assertRaises(ValueError): check_scope([('A','.github/workflows/autotrade.yml')])
    def test_apply_refused(self):
        with self.assertRaises(SystemExit) as e: main(['--release-sha',BASE,'--apply'])
        self.assertEqual(e.exception.code,2)

if __name__=='__main__': unittest.main()
