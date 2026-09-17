import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from test_fast_momentum import candles

spec=importlib.util.spec_from_file_location('session_review',Path(__file__).resolve().parents[1]/'tools/review_execution_session.py')
review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)


class SessionReview(unittest.TestCase):
    def test_audit_whitelist_does_not_expose_tokens_or_raw_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'audit.jsonl'
            p.write_text(json.dumps(dict(event='ANGELONE_RESPONSE',signal_id=42,ok=False,
                                        api_key='secret',response={'token':'secret'}))+'\n')
            result=review.audit_summary(p)
            self.assertEqual(result['event_counts']['ANGELONE_RESPONSE'],1)
            self.assertNotIn('secret',json.dumps(result))

    def test_price_replay_only_consumes_completed_history(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE candles (token,interval,start_ts,open,high,low,close,closed)')
        one,five=candles()
        for interval,rows in [(1,one),(5,five)]:
            for r in rows:c.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)',
                ('U',interval,r['start_ts'],r['open'],r['high'],r['low'],r['close'],1))
        a=review.replay(c,'U','2026-09-17')
        self.assertEqual(len(a['price_setups']),1)
        self.assertEqual(a['price_setups'][0]['direction'],'CE')
        self.assertIn('not executable',a['note'])
        c.execute("INSERT INTO candles VALUES ('U',5,'2026-09-17T10:00:00+05:30',2000,2001,1999,2000,1)")
        self.assertEqual(review.replay(c,'U','2026-09-17'),a)
        c.close()
