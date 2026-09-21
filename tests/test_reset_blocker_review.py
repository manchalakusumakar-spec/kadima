"""Offline tests: passive reset evidence, privacy and unknown-not-flat semantics."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, MagicMock

BASE = Path(__file__).resolve().parents[1]
MODULE = BASE/'multi_position_update'/'review_reset_blocker.py'
if not MODULE.exists():
    MODULE = Path(__file__).with_name('review_reset_blocker.py')
spec = importlib.util.spec_from_file_location('reset_blocker_review_tested', MODULE)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
NOW = datetime(2026, 9, 20, 1, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def fixture():
    return (dict(ts=NOW.isoformat(), config=dict(auto_trade_enabled=False, kill_latched=True),
                 positions=[], system={}),
            dict(ts=NOW.isoformat(), controls=dict(live_enabled=False)))


class SummaryTests(unittest.TestCase):
    def test_missing_health_is_not_a_verified_flat_account(self):
        gd, sd = fixture()
        r = review.summarize(gd, sd, NOW)
        self.assertEqual(r['reconciliation']['state'], 'NOT_PUBLISHED')
        self.assertEqual(r['engine_positions']['count'], 0)
        self.assertEqual(r['kill_latched'], 'YES')
        self.assertNotIn('broker_flat', r)

    def test_missing_positions_are_unknown_not_zero(self):
        gd, sd = fixture(); gd.pop('positions')
        self.assertIsNone(review.summarize(gd, sd, NOW)['engine_positions']['count'])

    def test_old_check_is_never_restamped(self):
        gd, sd = fixture()
        gd['system']['reconciliation'] = dict(ok=False, ts=(NOW-timedelta(minutes=10)).isoformat(),
                                             reason='checking_broker_books')
        r = review.summarize(gd, sd, NOW)['reconciliation']
        self.assertFalse(r['fresh_within_120s']); self.assertEqual(r['age_seconds'], 600)
        self.assertEqual(r['reason'], 'checking_broker_books')

    def test_source_timestamps_must_be_fresh_aware_and_not_future(self):
        for at in [NOW-timedelta(seconds=31), NOW+timedelta(seconds=1), NOW.replace(tzinfo=None)]:
            gd, sd = fixture(); gd['ts'] = at.isoformat()
            with self.assertRaises(ValueError): review.summarize(gd, sd, NOW)

    def test_observation_reasons_and_open_position_states_reported(self):
        gd, sd = fixture()
        gd['system']['reconciliation'] = dict(ok=False, ts=NOW.isoformat(), positions=[
            dict(reason='external_pending_orders', count=2),
            dict(reason='managed_position_missing_at_broker')])
        gd['positions'] = [dict(status='entry_pending'), dict(status='open')]
        r = review.summarize(gd, sd, NOW)
        self.assertEqual(r['engine_positions']['count'], 2)
        self.assertIn('external_pending_orders', r['reconciliation']['observations']['by_reason'])

    def test_arbitrary_provider_text_auth_and_identifiers_are_not_emitted(self):
        gd, sd = fixture()
        gd['system']['reconciliation'] = dict(ok=False, ts=NOW.isoformat(), reason='SECRET_AUTH',
            positions=[dict(reason='SECRET_BODY', order_id='SECRET_ID', token='SECRET_TOKEN')])
        gd['positions'] = [dict(status='open', symbol='PRIVATE', token='SECRET_TOKEN')]
        text = json.dumps(review.summarize(gd, sd, NOW))
        self.assertNotIn('SECRET', text); self.assertNotIn('PRIVATE', text)

    def test_boolean_strings_are_unknown(self):
        gd, sd = fixture(); gd['config']['kill_latched'] = 'false'
        self.assertEqual(review.summarize(gd, sd, NOW)['kill_latched'], 'UNKNOWN')


class FilesTests(unittest.TestCase):
    def test_missing_database_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            self.assertIsNone(review.pending_summary(p)['count'])
            self.assertFalse((p/'kadima.db').exists())

    def test_unknown_ledger_is_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); c = sqlite3.connect(p/'kadima.db'); c.close()
            self.assertIsNone(review.pending_summary(p)['count'])

    def test_pending_terminal_and_null_states_and_read_only_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); file = p/'kadima.db'
            c = sqlite3.connect(file)
            c.execute('CREATE TABLE execution_orders(state TEXT,side TEXT)')
            c.executemany('INSERT INTO execution_orders VALUES (?,?)',
                [('terminal', 'BUY'), ('pending', 'SELL'), (None, 'BUY'), ('unknown', 'BUY')])
            c.commit(); c.close(); before = file.read_bytes()
            r = review.pending_summary(p)
            self.assertEqual(r['count'], 3); self.assertEqual(before, file.read_bytes())
            self.assertIn('UNKNOWN_OR_OMITTED', [r['state'] for r in r['groups']])

    def test_confirmed_empty_ledger_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); c = sqlite3.connect(p/'kadima.db')
            c.execute('CREATE TABLE execution_orders(state TEXT,side TEXT)')
            c.commit(); c.close()
            self.assertEqual(review.pending_summary(p), {'count': 0, 'groups': []})

    def test_symlinks_and_oversized_auth_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); target = p/'secret'; target.write_bytes(b'abc')
            link = p/'alias'; link.symlink_to(target)
            with self.assertRaises(ValueError): review.read_regular(link)
            with self.assertRaises(ValueError): review.read_regular(target, 2)

    def test_recent_events_only_and_no_provider_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'audit').mkdir()
            events = [dict(ts=NOW.isoformat(), event='login_failed', response='SECRET'),
                dict(ts=NOW.isoformat(), event='order_book_unavailable', error='SECRET'),
                dict(ts=(NOW-timedelta(hours=1)).isoformat(), event='login_ok'),
                dict(ts=NOW.isoformat(), event='arbitrary_secret_event')]
            (p/'audit'/'2026-09-20.jsonl').write_text('\n'.join(json.dumps(x) for x in events))
            r = review.recent_events(p, NOW)
            self.assertEqual([x['event'] for x in r['last_events']], ['login_failed', 'order_book_unavailable'])
            self.assertNotIn('SECRET', json.dumps(r))


class TransportTests(unittest.TestCase):
    def test_reset_resume_and_arbitrary_urls_not_allowed(self):
        c = object.__new__(review.Client); c.opener = Mock(); c.token = 'PRIVATE'
        for action in ['reset', 'reset-kill', 'resume', 'https://example.test']:
            with self.assertRaises(ValueError): c.read(action)
        c.opener.open.assert_not_called()

    def test_reads_use_only_fixed_loopback_get(self):
        c = object.__new__(review.Client); c.opener = MagicMock(); c.token = 'PRIVATE'
        c.opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        c.read('top20')
        req = c.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'http://127.0.0.1:8081/api/kadima/top20')
        self.assertEqual(req.get_method(), 'GET'); self.assertIsNone(req.data)
        self.assertIsNone(review.NoRedirect().redirect_request())


if __name__ == '__main__': unittest.main()
