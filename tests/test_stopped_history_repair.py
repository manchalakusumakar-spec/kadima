"""File-backed, offline stopped-history transaction tests; synthetic account data."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'momentum_recovery'/filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


module = load('_test_stopped_history_repair', 'repair_stopped_history.py')
note = load('_test_stopped_history_note', 'reconcile_contract_note.py')
readiness = load('_test_stopped_history_readiness', 'check_stopped_readiness.py')
broker_read = load('_test_stopped_history_broker', 'stopped_broker_read.py')
NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)


class StoppedHistoryRepair(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root/'kadima.db'
        self.backups = self.root/'backups'
        self.backups.mkdir(mode=0o700)
        self.evidence = dict(
            document_sha256='a'*64, signal_id=1, quantity=60,
            symbol='TESTPE', token='123', exchange='BFO', trade_date='2026-09-24',
            expiry='2026-09-24', entry_at='2026-09-24T09:32:00+05:30',
            intent_at='2026-09-24T09:33:00+05:30', exit_at='2026-09-24T09:39:00+05:30',
            entry_price=100, exit_price=120, expected_entry_price=105,
            account_sha256=hashlib.sha256(b'TESTACCOUNT').hexdigest(),
            exchange_order_id='987', trade_id='654', intent='Ktest',
            day_buy_qty=90, day_sell_qty=90)
        with sqlite3.connect(self.db) as connection:
            connection.executescript('''
                CREATE TABLE positions(signal_id INTEGER, symbol TEXT, token TEXT, exchange TEXT,
                    kind TEXT, status TEXT, remaining_qty INTEGER, lots INTEGER, lot_size INTEGER,
                    entry_price REAL, entry_ts TEXT, exit_price REAL, exit_ts TEXT,
                    exit_order_id TEXT, exit_reason TEXT);
                CREATE TABLE execution_orders(intent TEXT, signal_id INTEGER, side TEXT,
                    quantity INTEGER, order_id TEXT, state TEXT, filled INTEGER,
                    notional REAL, created_at TEXT, dry_run INTEGER);
                CREATE TABLE execution_fills(intent TEXT);
                CREATE TABLE celestial_external_fills(signal_id INTEGER, order_id TEXT,
                    cumulative_qty INTEGER, quantity INTEGER, price REAL, pnl REAL, ts TEXT,
                    PRIMARY KEY(signal_id, order_id, cumulative_qty));
                CREATE TABLE celestial_config(id INTEGER, payload TEXT);
                CREATE TABLE stock_fast_controls(id INTEGER, payload TEXT);
            ''')
            connection.execute('INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (1, 'TESTPE', '123', 'BFO', 'MANUAL_ADOPTED', 'exit_pending', 60, 2, 30,
                 105, self.evidence['entry_at'], None, None, None, 'trail'))
            connection.execute('INSERT INTO execution_orders VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('Ktest', 1, 'SELL', 60, None, 'unknown', 0, 0, self.evidence['intent_at'], 0))
            connection.execute('INSERT INTO celestial_config VALUES (1,?)',
                (json.dumps(dict(auto_trade_enabled=False, kill_latched=False)),))
            connection.execute('INSERT INTO stock_fast_controls VALUES (1,?)',
                (json.dumps(dict(live_enabled=False, revision=14)),))
        os.chmod(self.db, 0o600)
        self.expected = readiness.local_readiness(self.db, note, self.evidence)
        self.original = self.snapshot()
        self.guard = Mock()
        self.broker = Mock(side_effect=lambda: self.receipt())

    def receipt(self, **changes):
        result = dict(
            started_at=(NOW-timedelta(seconds=2)).isoformat(),
            checked_at=(NOW-timedelta(seconds=1)).isoformat(),
            books_complete=True, flat=False, readiness=True,
            historical_contract_absent=True, permitted_manual_holding_observed=True,
            position_rows=1, order_rows=0, pending_orders=0, pending_order_details=[],
            nonzero_positions=[dict(broker_read.ALLOWED_HOLDING)],
            nonzero_position_count=1, error=None)
        result.update(changes)
        return broker_read.validate_receipt(result, NOW)

    def pending_receipt(self):
        pending = dict(exchange='NFO', tradingsymbol='NIFTY29SEP2623250CE',
            symboltoken='73921', producttype='CARRYFORWARD', variety='NORMAL',
            orderid='SYNTHETIC123', transactiontype='SELL', status='open', quantity=130,
            filled_quantity=0, remaining_quantity=130, price=200.0, trigger_price=None)
        return self.receipt(order_rows=1, pending_orders=1, pending_order_details=[pending],
            readiness=False, error=dict(stage='orderBook',
            reason='unresolved_broker_orders', type='ValueError'))

    def run_repair(self, *, apply=True, **changes):
        arguments = dict(db=self.db, note=note, evidence=self.evidence,
                         expected_local=self.expected, broker=self.broker,
                         guard=self.guard, backup_root=self.backups,
                         now=lambda: NOW, apply=apply,
                         validate_broker=broker_read.validate_receipt)
        arguments.update(changes)
        return module.repair_transaction(**arguments)

    def snapshot(self, path=None):
        with sqlite3.connect(path or self.db) as connection:
            return '\n'.join(connection.iterdump())

    def rows(self, table):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute('SELECT * FROM '+table)]

    def execute(self, sql, parameters=()):
        with sqlite3.connect(self.db) as connection:
            connection.execute(sql, parameters)

    def blocked(self):
        return self.assertRaises((RuntimeError, ValueError, sqlite3.Error))

    def test_plan_reads_broker_without_mutating_database_or_creating_backup(self):
        before = self.db.stat()
        report = self.run_repair(apply=False)
        after = self.db.stat()
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(self.broker.call_count, 1)
        self.assertFalse(report['restart_requested'])
        self.assertFalse(report['activation'])
        self.assertFalse(report['broker_flat_verified'])

    def test_apply_archives_unknown_intent_and_records_only_historical_external_fill(self):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            original_position, original_order = note.inspect(connection, self.evidence)
        report = self.run_repair()
        self.assertEqual(self.broker.call_count, 2)
        self.assertFalse(report['restart_requested'])
        self.assertFalse(report['activation'])
        self.assertFalse(report['broker_flat_verified'])
        position, = self.rows('positions')
        self.assertEqual(position, dict(original_position, entry_price=100.0, exit_price=120.0,
            exit_ts=self.evidence['exit_at'], remaining_qty=0, status='closed',
            exit_reason='external_fill_reconciled'))
        order, = self.rows('execution_orders')
        self.assertEqual(order, dict(original_order, state='terminal'))
        self.assertIsNone(order['order_id'])
        self.assertEqual((order['filled'], order['notional']), (0, 0))
        self.assertEqual(self.rows('execution_fills'), [])
        external, = self.rows('celestial_external_fills')
        self.assertEqual(external, dict(signal_id=1, order_id='EXCHANGE:BFO:987:654',
            cumulative_qty=60, quantity=60, price=120.0, pnl=1200.0,
            ts=self.evidence['exit_at']))
        archive, = self.rows('contract_note_reconciliations')
        self.assertEqual(json.loads(archive['evidence']), self.evidence)
        self.assertEqual(json.loads(archive['original_position']), original_position)
        self.assertEqual(json.loads(archive['original_order']), original_order)
        receipts = json.loads(archive['broker_receipt'])
        self.assertEqual(set(receipts),
                         {'schema', 'manual_holding_external', 'before_backup', 'before_commit'})
        self.assertEqual(receipts['schema'], 'stopped_historical_repair_v1')
        self.assertTrue(receipts['manual_holding_external'])
        for receipt in (receipts['before_backup'], receipts['before_commit']):
            self.assertFalse(receipt['flat'])
            self.assertTrue(receipt['permitted_manual_holding_observed'])
        self.assertEqual(json.loads(self.rows('celestial_config')[0]['payload']),
                         dict(auto_trade_enabled=False, kill_latched=False))
        self.assertEqual(json.loads(self.rows('stock_fast_controls')[0]['payload']),
                         dict(live_enabled=False, revision=14))

    def test_backup_is_private_and_contains_original_state(self):
        self.run_repair()
        candidates = []
        for path in self.backups.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
                if path.read_bytes().startswith(b'SQLite format 3\0'):
                    candidates.append(path)
            elif path.is_dir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.snapshot(candidates[0]), self.original)

    def test_backup_includes_committed_wal_contents(self):
        with sqlite3.connect(self.db) as keeper:
            self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            keeper.execute("UPDATE positions SET exit_reason='committed_in_wal'")
            keeper.commit()
            self.assertTrue(Path(str(self.db)+'-wal').is_file())
            self.expected = readiness.local_readiness(self.db, note, self.evidence)
            before = self.snapshot()
            report = self.run_repair()
            self.assertEqual(self.snapshot(Path(report['backup'])), before)

    def test_pending_broker_order_blocks_without_changes(self):
        self.broker.side_effect = self.pending_receipt
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), self.original)

    def test_second_broker_read_blocker_rolls_back(self):
        pending = self.pending_receipt()
        self.broker.side_effect = [self.receipt(), pending]
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.broker.call_count, 2)
        self.assertEqual(self.snapshot(), self.original)

    def test_outer_guard_failure_prevents_repair(self):
        self.guard.side_effect = RuntimeError('synthetic_guard_failure')
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), self.original)

    def test_guard_rechecks_after_final_broker_read_before_committing(self):
        def guard():
            if self.broker.call_count == 2:
                raise RuntimeError('synthetic_context_changed_during_broker_read')
        self.guard.side_effect = guard
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.broker.call_count, 2)
        self.assertEqual(self.snapshot(), self.original)

    def test_final_receipt_cannot_expire_during_guard_before_commit(self):
        observed_now = [NOW]
        def guard():
            if self.broker.call_count == 2:
                observed_now[0] = NOW+timedelta(seconds=31)
        self.guard.side_effect = guard
        with self.blocked():
            self.run_repair(now=lambda: observed_now[0])
        self.assertEqual(self.broker.call_count, 2)
        self.assertEqual(self.snapshot(), self.original)

    def test_stale_broker_receipt_is_rejected_independently_of_callback(self):
        stale = self.receipt()
        stale['started_at'] = (NOW-timedelta(minutes=3)).isoformat()
        stale['checked_at'] = (NOW-timedelta(minutes=2)).isoformat()
        self.broker.side_effect = lambda: deepcopy(stale)
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), self.original)

    def test_misleading_readiness_with_unpermitted_holding_is_rejected(self):
        invalid = self.receipt()
        invalid['nonzero_positions'][0]['netqty'] = 131
        self.broker.side_effect = lambda: deepcopy(invalid)
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), self.original)

    def test_changed_reviewed_row_cannot_be_repaired(self):
        self.execute("UPDATE positions SET exit_reason='operator_changed'")
        changed = self.snapshot()
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), changed)

    def test_database_replacement_with_same_rows_is_rejected(self):
        replacement = self.root/'replacement.db'
        with sqlite3.connect(self.db) as connection, sqlite3.connect(replacement) as target:
            connection.backup(target)
        os.chmod(replacement, 0o600)
        replacement.replace(self.db)
        self.assertEqual(self.snapshot(), self.original)
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), self.original)

    def test_database_symlink_is_rejected_even_with_matching_identity(self):
        alias = self.root/'alias.db'
        alias.symlink_to(self.db)
        with self.blocked():
            self.run_repair(db=alias)
        self.assertEqual(self.snapshot(), self.original)

    def test_mismatched_reviewed_row_hash_is_rejected(self):
        expected = dict(self.expected, historical_rows_sha256='0'*64)
        with self.blocked():
            self.run_repair(expected_local=expected)
        self.assertEqual(self.snapshot(), self.original)

    def test_persisted_control_and_revision_changes_block(self):
        cases = (
            ('celestial_config', dict(auto_trade_enabled=True, kill_latched=False)),
            ('celestial_config', dict(auto_trade_enabled=False, kill_latched=True)),
            ('stock_fast_controls', dict(live_enabled=True, revision=14)),
            ('stock_fast_controls', dict(live_enabled=False, revision=15)),
        )
        for table, payload in cases:
            with self.subTest(table=table, payload=payload):
                original = self.rows(table)[0]['payload']
                self.execute('UPDATE '+table+' SET payload=? WHERE id=1', (json.dumps(payload),))
                changed = self.snapshot()
                with self.blocked():
                    self.run_repair()
                self.assertEqual(self.snapshot(), changed)
                self.execute('UPDATE '+table+' SET payload=? WHERE id=1', (original,))

    def test_missing_persisted_controls_block(self):
        self.execute('DELETE FROM stock_fast_controls')
        changed = self.snapshot()
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), changed)

    def test_additional_local_pending_order_blocks(self):
        self.execute("INSERT INTO execution_orders VALUES "
            "('OTHER',2,'SELL',10,NULL,'unknown',0,0,'2026-09-24',0)")
        changed = self.snapshot()
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), changed)

    def test_database_failure_rolls_back_archive_and_ledger_changes(self):
        self.execute("CREATE TRIGGER refuse_fill BEFORE INSERT ON celestial_external_fills "
            "BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
        changed = self.snapshot()
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), changed)

    def test_replay_fails_closed_without_duplicate_profit(self):
        self.run_repair()
        repaired = self.snapshot()
        with self.blocked():
            self.run_repair()
        self.assertEqual(self.snapshot(), repaired)
        self.assertEqual(len(self.rows('celestial_external_fills')), 1)

    def test_cleanup_failure_keeps_durable_commit_visible(self):
        with patch.object(module, 'preserve_sidecar_ownership',
                          side_effect=RuntimeError('synthetic_cleanup_failure')):
            with self.assertRaises(RuntimeError) as failure:
                self.run_repair()
        self.assertEqual(self.rows('positions')[0]['status'], 'closed')
        self.assertTrue(failure.exception.report['ledger_repaired'])
        self.assertTrue(failure.exception.report['state'].startswith('blocked_'))

    def test_status_write_failure_keeps_durable_commit_visible(self):
        def record(report):
            if report['ledger_repaired']:
                raise RuntimeError('synthetic_status_write_failure')
        with self.assertRaises(RuntimeError) as failure:
            self.run_repair(record=record)
        self.assertEqual(self.rows('positions')[0]['status'], 'closed')
        self.assertTrue(failure.exception.report['ledger_repaired'])
        self.assertTrue(failure.exception.report['state'].startswith('blocked_'))

    def test_exception_after_sqlite_commit_reports_uncertainty(self):
        original_repair = note.repair
        def commit_then_raise(*arguments, **keywords):
            original_repair(*arguments, **keywords)
            raise RuntimeError('synthetic_failure_after_commit_return')
        with patch.object(note, 'repair', side_effect=commit_then_raise):
            with self.assertRaises(RuntimeError) as failure:
                self.run_repair()
        self.assertEqual(self.rows('positions')[0]['status'], 'closed')
        self.assertIsNone(failure.exception.report['ledger_repaired'])
        self.assertTrue(failure.exception.report['commit_attempted'])
        self.assertEqual(failure.exception.report['state'], 'blocked_commit_outcome_uncertain')


@unittest.skipUnless(os.geteuid() == 0, 'ownership normalization requires root for real fchown tests')
class SidecarOwnershipNormalization(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root/'kadima.db'
        self.db.write_bytes(b'synthetic database placeholder; never opened as SQLite')
        os.chmod(self.db, 0o600)
        self.owner = (1000, 1000)
        self.guard = Mock()

    def chown_or_skip(self, path, uid, gid):
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            if exc.errno in (1, 22, 95):
                self.skipTest('runtime UID mapping cannot perform the required ownership change')
            raise

    def create_sidecar(self, suffix='-wal', *, uid=0, gid=0, mode=0o640):
        path = Path(str(self.db)+suffix)
        path.write_bytes(b'synthetic sidecar bytes\x00\xff'+suffix.encode())
        self.chown_or_skip(path, uid, gid)
        os.chmod(path, mode)
        return path

    def normalize(self, *, apply=True, guard=None):
        previous = module.sidecars(self.db)
        receipts = []
        module.normalize_sidecars(self.db, previous, self.owner, apply,
                                  guard if guard is not None else self.guard, receipts)
        return previous, receipts

    def test_only_root_owned_wal_and_shm_ownership_changes_with_receipts(self):
        self.chown_or_skip(self.db, *self.owner)
        paths = [self.create_sidecar(suffix) for suffix in ('-wal', '-shm')]
        original = {path: (path.read_bytes(), path.stat()) for path in paths}
        previous, receipts = self.normalize()
        self.assertEqual(self.guard.call_count, 4)
        self.assertEqual({row['suffix'] for row in receipts}, {'-wal', '-shm'})
        for path in paths:
            before_bytes, before = original[path]
            after = path.stat()
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual((after.st_uid, after.st_gid), self.owner)
            unchanged = lambda info: (info.st_dev, info.st_ino, info.st_nlink,
                stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns)
            self.assertEqual(unchanged(after), unchanged(before))
            self.assertEqual(previous[path], after)
            receipt, = [row for row in receipts if row['suffix'] == str(path)[len(str(self.db)):]]
            self.assertEqual((receipt['before']['uid'], receipt['before']['gid']), (0, 0))
            self.assertEqual((receipt['after']['uid'], receipt['after']['gid']), self.owner)
            self.assertTrue(receipt['ownership_restored'])
            self.assertTrue(receipt['ownership_change_attempted'])
            self.assertTrue(receipt['verification_complete'])
            self.assertTrue(receipt['contents_unchanged'])
            self.assertEqual(receipt['content_sha256'], hashlib.sha256(before_bytes).hexdigest())

    def test_plan_reports_repair_needed_without_changing_ownership(self):
        path = self.create_sidecar()
        before = path.stat()
        previous, receipts = module.sidecars(self.db), []
        with self.assertRaisesRegex(module.Blocked, 'ownership_repair_required'):
            module.normalize_sidecars(self.db, previous, self.owner, False, self.guard, receipts)
        self.assertEqual(path.stat(), before)
        self.guard.assert_not_called()
        self.assertEqual(len(receipts), 1)
        self.assertFalse(receipts[0]['ownership_restored'])
        self.assertNotIn('after', receipts[0])

    def test_matching_owner_is_noop_in_plan(self):
        self.owner = (0, 0)
        path = self.create_sidecar()
        before = path.stat()
        _, receipts = self.normalize(apply=False)
        self.assertEqual(path.stat(), before)
        self.guard.assert_not_called()
        self.assertFalse(receipts[0]['ownership_restored'])

    def test_unreviewed_owner_group_or_mode_metadata_blocks_without_mutation(self):
        for uid, gid, mode in ((2000, 1000, 0o600), (0, 2000, 0o600),
                               (1000, 0, 0o600), (0, 0, 0o660), (0, 0, 0o4640)):
            with self.subTest(uid=uid, gid=gid, mode=oct(mode)):
                path = self.create_sidecar()
                before = path.stat()
                attributes = {name: getattr(before, name) for name in dir(before)
                              if name.startswith('st_')}
                attributes.update(st_uid=uid, st_gid=gid,
                                  st_mode=stat.S_IFMT(before.st_mode) | mode)
                previous = module.sidecars(self.db)
                previous[path] = SimpleNamespace(**attributes)
                with self.assertRaises(module.Blocked):
                    module.normalize_sidecars(self.db, previous, self.owner, True, self.guard, [])
                self.assertEqual(path.stat(), before)
                path.unlink()

    def test_fchown_failure_records_uncertain_attempt_without_opening_database(self):
        path = self.create_sidecar()
        before = path.stat()
        previous, receipts = module.sidecars(self.db), []
        with patch.object(module.os, 'fchown', side_effect=PermissionError('synthetic_chown_failure')), \
             patch.object(module.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(PermissionError, 'synthetic_chown_failure'):
                module.normalize_sidecars(self.db, previous, self.owner, True, self.guard, receipts)
            connect.assert_not_called()
        self.assertEqual(path.stat(), before)
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]['ownership_change_attempted'])
        self.assertIsNone(receipts[0]['ownership_restored'])
        self.assertFalse(receipts[0]['verification_complete'])

    def test_fsync_failure_does_not_claim_verified_ownership(self):
        path = self.create_sidecar()
        before = path.stat()
        previous, receipts = module.sidecars(self.db), []
        with patch.object(module.os, 'fchown'), \
             patch.object(module.os, 'fsync', side_effect=OSError('synthetic_fsync_failure')), \
             patch.object(module.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(OSError, 'synthetic_fsync_failure'):
                module.normalize_sidecars(self.db, previous, self.owner, True, self.guard, receipts)
            connect.assert_not_called()
        self.assertEqual(path.stat(), before)
        self.assertTrue(receipts[0]['ownership_change_attempted'])
        self.assertFalse(receipts[0]['ownership_restored'])
        self.assertFalse(receipts[0]['verification_complete'])
        self.assertNotIn('contents_unchanged', receipts[0])

    def test_root_owned_rollback_journal_is_not_normalized(self):
        path = self.create_sidecar('-journal')
        before = path.stat()
        with self.assertRaisesRegex(module.Blocked, 'owner_unreviewed'):
            self.normalize()
        self.assertEqual(path.stat(), before)

    def test_sidecar_symlink_directory_fifo_or_hardlink_is_rejected(self):
        for kind in ('symlink', 'directory', 'fifo', 'hardlink'):
            with self.subTest(kind=kind):
                path = Path(str(self.db)+'-wal')
                target = self.root/('target-'+kind)
                if kind == 'symlink':
                    target.write_bytes(b'untouched target')
                    path.symlink_to(target)
                elif kind == 'directory':
                    path.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    target.write_bytes(b'untouched target')
                    os.link(target, path)
                before = path.lstat()
                with self.assertRaisesRegex(module.Blocked, 'sidecar_unreviewed'):
                    self.normalize()
                self.assertEqual(path.lstat(), before)
                if kind == 'directory':
                    path.rmdir()
                else:
                    path.unlink()

    def test_guard_failure_before_fchown_preserves_ownership(self):
        for fail_on in (1, 2):
            with self.subTest(fail_on=fail_on):
                path = self.create_sidecar()
                before = path.stat()
                guard = Mock(side_effect=[None]*(fail_on-1)+[RuntimeError('synthetic_guard_failure')])
                with patch.object(module.os, 'fchown', wraps=os.fchown) as fchown:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic_guard_failure'):
                        self.normalize(guard=guard)
                    fchown.assert_not_called()
                self.assertEqual(path.stat(), before)
                path.unlink()


if __name__ == '__main__':
    unittest.main()
