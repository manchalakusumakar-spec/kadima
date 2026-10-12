from pathlib import Path
import sqlite3
import tempfile
import unittest
from precision_lab.observations import ObservationJournal,horizon_report
from precision_lab.core import EvidenceError

class Observations(unittest.TestCase):
    def test_restart_immutable_packet(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'capture.precision-lab.sqlite3'
            j=ObservationJournal(path); j.save_packet({'id':'a','value':1}); j.close()
            j=ObservationJournal(path)
            self.assertFalse(j.save_packet({'id':'a','value':1}))
            with self.assertRaises(EvidenceError):j.save_packet({'id':'a','value':2})
            with self.assertRaises(sqlite3.IntegrityError):j.db.execute('DELETE FROM snapshots')
            j.close()
    def test_strict_target_missing_and_horizons(self):
        scores=[{'kind':'ORIGINAL','forecast_id':'one','rows':[
            {'horizon_minutes':5,'absolute_error':5},
            {'horizon_minutes':10,'absolute_error':None}]},
            {'kind':'ORIGINAL','forecast_id':'two','rows':[
            {'horizon_minutes':5,'absolute_error':4.999}]}]
        r=horizon_report(scores)
        self.assertEqual(r['horizons'][5]['below_5_fraction'],.5)
        self.assertEqual(r['horizons'][5]['worst_error_points'],5)
        self.assertEqual(r['horizons'][10]['coverage'],0)
        self.assertIsNone(r['horizons'][10]['below_5_fraction'])
        self.assertIsNone(r['net_error'])
    def test_revision_rejected(self):
        with self.assertRaises(EvidenceError): horizon_report([{'kind':'REVISION'}])
