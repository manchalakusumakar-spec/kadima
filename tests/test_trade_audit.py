"""Audit persistence and ledger accounting; all broker transports are replaced."""
import copy
import csv
import importlib
import io
import json
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import test_execution_lifecycle as f

a = importlib.import_module(f.PKG + '.trade_audit')
c = importlib.import_module(f.PKG + '.celestial')


def gamma_candidate(token, **changes):
    row = dict(id='NFO:'+str(token), exchange='NFO', token=str(token),
               symbol='TEST'+str(token)+'CE', underlying='STOCK'+str(token),
               direction='CE', best_strike=100, option_ltp=110,
               premium_move_pct=10, gamma_score=80, priority=2,
               last_updated=f.NOW.isoformat(), spot_at=f.NOW.isoformat(),
               volume_spike=True, oi_support=True, spread_status='CLEAN',
               tradeable=True, direction_clear=True)
    row.update(changes)
    return row


def proposal(**changes):
    row = dict(ts=f.NOW.isoformat(), quote_at=f.NOW.isoformat(), exchange='NFO',
               token='T', symbol='TESTCE', underlying='INDEX', direction='CE',
               strategy='fast_momentum', episode='episode-1', entry=100, ltp=100,
               stop=90, t1=110, t2=120, planned_exit=125, holding_minutes=15,
               strike=25000, score=80, stage='READY', blocked_reasons=[])
    row.update(changes)
    return row


class TradeAudit(unittest.TestCase):
    setUp = f.Lifecycle.setUp
    tearDown = f.Lifecycle.tearDown

    def rows(self, **kw):
        return a.report(day='2026-09-14', now=f.NOW, **kw)['rows']

    def signal(self, sid=1, payload=None, kind='fast_momentum'):
        db = f.storage.conn()
        with db:
            db.execute('INSERT INTO signals (id,ts,index_name,tier,score,direction,payload,kind) VALUES (?,?,?,?,?,?,?,?)',
                       (sid, f.NOW.isoformat(), 'INDEX', 'READY', 80, 'CE',
                        json.dumps(payload if payload is not None else proposal()), kind))

    def buy(self, dry=False):
        self.signal()
        a.record(proposal(), signal_id=1)
        self.pos.kind = 'fast_momentum'
        self.pos.entry_price = 0
        self.pos.remaining_qty = 0
        if dry:
            self.place.return_value = f.orders.OrderResult(True, 'DRY', 'dry_run', dry_run=True)
        f.ledger.submit(self.pos, 'BUY', 10, 102)

    def confirm(self, side, quantity, avg, seconds, state='complete'):
        row = f.ledger.pending(self.pos.signal_id)[0]
        self.assertEqual(row['side'], side)
        with patch.object(f.ledger, 'now_ist', return_value=f.NOW + timedelta(seconds=seconds)):
            f.ledger.apply_fill(self.pos, row, f.orders.OrderStatus('O1', state, quantity, avg, {}))

    def test_frozen_plan_survives_updates_disappearance_and_restart(self):
        aid = a.record(proposal())
        a.record(proposal(ts=(f.NOW+timedelta(minutes=1)).isoformat(), entry=999, stop=800,
                          holding_minutes=5, blocked_reasons=['paused']))
        f.storage._local.conn.close()
        f.storage._local = threading.local()
        row = self.rows()[0]
        self.assertEqual(row['audit_id'], aid)
        self.assertEqual(row['plan']['entry'], 100)
        self.assertEqual(row['plan']['holding_minutes'], 15)
        self.assertEqual(row['blocked_reasons'], ['paused'])
        self.assertEqual(row['suggested_at'], f.NOW.astimezone(a.IST).isoformat())
        self.assertEqual(len(self.rows()), 1)

    def test_invalid_identity_or_timestamp_cannot_create_plan(self):
        for override in ({'token': ''}, {'exchange': ''}, {'symbol': ''}, {'ts': '2026-09-14T10:00:00'}):
            self.assertIsNone(a.record(proposal(**override)))
        self.assertEqual(self.rows(), [])

    def test_nonfinite_or_inverted_barriers_do_not_become_targets(self):
        a.record(proposal(entry=True, stop=float('nan'), t1=99, t2=float('inf')), 'GAMMA')
        p = self.rows()[0]['plan']
        for key in ('entry', 'stop', 't1', 't2'):
            self.assertIsNone(p[key])
        self.assertEqual(p['entry_basis'], 'LTP_REFERENCE')

    def test_scalp_warmup_does_not_freeze_an_incomplete_plan(self):
        self.assertIsNone(a.record(proposal(entry=None, stop=None, t1=None, t2=None)))
        a.record(proposal())
        self.assertEqual(self.rows()[0]['plan']['entry'], 100)

    def test_recent_quote_before_suggestion_is_fresh_but_not_a_target_hit(self):
        a.record(proposal(quote_at=(f.NOW-timedelta(seconds=5)).isoformat(), ltp=125))
        row = self.rows()[0]
        self.assertEqual(row['freshness'], 'RECENT_POLL')
        self.assertEqual(row['ltp'], 125)
        self.assertIsNone(row['t1_at'])

    def test_new_contract_episode_and_ist_date_are_distinct(self):
        for overrides in ({}, {'token': 'T2'}, {'episode': 'episode-2'},
                          {'ts': '2026-09-14T19:00:00+00:00'}):
            a.record(proposal(**overrides))
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(a.report(day='2026-09-15')['total'], 1)

    def test_gamma_history_includes_all_threshold_leads_beyond_top20(self):
        candidates = [gamma_candidate(i) for i in range(1, 32)]
        candidates += [gamma_candidate(50, underlying='NIFTY', priority=1),
                       gamma_candidate(51, underlying='NIFTY', priority=1)]
        c.publish('all-gamma', candidates)
        self.assertEqual(len(c.top20()), 20)
        self.assertNotIn('51', {r['token'] for r in c.top20()})
        self.assertEqual({r['token'] for r in self.rows()}, {r['token'] for r in candidates})
        c.publish('all-gamma', [])
        self.assertEqual(c.top20(), [])
        self.assertEqual(len(self.rows()), 33)
        self.place.assert_not_called()

    def test_gamma_threshold_uses_final_score_and_keeps_freshness_gates(self):
        candidates = [gamma_candidate(1, gamma_score=55),
                      gamma_candidate(2, gamma_score=65, event_risk_penalty=20),
                      gamma_candidate(3, gamma_score=50),
                      gamma_candidate(4, last_updated=(f.NOW-timedelta(seconds=31)).isoformat()),
                      gamma_candidate(5, premium_move_pct=4.99)]
        news = dict(news_score=10, news_available=True, news_blocked=False, news_sources=[])
        with patch.object(c, 'news_impact', return_value=news):
            c.publish('threshold', candidates)
        self.assertEqual({r['token']:r['score'] for r in self.rows()}, {'1':65, '3':60})
        self.assertEqual(c.read_config()['score_threshold'], 65)
        self.assertFalse(c.read_config()['auto_trade_enabled'])

    def interleaved_quotes(self, first_seconds, first_price, second_seconds, second_price):
        """A second SQLite writer commits after the first writer has read a row."""
        a.record(proposal())
        db = a.connection()
        other = sqlite3.connect(f.storage.DB_PATH)
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        interleaved = []
        class Connection:
            def __enter__(self):
                db.__enter__()
                return self
            def __exit__(self, *args):
                return db.__exit__(*args)
            def execute(self, sql, parameters=()):
                if 'UPDATE trade_audit_plans SET ltp' in sql and not interleaved:
                    interleaved.append(True)
                    at = f.NOW+timedelta(seconds=second_seconds)
                    with patch.object(a, 'connection', return_value=other):
                        a.observe('NFO', [('T', second_price)], at, now=at)
                return db.execute(sql, parameters)
        at = f.NOW+timedelta(seconds=first_seconds)
        with patch.object(a, 'connection', return_value=Connection()):
            a.observe('NFO', [('T', first_price)], at, now=at)
        self.assertEqual(interleaved, [True])
        return self.rows()[0]

    def test_concurrent_writer_preserves_first_target_and_observed_peak(self):
        row = self.interleaved_quotes(20, 105, 10, 111)
        self.assertEqual(row['time_to_t1_seconds'], 10)
        self.assertEqual(row['high_ltp'], 111)
        self.assertEqual(row['ltp'], 105)

    def test_concurrent_older_quote_cannot_replace_newer_quote_or_result(self):
        row = self.interleaved_quotes(10, 89, 20, 121)
        self.assertEqual(row['quote_at'], (f.NOW+timedelta(seconds=20)).astimezone(a.IST).isoformat())
        self.assertEqual(row['ltp'], 121)
        self.assertEqual(row['time_to_t2_seconds'], 20)
        self.assertIsNone(row['sl_at'])

    def test_concurrent_stop_is_terminal_for_a_later_rebound(self):
        row = self.interleaved_quotes(20, 125, 10, 89)
        self.assertEqual(row['time_to_sl_seconds'], 10)
        self.assertIsNone(row['t1_at'])
        self.assertIsNone(row['t2_at'])

    def test_concurrent_t2_is_terminal_for_a_later_fall(self):
        row = self.interleaved_quotes(20, 89, 10, 125)
        self.assertEqual(row['time_to_t2_seconds'], 10)
        self.assertIsNone(row['sl_at'])

    def test_thresholds_are_sampled_with_first_pass_and_restart_persistence(self):
        a.record(proposal())
        for seconds, price in ((10, 111), (20, 122), (30, 80)):
            at = f.NOW+timedelta(seconds=seconds)
            a.observe('NFO', [('T', price)], at, now=at)
        row = self.rows()[0]
        self.assertEqual(row['observed_result'], 'T2_OBSERVED')
        self.assertEqual((row['time_to_t1_seconds'], row['time_to_t2_seconds']), (10, 20))
        self.assertIsNone(row['sl_at'])
        self.assertEqual(row['ltp'], 80)
        self.assertEqual(row['high_ltp'], 122)

    def test_stop_ends_observation_and_expired_horizon_does_not_record_hits(self):
        a.record(proposal())
        for seconds, price in ((1, 89), (2, 130)):
            at = f.NOW+timedelta(seconds=seconds)
            a.observe('NFO', [('T', price)], at, now=at)
        row = self.rows()[0]
        self.assertEqual(row['observed_result'], 'SL_OBSERVED')
        self.assertIsNone(row['t2_at'])
        a.record(proposal(token='T2', holding_minutes=1))
        at = f.NOW+timedelta(seconds=61)
        a.observe('NFO', [('T2', 150)], at, now=at)
        other = next(r for r in self.rows() if r['token']=='T2')
        self.assertIsNone(other['t1_at'])

    def test_stale_future_missing_and_out_of_order_quotes_are_ignored(self):
        a.record(proposal())
        for at in (None, 'not-time', f.NOW-timedelta(seconds=31), f.NOW+timedelta(seconds=1)):
            a.observe('NFO', [('T', 130)], at, now=f.NOW)
        a.observe('NFO', [('T', 130)], f.NOW, now=f.NOW)  # duplicate sample
        self.assertEqual(self.rows()[0]['ltp'], 100)
        self.assertIsNone(self.rows()[0]['t2_at'])
        row = a.report(day='2026-09-14', now=f.NOW+timedelta(seconds=31))['rows'][0]
        self.assertEqual(row['freshness'], 'STALE')

    def test_acknowledgement_has_no_actual_entry_exit_pnl_or_hold(self):
        self.buy()
        row = self.rows()[0]
        self.assertEqual(row['execution_status'], 'PENDING')
        for key in ('actual_entry', 'actual_exit', 'realized_pnl', 'actual_holding_seconds'):
            self.assertIsNone(row[key])
        self.assertEqual(row['entry_order_limit'], 102)

    def test_partial_and_duplicate_fills_weighted_actuals_and_hold(self):
        self.buy()
        self.confirm('BUY', 4, 101, 10, 'open')
        self.confirm('BUY', 4, 101, 10, 'open')
        self.confirm('BUY', 10, 102, 20)
        f.ledger.submit(self.pos, 'SELL', 10, 111)
        self.confirm('SELL', 4, 110, 40, 'open')
        self.assertIsNone(self.rows()[0]['actual_holding_seconds'])
        self.confirm('SELL', 10, 112, 70)
        row = self.rows()[0]
        self.assertEqual(row['execution_status'], 'CLOSED')
        self.assertEqual((row['actual_entry'], row['actual_exit']), (102, 112))
        self.assertEqual((row['filled_buy_quantity'], row['filled_sell_quantity']), (10, 10))
        self.assertEqual(row['realized_pnl'], 100)
        self.assertEqual(row['actual_holding_seconds'], 60)
        self.assertEqual(row['entry_difference'], 2)
        self.assertEqual(row['exit_difference'], -13)
        self.assertEqual(row['exit_order_limit'], 111)
        self.assertEqual(row['holding_time_basis'], 'ENGINE_FILL_CONFIRMATION')

    def test_paper_fills_are_not_actuals(self):
        self.buy(dry=True)
        row = self.rows()[0]
        self.assertEqual(row['execution_status'], 'PAPER')
        self.assertEqual(row['filled_buy_quantity'], 0)
        self.assertIsNone(row['actual_entry'])
        self.assertIsNone(row['realized_pnl'])

    def test_mixed_paper_and_live_exposure_is_unknown(self):
        self.buy()
        self.confirm('BUY', 10, 102, 10)
        self.place.return_value = f.orders.OrderResult(True, 'DRY', 'dry_run', dry_run=True)
        f.ledger.submit(self.pos, 'SELL', 10, 111)
        self.assertEqual(self.rows()[0]['execution_status'], 'UNKNOWN')
        self.assertIsNone(self.rows()[0]['actual_holding_seconds'])

    def test_same_contract_different_signal_cannot_steal_first_plan(self):
        a.record(proposal(), signal_id=1)
        self.signal(2)
        other = copy.copy(self.pos)
        other.signal_id = 2
        other.kind = 'fast_momentum'
        a.record_submission(other, 'BUY', 10, 100, 'next-intent')
        rows = self.rows()
        self.assertEqual({r['signal_id'] for r in rows}, {1, 2})
        self.assertEqual(len({r['audit_id'] for r in rows}), 2)
        self.assertEqual(next(r for r in rows if r['signal_id']==1)['origin'], 'SUGGESTION')

    def test_repeated_signal_capture_creates_distinct_original_plan(self):
        a.record(proposal(), signal_id=1)
        a.record(proposal(entry=102), signal_id=2)
        rows = {r['signal_id']:r for r in self.rows()}
        self.assertEqual(rows[1]['plan']['entry'], 100)
        self.assertEqual(rows[2]['plan']['entry'], 102)
        a.record(proposal(entry=104), signal_id=2)
        self.assertEqual(len(self.rows()), 2)

    def test_old_execution_does_not_borrow_a_later_same_day_suggestion(self):
        self.signal()
        a.record(proposal(ts=(f.NOW+timedelta(minutes=5)).isoformat(), entry=150))
        a.record_submission(self.pos, 'SELL', 10, 110, 'historical-exit')
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        original = next(r for r in rows if r['signal_id']==1)
        self.assertEqual(original['plan']['entry'], 100)
        self.assertEqual(original['origin'], 'PERSISTED_SIGNAL')

    def test_contract_conflict_is_rejected_not_silently_joined(self):
        a.record(proposal(), signal_id=1)
        self.pos.token = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'contract_mismatch'):
            a.record_submission(self.pos, 'BUY', 10, 100, 'invalid')
        self.assertEqual(self.rows()[0]['token'], 'T')

    def test_missing_original_payload_never_reconstructs_plan_from_fill(self):
        self.signal(payload={'entry': 700, 'stop': 600})
        self.pos.kind = 'fast_momentum'
        a.record_submission(self.pos, 'SELL', 10, 120, 'exit-only')
        row = self.rows()[0]
        self.assertEqual(row['origin'], 'EXECUTION_ONLY')
        self.assertIsNone(row['plan']['entry'])
        self.assertIsNone(row['actual_entry'])

    def test_backfill_is_idempotent_and_labels_missing_original_evidence(self):
        self.signal(payload={'entry': 700})
        self.assertEqual(a.backfill_positions()['imported'], 1)
        self.assertEqual(a.backfill_positions()['imported'], 0)
        row = self.rows()[0]
        self.assertEqual(row['origin'], 'HISTORICAL_ACTUALS_ONLY')
        self.assertEqual(row['execution_status'], 'UNKNOWN')
        self.assertIsNone(row['plan']['entry'])
        self.assertIsNone(row['actual_entry'])  # position estimate is not a ledger fill

    def test_strategy_uses_move_capture_producer_over_reused_executor_lane(self):
        self.signal(payload={'source_strategy':'move_capture'}, kind='fast_momentum')
        self.assertEqual(a.strategy_for_position(self.pos), 'MOVE_CAPTURE')
        self.assertEqual(a.strategy_code('mcx_momentum'), 'MCX')
        self.assertEqual(a.strategy_code('cas_premium_awakening'), 'CAS')

    def test_filters_and_stable_paging_match_total(self):
        a.record(proposal(symbol='NIFTYCE'))
        a.record(proposal(token='2', symbol='OTHERPE'), 'GAMMA')
        a.record(proposal(token='3', symbol='NIFTYPE'))
        self.assertEqual(len(self.rows(strategy='SCALP', symbol='nifty', status='SUGGESTED')), 2)
        first, second = self.rows(limit=1), self.rows(limit=1, offset=1)
        self.assertNotEqual(first[0]['audit_id'], second[0]['audit_id'])
        for args in ({'day':'2026-02-30'}, {'day':'2026-1-01'}, {'strategy':'bad'},
                     {'status':'bad'}, {'limit':201}, {'offset':-1}, {'symbol':'x'*81}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                a.report(**args)

    def test_csv_is_filtered_paged_and_formula_safe(self):
        for n in range(202):
            a.record(proposal(token=str(n), symbol='=WEBSERVICE("bad")' if n==0 else 'ABC'+str(n)))
        before = a.connection().total_changes
        result = a.export_report(day='2026-09-14', strategy='SCALP')
        rows = list(csv.DictReader(io.StringIO(result['content'])))
        self.assertEqual(len(rows), 202)
        self.assertEqual(len({r['Strike / contract'] for r in rows}), 202)
        self.assertTrue(next(r for r in rows if 'WEBSERVICE' in r['Strike / contract'])['Strike / contract'].startswith("'="))
        self.assertEqual(a.csv_value({'v':-10}, 'v'), -10)
        self.assertTrue(a.csv_value({'v':'\x00  =1+1'}, 'v').startswith("'"))
        self.assertEqual(a.connection().total_changes, before)
        self.assertFalse(a.connection().in_transaction)

    def test_report_count_and_rows_share_one_snapshot(self):
        a.record(proposal())
        c = a.connection()
        added = []
        other = sqlite3.connect(f.storage.DB_PATH)
        self.addCleanup(other.close)
        def concurrent_write(sql):
            if 'SELECT * FROM rows' in sql and not added:
                added.append(True)
                other.execute("UPDATE trade_audit_plans SET symbol='CHANGED'")
                other.commit()
        c.set_trace_callback(concurrent_write)
        try:
            self.assertEqual(self.rows()[0]['symbol'], 'TESTCE')
        finally:
            c.set_trace_callback(None)
        self.assertEqual(self.rows()[0]['symbol'], 'CHANGED')

    def test_capture_failure_does_not_suppress_broker_exit(self):
        with patch.object(a, 'record_submission', side_effect=sqlite3.OperationalError('private details')):
            result = f.ledger.submit(self.pos, 'SELL', 10, 99)
        self.assertTrue(result['pending'])
        self.place.assert_called_once()
        self.assertEqual(f.storage.conn().execute('PRAGMA busy_timeout').fetchone()[0], 30000)

    def test_capture_never_commits_an_existing_execution_transaction(self):
        c = f.storage.conn()
        c.execute('BEGIN')
        called = Mock()
        self.assertIsNone(a.safe_call(called))
        called.assert_not_called()
        self.assertTrue(c.in_transaction)
        c.rollback()


if __name__ == '__main__':
    unittest.main()
