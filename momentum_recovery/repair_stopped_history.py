"""Repair only the reviewed expired BANKEX ledger case while services stay stopped.

Default is a plan. --apply requires fresh complete broker books without pending
orders, exact reviewed local rows, and a consistent SQLite backup. The external
manual NIFTY holding is never closed, adopted, or described as account flat.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parents[1]
READINESS_BLOB = '5072a286604016d29c60dbafd84687e7079e89be'
STATE = Path('/var/lib/kadima-stopped-history-20260928')
EVIDENCE_SHA256 = 'fd62dcb69291a1689925c3df88f528f0a57511f2e40f78d57808f11eb08efef0'
ROWS_SHA256 = '4e7c0325e185417baa8124d0116c66325c6e4c24821c297040a67d6c906a3dfa'
DB_IDENTITY = (66305, 778925, 1000, 1000, 1)


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def utc_now():
    return datetime.now(timezone.utc)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink)


def digest_rows(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def controls(c):
    cfg = c.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
    stock = c.execute('SELECT payload FROM stock_fast_controls WHERE id=1').fetchone()
    require(cfg is not None and stock is not None, 'persisted_controls_required')
    cfg, stock = json.loads(cfg[0]), json.loads(stock[0])
    result = dict(auto_trade_enabled=cfg.get('auto_trade_enabled'), kill_latched=cfg.get('kill_latched'),
                  stock_live_enabled=stock.get('live_enabled'), stock_revision=stock.get('revision'))
    require(result['auto_trade_enabled'] is False and result['kill_latched'] is False
            and result['stock_live_enabled'] is False and type(result['stock_revision']) is int,
            'persisted_pause_required')
    return result


def inspect_case(c, note, evidence, expected_local):
    require(controls(c) == expected_local['controls'], 'persisted_controls_changed')
    expected = note.inspect(c, evidence)
    require(digest_rows(expected) == expected_local['historical_rows_sha256'], 'reviewed_historical_rows_changed')
    return expected


def verify_committed(c, e, expected, receipt):
    """Exact outcomes with a truthful stopped/manual-holding receipt schema."""
    p, order = expected
    row = c.execute('SELECT * FROM positions WHERE signal_id=?', (e['signal_id'],)).fetchone()
    wanted = dict(p, entry_price=float(e['entry_price']), exit_price=float(e['exit_price']),
                  exit_ts=e['exit_at'], remaining_qty=0, status='closed', exit_reason='external_fill_reconciled')
    require(row is not None and dict(row) == wanted, 'repaired_position_changed')
    row = c.execute('SELECT * FROM execution_orders WHERE intent=?', (e['intent'],)).fetchone()
    require(row is not None and dict(row) == dict(order, state='terminal'), 'repaired_intent_changed')
    require(c.execute("SELECT 1 FROM positions WHERE status IS NULL OR status!='closed'").fetchone() is None
            and c.execute("SELECT 1 FROM execution_orders WHERE state IS NULL OR state!='terminal'").fetchone() is None,
            'unresolved_local_state_after_repair')
    require(c.execute('SELECT 1 FROM execution_fills WHERE intent=?', (e['intent'],)).fetchone() is None,
            'unexpected_internal_fill_after_repair')
    archive = c.execute('SELECT * FROM contract_note_reconciliations WHERE intent=?', (e['intent'],)).fetchone()
    require(archive is not None and json.loads(archive['evidence']) == e
            and json.loads(archive['original_position']) == p and json.loads(archive['original_order']) == order
            and json.loads(archive['broker_receipt']) == receipt, 'repair_archive_changed')
    pnl = (Decimal(str(e['exit_price'])) - Decimal(str(e['entry_price']))) * e['quantity']
    reference = 'EXCHANGE:'+e['exchange']+':'+str(e['exchange_order_id'])+':'+str(e['trade_id'])
    fills = [dict(row) for row in c.execute('SELECT * FROM celestial_external_fills WHERE signal_id=?', (e['signal_id'],))]
    require(fills == [dict(signal_id=e['signal_id'], order_id=reference, cumulative_qty=e['quantity'],
                          quantity=e['quantity'], price=float(e['exit_price']), pnl=float(pnl), ts=e['exit_at'])],
            'historical_external_fill_changed')


def backup_database(c, db, root, note, evidence, expected_local):
    require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'canonical_backup_root_required')
    require(shutil.disk_usage(root).free > db.stat().st_size * 1.2 + 100_000_000, 'backup_space_required')
    directory = Path(tempfile.mkdtemp(prefix='kadima-stopped-history-', dir=root))
    directory.chmod(0o700)
    destination = directory/'before.db'
    deadline = time.monotonic() + 60
    def progress(*unused):
        require(time.monotonic() < deadline, 'backup_timed_out')
    with closing(sqlite3.connect(destination)) as backup:
        c.backup(backup, pages=1024, progress=progress)
        backup.row_factory = sqlite3.Row
        require([row[0] for row in backup.execute('PRAGMA quick_check')] == ['ok'], 'backup_integrity_failed')
        inspect_case(backup, note, evidence, expected_local)
    destination.chmod(0o600)
    with destination.open('rb') as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return destination


def sidecars(db):
    result = {}
    for suffix in ('-wal', '-shm', '-journal'):
        path = Path(str(db)+suffix)
        if not path.exists() and not path.is_symlink():
            result[path] = None
            continue
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not path.is_symlink(), 'database_sidecar_unreviewed')
        result[path] = info
    return result


def normalize_sidecars(db, previous, owner, apply, guard, receipts):
    """Restore only reviewed root-created WAL/SHM ownership, never contents."""
    def stamp(info):
        return (*identity(info), stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def summary(info):
        return dict(uid=info.st_uid, gid=info.st_gid, mode=format(stat.S_IMODE(info.st_mode), '04o'),
                    device=info.st_dev, inode=info.st_ino, size=info.st_size)
    def content(stream):
        stream.seek(0)
        digest = hashlib.sha256()
        remaining = 1_000_000_001
        while remaining:
            part = stream.read(min(1_048_576, remaining))
            if not part:
                return digest.hexdigest()
            digest.update(part)
            remaining -= len(part)
        raise Blocked('database_sidecar_oversized')
    for path, first in previous.items():
        if first is None:
            continue
        require(first.st_uid in (0, owner[0]) and first.st_gid in (0, owner[1])
                and not first.st_mode & 0o7022, 'database_sidecar_owner_or_mode_unreviewed')
        needs_restore = (first.st_uid, first.st_gid) != owner
        receipt = dict(suffix=str(path)[len(str(db)):], before=summary(first), ownership_restored=False,
                       ownership_change_attempted=False, verification_complete=False)
        receipts.append(receipt)
        if not needs_restore:
            continue
        require(first.st_uid == 0 and str(path).endswith(('-wal', '-shm')), 'database_sidecar_owner_unreviewed')
        require(apply, 'database_sidecar_ownership_repair_required')
        guard()
        require(path.resolve(strict=True) == path, 'database_sidecar_path_unreviewed')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stamp(before) == stamp(first), 'database_sidecar_changed')
            raw_digest = content(stream)
            require(stamp(os.fstat(stream.fileno())) == stamp(before), 'database_sidecar_changed')
            guard()
            require(stamp(os.fstat(stream.fileno())) == stamp(before), 'database_sidecar_changed')
            receipt.update(ownership_change_attempted=True, ownership_restored=None)
            os.fchown(stream.fileno(), *owner)
            changed = os.fstat(stream.fileno())
            receipt.update(after=summary(changed), ownership_restored=(changed.st_uid, changed.st_gid) == owner)
            os.fsync(stream.fileno())
            require(content(stream) == raw_digest, 'database_sidecar_content_changed')
            after = os.fstat(stream.fileno())
            require((after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns,
                     stat.S_IMODE(after.st_mode)) ==
                    (before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns,
                     stat.S_IMODE(before.st_mode)) and (after.st_uid, after.st_gid) == owner
                    and path.resolve(strict=True) == path and stamp(path.lstat()) == stamp(after),
                    'database_sidecar_ownership_verification_failed')
            receipt.update(after=summary(after), ownership_restored=True, contents_unchanged=True,
                           content_sha256=raw_digest, verification_complete=True)
            previous[path] = after


def preserve_sidecar_ownership(db, previous, owner, receipts=None):
    """Only newly created root-owned SQLite sidecars may need owner restoration."""
    for path, info in sidecars(db).items():
        if info is None:
            continue
        old = previous[path]
        require(info.st_uid == owner[0] or (old is None and info.st_uid == 0), 'database_sidecar_owner_changed')
        if old is None and info.st_uid == 0 and owner[0] != 0:
            receipt = dict(suffix=str(path)[len(str(db)):], newly_created=True,
                           before=dict(uid=info.st_uid, gid=info.st_gid, mode=format(stat.S_IMODE(info.st_mode), '04o')),
                           ownership_change_attempted=False, ownership_restored=False, verification_complete=False)
            if receipts is not None:
                receipts.append(receipt)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                require(identity(os.fstat(descriptor)) == identity(info), 'database_sidecar_changed')
                receipt.update(ownership_change_attempted=True, ownership_restored=None)
                os.fchown(descriptor, *owner)
                after = os.fstat(descriptor)
                receipt.update(after=dict(uid=after.st_uid, gid=after.st_gid, mode=format(stat.S_IMODE(after.st_mode), '04o')),
                               ownership_restored=(after.st_uid, after.st_gid) == owner)
                os.fsync(descriptor)
                require((after.st_uid, after.st_gid) == owner and stat.S_IMODE(after.st_mode) == stat.S_IMODE(info.st_mode)
                        and (after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns) ==
                            (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns)
                        and identity(path.lstat()) == identity(after), 'database_sidecar_ownership_verification_failed')
                receipt['verification_complete'] = True
            finally:
                os.close(descriptor)


def repair_transaction(db, note, evidence, expected_local, broker, guard, backup_root, *,
                       validate_broker, now=utc_now, apply=False, claim=lambda: None, record=lambda report: None):
    report = dict(action='stopped_historical_repair' if apply else 'stopped_historical_repair_plan',
                  state='checking', ledger_repaired=False, restart_requested=False, activation=False,
                  broker_flat_verified=False, broker_receipts={}, backup=None, commit_attempted=False,
                  sidecar_ownership=[])
    db, backup_root = Path(db), Path(backup_root)
    require(db.is_absolute() and db.resolve() == db and db.is_file(), 'canonical_existing_db_required')
    metadata = db.stat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
            and identity(metadata) == tuple(expected_local['database_identity']), 'reviewed_database_identity_changed')
    require(expected_local['persisted_pause_verified'] is True and expected_local['reviewed_historical_case_matches'] is True
            and expected_local['unresolved_positions'] == expected_local['unresolved_orders'] == 1,
            'reviewed_local_readiness_required')
    previous = sidecars(db)
    sqlite_opened = False

    def persist():
        try:
            record(report)
        except Exception as exc:
            report['state'] = 'blocked_status_recording'
            exc.report = report
            raise

    def checked_broker(stage):
        receipt = validate_broker(broker(), now())
        report['broker_receipts'][stage] = receipt
        report['broker_flat_verified'] = receipt['flat'] is True
        require(receipt['books_complete'] is True and receipt['readiness'] is True
                and receipt['historical_contract_absent'] is True and receipt['pending_orders'] == 0,
                'fresh_broker_readiness_required')
        return receipt

    def guarded():
        guard()
        require(identity(db.stat()) == identity(metadata), 'database_identity_changed')

    try:
        guarded()
        normalize_sidecars(db, previous, (metadata.st_uid, metadata.st_gid), apply, guarded,
                           report['sidecar_ownership'])
        sqlite_opened = True
        with closing(sqlite3.connect(db.as_uri()+('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=5)) as c:
            c.row_factory = sqlite3.Row
            if not apply:
                c.execute('PRAGMA query_only=ON')
            expected = inspect_case(c, note, evidence, expected_local)
            first = checked_broker('before_backup')
            guarded()
            require(inspect_case(c, note, evidence, expected_local) == expected, 'historical_case_changed')
            if not apply:
                report['state'] = 'stopped_historical_repair_planned'
                return report
            backup = backup_database(c, db, backup_root, note, evidence, expected_local)
            report['backup'] = str(backup)
            guarded()
            receipt = dict(schema='stopped_historical_repair_v1', manual_holding_external=True,
                           before_backup=first, before_commit=None)
            def commit_guard(connection):
                guarded()
                require(inspect_case(connection, note, evidence, expected_local) == expected, 'historical_case_changed')
                receipt['before_commit'] = checked_broker('before_commit')
                guarded()
                require(inspect_case(connection, note, evidence, expected_local) == expected, 'historical_case_changed')
                claim()
                report['commit_attempted'] = True
                report['state'] = 'committing'
                persist()
                # Service/source inspection and durable claim recording can
                # take time; revalidate immediately before returning to SQL.
                validate_broker(receipt['before_commit'], now())
            note.repair(c, evidence, expected, receipt, now(), commit_guard)
            report['ledger_repaired'] = True
            verify_committed(c, evidence, expected, receipt)
            require(controls(c) == expected_local['controls'], 'persisted_controls_changed')
        guarded()
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=5)) as verified:
            verified.row_factory = sqlite3.Row
            verified.execute('PRAGMA query_only=ON')
            verify_committed(verified, evidence, expected, receipt)
            require(controls(verified) == expected_local['controls'], 'persisted_controls_changed')
        guarded()
        report['state'] = 'historical_repair_committed_services_stopped'
        report['original_rows_archived'] = True
        report['unknown_intent_fill_fabricated'] = False
        return report
    except BaseException as exc:
        if report['ledger_repaired']:
            report['state'] = 'blocked_after_commit'
        elif report['commit_attempted']:
            report['state'] = 'blocked_commit_outcome_uncertain'
            report['ledger_repaired'] = None
        else:
            report['state'] = 'blocked_without_commit'
        persist()
        if isinstance(exc, Exception):
            exc.report = report
        raise
    finally:
        try:
            if apply and sqlite_opened:
                preserve_sidecar_ownership(db, previous, (metadata.st_uid, metadata.st_gid), report['sidecar_ownership'])
        except Exception as exc:
            report['state'] = 'blocked_sidecar_verification'
            exc.report = report
            persist()
            raise
        if report['state'] == 'historical_repair_committed_services_stopped':
            persist()


def load_readiness():
    path = PACKAGE/'momentum_recovery/check_stopped_readiness.py'
    require(path.resolve() == path and not path.is_symlink(), 'readiness_helper_path_unreviewed')
    raw = path.read_bytes()
    require(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() == READINESS_BLOB,
            'reviewed_readiness_helper_required')
    spec = importlib.util.spec_from_file_location('_stopped_history_readiness', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    os.umask(0o077)
    sys.dont_write_bytecode = True
    report = dict(ledger_repaired=False, restart_requested=False, activation=False)
    try:
        ready = load_readiness()
        helper, prep, source, note, context_module, broker_module, hold = (ready.load_reviewed(name) for name in ready.PINS)
        user = pwd.getpwnam('ubuntu')
        with ExitStack() as locks:
            for name in source.LOCKS:
                path = Path('/run/lock')/(name+'.lock')
                require(path.resolve(strict=False) == path, 'lock_path_unreviewed')
                handle = locks.enter_context(os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w'))
                info = os.fstat(handle.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                        and not stat.S_IMODE(info.st_mode) & 0o022, 'private_lock_required')
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, fingerprints = ready.verify_maintenance(helper, ready.AUDIT_STATE)
            _, previous = source.verify_prior_state(helper, ready.PRIOR_STATE)
            fingerprints.update(previous)
            prep.verify_timer_hold()
            source.ensure_stopped(ready.UNITS, helper.observe_unit)
            hold.verify_held_cron(helper)
            context = context_module.inspect_context(user=user, target=ready.TARGET)
            directory = Path(tempfile.mkdtemp(prefix='kadima-stopped-history-source-', dir='/opt'))
            helper.private_path(directory, directory=True)
            helper.root_parents(directory)
            release = directory/'release'
            helper.stage_release(helper.canonical(args.history), release, source.RELEASE)
            installer = source.load_helper('_stopped_history_installer', release/'trade_audit_update/install.py', source.INSTALLER_BLOB)
            base = installer.base
            fingerprints.update(ready.verify_sources(installer, release, ready.TARGET,
                PACKAGE/'momentum_recovery/reviewed_dashboard', (user.pw_uid, user.pw_gid), source))
            require(ready.blob(base.read_file(ready.TARGET/'kadima/config.py')) == ready.CONFIG_BLOB, 'installed_configuration_changed')
            raw = ready.private_evidence(args.evidence, helper, user.pw_uid)
            require(hashlib.sha256(raw).hexdigest() == EVIDENCE_SHA256, 'exact_reviewed_evidence_required')
            evidence = note.evidence(json.loads(raw), utc_now())
            note.account_matches(evidence, context.env)
            # These are the reviewed observations, not a claim of current
            # state. The transaction independently checks every field under
            # closed SQLite connections after source/service admission.
            expected_local = dict(database_identity=DB_IDENTITY, historical_rows_sha256=ROWS_SHA256,
                unresolved_positions=1, unresolved_orders=1, persisted_pause_verified=True,
                reviewed_historical_case_matches=True, controls=dict(auto_trade_enabled=False,
                    kill_latched=False, stock_live_enabled=False, stock_revision=14))
            def guard():
                context.guard()
                prep.verify_timer_hold()
                source.ensure_stopped(ready.UNITS, helper.observe_unit)
                hold.verify_held_cron(helper)
                triggers = ready.watchdog_triggers(helper)
                require(not triggers['references'] and not triggers['running_pids'] and not triggers['inspection_errors']
                        and all(value.get('ActiveState') == 'inactive' for value in triggers['timers'].values()),
                        'watchdog_triggers_must_remain_held')
                base.check_fingerprints(fingerprints)
                require(ready.private_evidence(args.evidence, helper, user.pw_uid) == raw, 'reviewed_evidence_changed')
            if args.apply:
                helper.root_parents(STATE)
                STATE.mkdir(mode=0o700, exist_ok=True)
                helper.private_path(STATE, directory=True)
                require(not (STATE/'attempted.json').exists(), 'historical_repair_attempt_requires_review')
            report = repair_transaction(context.db_path, note, evidence, expected_local,
                lambda: broker_module.read_broker(context.python, context.env, user, ready.TARGET, evidence),
                guard, Path('/var/backups'), validate_broker=broker_module.validate_receipt, apply=args.apply,
                claim=lambda: helper.claim_attempt(STATE, utc_now()),
                record=(lambda value: helper.record_status(STATE, value)) if args.apply else (lambda value: None))
    except Exception as exc:
        report.update(getattr(exc, 'report', {}))
        if not str(report.get('state', '')).startswith('blocked'):
            report['state'] = 'blocked'
        report['blocked'] = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    print('Historical ledger repair committed; all services remain stopped.' if report['ledger_repaired']
          else 'Plan only; no ledger repair or service start performed.')


if __name__ == '__main__':
    main()
