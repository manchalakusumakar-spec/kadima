"""Seven-day NIFTY/BANKNIFTY market-data retention; no engine/broker imports."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/var/lib/kadima/kadima.db')
    parser.add_argument('--backup-root', default='/var/lib/kadima/retention_backups')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    now = datetime.now(IST)
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
    else:
        directory = Path(args.backup_root) / ('run-' + now.strftime('%Y%m%dT%H%M%S%f'))
        directory.mkdir(parents=True, mode=0o700)
        counts = prune(connection, cutoff, directory)
        (directory / 'receipt.json').write_text(json.dumps(
            {'cutoff': cutoff, 'completed_at': now.isoformat(),
             'indices': INDICES, 'deleted': counts}, indent=2))
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
