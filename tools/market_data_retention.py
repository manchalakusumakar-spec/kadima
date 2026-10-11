"""Market-data retention; no engine/broker imports.

NIFTY / BANKNIFTY: seven calendar days (= the last five trading days, Mon-Fri),
deleted rows archived to verified gzip backups (unchanged behaviour).

All other indices and stocks (trader's decision, 11 Oct 2026): only the most
recent completed trading session is kept (always at least 24 hours), so the
engine still has the previous session's candles for indicator warm-up at the
open (on Monday that is Friday). These deletions are not archived, so cleanup
never refills the disk. The run stops cleanly if free disk drops below a floor.
"""
import argparse
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
SPOT_TOKENS = ('99926000', '99926009')
INDICES = ('NIFTY', 'BANKNIFTY')


def scope(connection):
    return {
        'ticks': "ts < ? AND (token IN ('99926000','99926009') "
                 "OR symbol IN ('NIFTY','BANKNIFTY') "
                 "OR symbol GLOB 'NIFTY[0-9]*' OR symbol GLOB 'BANKNIFTY[0-9]*')",
        'candles': "start_ts < ? AND token IN ('99926000','99926009')",
        'oi_snapshots': "ts < ? AND index_name IN ('NIFTY','BANKNIFTY')",
    }


def archive(rows, path):
    content = b''.join((json.dumps(dict(row), separators=(',', ':')) + '\n').encode()
                       for row in rows)
    with path.open('xb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1) as zipped:
            zipped.write(content)
        raw.flush()
        os.fsync(raw.fileno())
    with gzip.open(path, 'rb') as zipped:
        if hashlib.sha256(zipped.read()).digest() != hashlib.sha256(content).digest():
            raise RuntimeError('Deletion backup verification failed')


def prune(connection, cutoff, directory):
    connection.row_factory = sqlite3.Row
    predicates = scope(connection)
    counts = {}
    for table, predicate in predicates.items():
        count = 0
        last_rowid = -1
        while True:
            connection.execute('BEGIN IMMEDIATE')
            try:
                rows = connection.execute(
                    f'SELECT rowid AS retention_rowid,* FROM {table} '
                    f'WHERE rowid > ? AND {predicate} ORDER BY rowid LIMIT 5000',
                    (last_rowid, cutoff)).fetchall()
                if not rows:
                    connection.commit()
                    break
                archive(rows, directory / f'{table}-{count:012d}.jsonl.gz')
                connection.executemany(f'DELETE FROM {table} WHERE rowid=?',
                                       [(row['retention_rowid'],) for row in rows])
                connection.commit()
                count += len(rows)
                last_rowid = rows[-1]['retention_rowid']
            except Exception:
                connection.rollback()
                raise
        counts[table] = count
        print(f'{table}: deleted {count} old rows', flush=True)
    return counts


# ------------------------------------------------------------- other instruments
OTHER_MIN_AGE_HOURS = 24
FREE_SPACE_FLOOR_BYTES = 500 * 1024 * 1024
OTHER_BATCH = 20000
CHECKPOINT_EVERY = 20
_NIFTY_BANKNIFTY_TICK = ("(token IN ('99926000','99926009') "
                         "OR COALESCE(symbol,'') IN ('NIFTY','BANKNIFTY') "
                         "OR COALESCE(symbol,'') GLOB 'NIFTY[0-9]*' "
                         "OR COALESCE(symbol,'') GLOB 'BANKNIFTY[0-9]*')")


def other_scope():
    """Everything the NIFTY/BANKNIFTY scope above does not cover."""
    return {
        'ticks': f"ts < ? AND NOT {_NIFTY_BANKNIFTY_TICK}",
        'candles': "start_ts < ? AND token NOT IN ('99926000','99926009')",
        'oi_snapshots': "ts < ? AND index_name NOT IN ('NIFTY','BANKNIFTY')",
    }


def other_cutoff(connection, now):
    """Start (00:00 IST) of the latest trading day before today that has data,
    and never later than now minus 24 hours. Derived from stored candles, so
    weekends and holidays are handled without a calendar. None = keep everything."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = connection.execute('SELECT MAX(start_ts) FROM candles WHERE start_ts < ?',
                             (today.isoformat(),)).fetchone()
    if not row or not row[0]:
        return None
    last = datetime.fromisoformat(row[0]).astimezone(IST)
    session_start = last.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(session_start, now - timedelta(hours=OTHER_MIN_AGE_HOURS)).isoformat()


def free_bytes(path):
    return shutil.disk_usage(path).free


def prune_unarchived(connection, predicates, cutoff, db_path, free=free_bytes):
    """Batched delete without archive; checkpoints the WAL and stops cleanly
    (all committed batches stay consistent) if free disk falls below the floor."""
    counts = {}
    for table, predicate in predicates.items():
        count, batches, last_rowid = 0, 0, -1
        while True:
            if free(db_path.parent) < FREE_SPACE_FLOOR_BYTES:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                raise SystemExit(f'Stopped safely: free disk below {FREE_SPACE_FLOOR_BYTES >> 20} MB '
                                 f'({table}: {count} rows deleted so far)')
            connection.execute('BEGIN IMMEDIATE')
            try:
                ids = [r[0] for r in connection.execute(
                    f'SELECT rowid FROM {table} WHERE rowid > ? AND {predicate} '
                    f'ORDER BY rowid LIMIT {OTHER_BATCH}', (last_rowid, cutoff))]
                if not ids:
                    connection.commit()
                    break
                connection.executemany(f'DELETE FROM {table} WHERE rowid=?', [(i,) for i in ids])
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            count += len(ids)
            last_rowid = ids[-1]
            batches += 1
            if batches % CHECKPOINT_EVERY == 0:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        counts[table] = count
        print(f'{table}: deleted {count} other-instrument rows', flush=True)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/var/lib/kadima/kadima.db')
    parser.add_argument('--backup-root', default='/var/lib/kadima/retention_backups')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-others', action='store_true',
                        help='only the original NIFTY/BANKNIFTY seven-day cleanup')
    parser.add_argument('--now', help=argparse.SUPPRESS)   # tests only
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now).astimezone(IST) if args.now else datetime.now(IST)
    if args.apply and 9 <= now.hour < 16:
        raise SystemExit('Cleanup deferred: outside-market-hours execution required')
    cutoff = (now - timedelta(days=7)).isoformat()
    db = Path(args.db).resolve()
    connection = sqlite3.connect(db.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'),
                                 uri=True, timeout=30)
    connection.execute('PRAGMA temp_store=MEMORY')
    connection.execute('PRAGMA synchronous=FULL')
    print('Indices: NIFTY, BANKNIFTY; seven-calendar-day cutoff:', cutoff, flush=True)
    if not args.apply:
        for table, predicate in scope(connection).items():
            count = connection.execute(f'SELECT count(*) FROM {table} WHERE {predicate}',
                                       (cutoff,)).fetchone()[0]
            print(f'{table}: would delete {count} rows', flush=True)
        others = None if args.skip_others else other_cutoff(connection, now)
        print('Other instruments; latest-session cutoff:', others or 'none (skipped)', flush=True)
        if others:
            for table, predicate in other_scope().items():
                count = connection.execute(f'SELECT count(*) FROM {table} WHERE {predicate}',
                                           (others,)).fetchone()[0]
                print(f'{table}: would delete {count} other-instrument rows', flush=True)
    else:
        directory = Path(args.backup_root) / ('run-' + now.strftime('%Y%m%dT%H%M%S%f'))
        directory.mkdir(parents=True, mode=0o700)
        counts = prune(connection, cutoff, directory)
        others = None if args.skip_others else other_cutoff(connection, now)
        print('Other instruments; latest-session cutoff:', others or 'none (skipped)', flush=True)
        other_counts = (prune_unarchived(connection, other_scope(), others, db)
                        if others else {})
        (directory / 'receipt.json').write_text(json.dumps(
            {'cutoff': cutoff, 'completed_at': now.isoformat(),
             'indices': INDICES, 'deleted': counts,
             'other_cutoff': others, 'other_deleted_unarchived': other_counts}, indent=2))
        # Only expire completed backups produced by this job, after seven days.
        for older in directory.parent.glob('run-*'):
            receipt = older / 'receipt.json'
            if older.is_symlink() or not receipt.is_file():
                continue
            metadata = json.loads(receipt.read_text())
            completed = metadata.get('completed_at')
            if (metadata.get('indices') == list(INDICES) and completed
                    and datetime.fromisoformat(completed) < now - timedelta(days=7)):
                shutil.rmtree(older)
        print('Verified deletion backups:', directory, flush=True)
    connection.close()


if __name__ == '__main__':
    main()
