"""PR20 export identity regression; no external connections or schema migrations."""
import csv
from datetime import datetime, timedelta
import io
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp(prefix='kadima-export-'))
from kadima import storage, trade_audit as audit
from kadima.config import IST

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=IST)


def proposal(**kw):
    row = dict(ts=NOW.isoformat(), strategy='fast_momentum', episode='breakout:1',
               exchange='NFO', token='100', symbol='TESTCE', underlying='TEST',
               direction='CE', entry=10., stop=9., t1=12., t2=14., holding_minutes=15)
    row.update(kw)
    return row


class ReplayExport(unittest.TestCase):
    def setUp(self):
        self.block = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.block.start()
        c = audit.connection()
        with c:
            c.execute('DELETE FROM trade_audit_plans')
            c.execute('DELETE FROM trade_audit_orders')

    def tearDown(self):
        self.block.stop()

    def exported(self):
        return list(csv.DictReader(io.StringIO(audit.export_report(day=NOW.date().isoformat())['content'])))

    def test_unexecuted_suggestion_exports_existing_id_with_blank_signal(self):
        aid = audit.record(proposal(), producer='stock_fast')
        row = self.exported()[0]
        self.assertEqual(row['Suggestion ID'], aid)
        self.assertEqual(row['Signal ID'], '')
        self.assertEqual((row['Producer'], row['Episode'], row['Exchange'], row['Token']),
                         ('stock_fast', 'breakout:1', 'NFO', '100'))

    def test_same_episode_freezes_plan_and_links_signal_after_bind(self):
        row = proposal()
        aid = audit.record(row, producer='stock_fast')
        later = proposal(ts=(NOW+timedelta(seconds=30)).isoformat(), entry=20., holding_minutes=5)
        self.assertEqual(audit.record(later, producer='stock_fast', signal_id=7), aid)
        exported = self.exported()[0]
        self.assertEqual(exported['Signal ID'], '7')
        self.assertEqual(exported['Proposed entry'], '10.0')
        self.assertEqual(exported['Proposed maximum hold minutes'], '15.0')
        storage._local.conn.close()
        storage._local.conn = None
        self.assertEqual(self.exported()[0]['Suggestion ID'], aid)

    def test_repeated_contract_distinct_episode_and_exchange_keep_separate_ids(self):
        for row in (proposal(), proposal(episode='breakout:2'), proposal(exchange='BFO')):
            audit.record(row)
        self.assertEqual(len({r['Suggestion ID'] for r in self.exported()}), 3)

    def test_missing_index_horizon_stays_unknown_never_defaults_to_fifteen(self):
        audit.record(proposal(holding_minutes=None, scenarios=[dict(minutes=15)]), producer='index_fast')
        row = self.exported()[0]
        self.assertEqual(row['Proposed maximum hold minutes'], '')
        self.assertEqual(row['Proposed holding basis'], 'UNAVAILABLE')

    def test_legacy_nulls_remain_null_and_schema_unchanged(self):
        audit.record(proposal())
        columns = [r['name'] for r in audit.connection().execute('PRAGMA table_info(trade_audit_plans)')]
        self.exported()
        self.assertEqual(columns, [r['name'] for r in audit.connection().execute('PRAGMA table_info(trade_audit_plans)')])
        self.assertNotIn('suggestion_id', columns)

    def test_new_export_text_is_formula_safe(self):
        audit.record(proposal(episode='=1+1'), producer='@bad')
        row = self.exported()[0]
        self.assertEqual(row['Producer'], "'@bad")
        self.assertEqual(row['Episode'], "'=1+1")


if __name__ == '__main__':
    unittest.main()
