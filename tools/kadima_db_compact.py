"""One-time compaction of kadima.db (SQLite VACUUM). No engine/broker imports.

Deleting rows only frees pages inside the file; VACUUM rewrites the file so the
disk space is actually returned. Run it outside market hours with the KADIMA
services that use the database stopped.

    python3 tools/kadima_db_compact.py            # report only, changes nothing
    python3 tools/kadima_db_compact.py --apply    # compact (services stopped)

Refuses to run on a weekday between 09:00 and 16:00 IST, when free disk is
below about twice the size of the live data, or when another connection still
holds the database. Never deletes rows itself.
"""
import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
MARGIN_BYTES = 300 * 1024 * 1024


def mb(n):
    return f'{n / 1024 / 1024:,.0f} MB'


def sizes(db):
    wal = Path(str(db) + '-wal')
    return db.stat().st_size, wal.stat().st_size if wal.exists() else 0


def plan(connection):
    page = connection.execute('PRAGMA page_size').fetchone()[0]
    pages = connection.execute('PRAGMA page_count').fetchone()[0]
    free_pages = connection.execute('PRAGMA freelist_count').fetchone()[0]
    return page * (pages - free_pages), page * free_pages


def main(argv=None, now=None, free=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/var/lib/kadima/kadima.db')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    now = now or datetime.now(IST)
    db = Path(args.db).resolve()
    file_size, wal_size = sizes(db)
    connection = sqlite3.connect(db.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'),
                                 uri=True, timeout=5)
    try:
        return _run(args, now, free, db, file_size, wal_size, connection)
    finally:
        connection.close()


def _run(args, now, free, db, file_size, wal_size, connection):
    live, reclaimable = plan(connection)
    available = free if free is not None else shutil.disk_usage(db.parent).free
    needed = 2 * live + MARGIN_BYTES
    print(f'kadima.db {mb(file_size)} (+ WAL {mb(wal_size)}); live data {mb(live)}; '
          f'reclaimable {mb(reclaimable)}; free disk {mb(available)}; needed {mb(needed)}', flush=True)
    if not args.apply:
        print('Report only. Re-run with --apply after stopping the KADIMA services.')
        return 0
    if now.weekday() < 5 and 9 <= now.hour < 16:
        raise SystemExit('Refused: weekday market hours (09:00-16:00 IST)')
    if available < needed:
        raise SystemExit(f'Refused: free disk {mb(available)} is below the {mb(needed)} VACUUM needs. '
                         'Free space or enlarge the volume first.')
    try:
        # Exclusive locking mode fails if ANY other connection is open (even idle),
        # and then holds the database for this process until it closes.
        connection.execute('PRAGMA locking_mode=EXCLUSIVE')
        connection.execute('BEGIN EXCLUSIVE')
        connection.execute('COMMIT')
    except sqlite3.OperationalError:
        raise SystemExit('Refused: another process is using kadima.db. Stop the KADIMA services first.')
    busy = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]
    if busy:
        raise SystemExit('Refused: could not checkpoint the WAL. Stop the KADIMA services first.')
    try:
        connection.execute('VACUUM')
    except sqlite3.OperationalError as exc:
        raise SystemExit(f'VACUUM did not run ({exc}); nothing changed. Stop the KADIMA services and retry.')
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    ok = connection.execute('PRAGMA quick_check').fetchone()[0]
    connection.close()        # release the exclusive lock before measuring
    after, after_wal = sizes(db)
    print(f'Done: {mb(file_size + wal_size)} -> {mb(after + after_wal)}; integrity: {ok}', flush=True)
    return 0 if ok == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
