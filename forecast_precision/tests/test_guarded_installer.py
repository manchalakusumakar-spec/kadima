import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import datetime,timezone,timedelta
import tempfile
import unittest
from unittest.mock import patch
from guarded_installer import preflight,stage

class Installer(unittest.TestCase):
    def setUp(self):
        self.identity={'files':{'engine':'hash'}}
        self.plan=patch('guarded_installer.plan',return_value={'release_sha':'a'*40});self.plan.start();self.addCleanup(self.plan.stop)
        self.inspect=patch('guarded_installer.inspect',return_value=self.identity);self.inspect.start();self.addCleanup(self.inspect.stop)
    def call(self,**kw):
        return stage('/unused','a'*40,'/live',self.identity,'/never-write/'+('a'*40),**kw)
    def test_approval_missing_no_write(self):
        with patch('pathlib.Path.mkdir',side_effect=AssertionError('write')):
            with self.assertRaises(ValueError):self.call()
    def test_missing_broker_provider_blocks(self):
        with self.assertRaises(ValueError):self.call(approved=True)
    def test_stale_reconciliation_blocks(self):
        with self.assertRaises(ValueError):self.call(approved=True,reconciler=lambda:{'received_at':(datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat()},deployment_check=lambda:True)
    def test_positions_not_closed_to_pass(self):
        with self.assertRaises(ValueError):self.call(approved=True,reconciler=lambda:{'received_at':datetime.now(timezone.utc).isoformat(),'complete':True,'positions':[1],'pending_orders':[]},deployment_check=lambda:True)
    def test_pending_orders_block(self):
        with self.assertRaises(ValueError):self.call(approved=True,reconciler=lambda:{'received_at':datetime.now(timezone.utc).isoformat(),'complete':True,'positions':[],'pending_orders':[1]},deployment_check=lambda:True)
    def test_competing_deployment_blocks(self):
        with self.assertRaises(ValueError):self.call(approved=True,reconciler=lambda:{'received_at':datetime.now(timezone.utc).isoformat(),'complete':True,'positions':[],'pending_orders':[]},deployment_check=lambda:False)
    def test_source_drift_blocks(self):
        with self.assertRaises(ValueError):preflight('/unused','a'*40,'/live',{'different':1})
