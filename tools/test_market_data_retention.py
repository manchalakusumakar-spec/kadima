"""Offline tests for market_data_retention.py and kadima_db_compact.py.

Uses a scratch database with the live table layout; never touches /var/lib/kadima.
Run:  python3 tools/test_market_data_retention.py
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
RET = HERE / 'market_data_retention.py'
FAILED = []
SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, token TEXT NOT NULL,
    symbol TEXT, ltp REAL, volume INTEGER, bid REAL, ask REAL);
CREATE INDEX idx_ticks_token_ts ON ticks(token, ts);
CREATE TABLE candles (token TEXT NOT NULL, interval INTEGER NOT NULL, start_ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER, vwap REAL, closed INTEGER DEFAULT 0,
    PRIMARY KEY (token, interval, start_ts));
CREATE TABLE oi_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, index_name TEXT NOT NULL,
    token TEXT NOT NULL, symbol TEXT, strike REAL, opt_type TEXT, ltp REAL, oi INTEGER, oi_change INTEGER,
    volume INTEGER, iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL);
CREATE TABLE signals (id INTEGER PRIMARY KEY, ts TEXT);
'''
NIFTY, BANK, SENSEX, MIDCP, HDFC = '99926000', '99926009', '99919000', '99926074', '1333'
# Trading days in the fixture: Mon 5 Oct .. Fri 9 Oct, plus older Thu 1 Oct; weekend 10-11 Oct.
DAYS = ['2026-10-01', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09']


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def build(path, extra_days=()):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for day in list(DAYS) + list(extra_days):
        for hh in ('09:20', '15:30'):
            ts = f'{day}T{hh}:00+05:30'
            for token, sym in ((NIFTY, 'NIFTY'), (BANK, 'BANKNIFTY'), (SENSEX, 'SENSEX'),
                               (MIDCP, 'MIDCPNIFTY'), (HDFC, None)):
                c.execute('INSERT INTO ticks (ts, token, symbol, ltp) VALUES (?,?,?,1)', (ts, token, sym))
                c.execute('INSERT INTO candles (token, interval, start_ts, close, closed) VALUES (?,5,?,1,1)',
                          (token, ts))
            c.execute("INSERT INTO ticks (ts, token, symbol, ltp) VALUES (?, '55555', 'NIFTY14OCT2625000CE', 1)", (ts,))
            c.execute("INSERT INTO ticks (ts, token, symbol, ltp) VALUES (?, '66666', 'NIFTYNXT5014OCT2670000CE', 1)", (ts,))
            for idx in ('NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY'):
                c.execute('INSERT INTO oi_snapshots (ts, index_name, token, ltp) VALUES (?,?,?,1)', (ts, idx, 't'))
    c.commit()
    c.close()


def days_left(path, sql, args=()):
    c = sqlite3.connect(path)
    rows = {r[0][:10] for r in c.execute(sql, args)}
    c.close()
    return sorted(rows)


def run(path, now, *extra):
    backups = Path(path).parent / 'backups'
    r = subprocess.run([sys.executable, str(RET), '--db', str(path), '--backup-root', str(backups),
                        '--now', now, *extra], capture_output=True, text=True, timeout=120)
    return r


def test_dry_run_changes_nothing():
    print('dry run reports and changes nothing')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    before = days_left(db, 'SELECT ts FROM ticks')
    r = run(db, '2026-10-11T18:30:00+05:30')
    check('exit 0', r.returncode == 0)
    check('reports other-instrument counts', 'other-instrument rows' in r.stdout)
    check('nothing deleted', days_left(db, 'SELECT ts FROM ticks') == before)


def test_sunday_evening_run():
    print('Sunday evening run: others keep Friday; NIFTY/BANKNIFTY keep seven days')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    r = run(db, '2026-10-11T18:30:00+05:30', '--apply')
    check('exit 0', r.returncode == 0)
    for token, name in ((SENSEX, 'SENSEX'), (MIDCP, 'MIDCPNIFTY'), (HDFC, 'HDFC (NULL symbol)')):
        check(f'{name} ticks: only Fri 9 Oct kept',
              days_left(db, 'SELECT ts FROM ticks WHERE token=?', (token,)) == ['2026-10-09'])
        check(f'{name} candles: only Fri 9 Oct kept',
              days_left(db, 'SELECT start_ts FROM candles WHERE token=?', (token,)) == ['2026-10-09'])
    check('NIFTYNXT50 option ticks are "others" (not NIFTY)',
          days_left(db, "SELECT ts FROM ticks WHERE token='66666'") == ['2026-10-09'])
    check('SENSEX / FINNIFTY OI: only Fri kept',
          days_left(db, "SELECT ts FROM oi_snapshots WHERE index_name IN ('SENSEX','FINNIFTY')") == ['2026-10-09'])
    week = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09']
    for token, name in ((NIFTY, 'NIFTY'), (BANK, 'BANKNIFTY')):
        check(f'{name} spot ticks: Mon-Fri kept, older removed',
              days_left(db, 'SELECT ts FROM ticks WHERE token=?', (token,)) == week)
        check(f'{name} candles: Mon-Fri kept',
              days_left(db, 'SELECT start_ts FROM candles WHERE token=?', (token,)) == week)
    check('NIFTY option ticks follow the NIFTY rule (Mon-Fri)',
          days_left(db, "SELECT ts FROM ticks WHERE token='55555'") == week)
    check('NIFTY / BANKNIFTY OI: Mon-Fri kept',
          days_left(db, "SELECT ts FROM oi_snapshots WHERE index_name IN ('NIFTY','BANKNIFTY')") == week)
    run_dir = sorted((Path(d) / 'backups').glob('run-*'))[-1]
    receipt = json.loads((run_dir / 'receipt.json').read_text())
    check('receipt records both cutoffs and counts',
          receipt['other_cutoff'].startswith('2026-10-09T00:00') and receipt['other_deleted_unarchived']['ticks'] > 0)
    check('NIFTY/BANKNIFTY deletions still archived (gzip backups written)',
          any(run_dir.glob('ticks-*.jsonl.gz')))
    import gzip
    archived = [json.loads(line) for f in run_dir.glob('*.jsonl.gz')
                for line in gzip.open(f).read().decode().splitlines()]
    check('archive holds only NIFTY/BANKNIFTY-family rows (others not archived)',
          archived and all(r.get('token') in (NIFTY, BANK, '55555', 't') and
                           r.get('index_name', 'NIFTY') in ('NIFTY', 'BANKNIFTY') for r in archived))
    check('archived NIFTY-family ticks = 1 Oct x 2 times x 3 tokens', receipt['deleted']['ticks'] == 6)


def test_monday_0230_keeps_friday():
    print('Monday 02:30 run keeps Friday for warm-up at the open')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    r = run(db, '2026-10-12T02:30:00+05:30', '--apply')
    check('exit 0', r.returncode == 0)
    check('SENSEX candles still have Friday', days_left(db, 'SELECT start_ts FROM candles WHERE token=?',
                                                        (SENSEX,)) == ['2026-10-09'])


def test_tuesday_0230_keeps_monday():
    print('Tuesday 02:30 run keeps Monday (normal 24-hour case)')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db, extra_days=['2026-10-12'])
    r = run(db, '2026-10-13T02:30:00+05:30', '--apply')
    check('exit 0', r.returncode == 0)
    check('SENSEX ticks: only Mon 12 Oct kept',
          days_left(db, 'SELECT ts FROM ticks WHERE token=?', (SENSEX,)) == ['2026-10-12'])


def test_market_hours_refused():
    print('apply refused during market hours')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    r = run(db, '2026-10-12T10:00:00+05:30', '--apply')
    check('refused, nothing deleted', r.returncode != 0
          and days_left(db, 'SELECT ts FROM ticks WHERE token=?', (SENSEX,)) == DAYS)


def test_skip_others():
    print('--skip-others keeps the original behaviour only')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    r = run(db, '2026-10-11T18:30:00+05:30', '--apply', '--skip-others')
    check('exit 0', r.returncode == 0)
    check('SENSEX untouched', days_left(db, 'SELECT ts FROM ticks WHERE token=?', (SENSEX,)) == DAYS)


def test_no_candles_skips_others():
    print('no candles -> others skipped (fail safe)')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    c = sqlite3.connect(db)
    c.execute('DELETE FROM candles')
    c.commit()
    c.close()
    r = run(db, '2026-10-11T18:30:00+05:30', '--apply')
    check('exit 0 and SENSEX ticks untouched', r.returncode == 0
          and days_left(db, 'SELECT ts FROM ticks WHERE token=?', (SENSEX,)) == DAYS)


def test_low_disk_stops_cleanly():
    print('low free disk stops cleanly before deleting')
    spec = importlib.util.spec_from_file_location('ret', RET)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db)
    c = sqlite3.connect(db)
    try:
        m.prune_unarchived(c, m.other_scope(), '2026-10-09T00:00:00+05:30', db, free=lambda p: 10)
        check('raised', False)
    except SystemExit as exc:
        check('stops with a clear message', 'free disk' in str(exc))
    c.close()
    check('nothing deleted', days_left(db, 'SELECT ts FROM ticks WHERE token=?', (SENSEX,)) == DAYS)


def test_compact():
    print('compaction tool')
    spec = importlib.util.spec_from_file_location('compact', HERE / 'kadima_db_compact.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo('Asia/Kolkata')
    d = tempfile.mkdtemp()
    db = Path(d) / 'k.db'
    build(db, extra_days=[f'2026-09-{n:02d}' for n in range(1, 29)])
    c = sqlite3.connect(db)
    c.execute("INSERT INTO ticks (ts, token, symbol) SELECT ts, token, symbol FROM ticks")
    for _ in range(6):
        c.execute("INSERT INTO ticks (ts, token, symbol, ltp) SELECT ts, token, symbol, ltp FROM ticks")
    c.commit()
    c.execute("DELETE FROM ticks WHERE token != ?", (NIFTY,))
    c.commit()
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    c.close()
    before = db.stat().st_size
    sunday = datetime(2026, 10, 11, 19, tzinfo=ist)
    check('report-only changes nothing', m.main(['--db', str(db)], now=sunday) == 0
          and db.stat().st_size == before)
    try:
        m.main(['--db', str(db), '--apply'], now=datetime(2026, 10, 12, 11, tzinfo=ist))
        check('market hours refused', False)
    except SystemExit as exc:
        check('market hours refused', 'market hours' in str(exc))
    try:
        m.main(['--db', str(db), '--apply'], now=sunday, free=1)
        check('low disk refused', False)
    except SystemExit as exc:
        check('low disk refused', 'free disk' in str(exc))
    for label, hold in (('reading', True), ('idle', False)):
        holder = sqlite3.connect(db)
        if hold:
            holder.execute('BEGIN')
        holder.execute('SELECT COUNT(*) FROM ticks').fetchone()
        try:
            m.main(['--db', str(db), '--apply'], now=sunday)
            check(f'refuses while another connection is open ({label})', False)
        except SystemExit as exc:
            check(f'refuses while another connection is open ({label})', 'Stop the KADIMA services' in str(exc))
        if hold:
            holder.rollback()
        holder.close()
    check('size unchanged after refusals', db.stat().st_size == before)
    def count():
        k = sqlite3.connect(db)
        try:
            return k.execute('SELECT COUNT(*) FROM ticks').fetchone()[0]
        finally:
            k.close()
    nifty_before = count()
    check('apply compacts', m.main(['--db', str(db), '--apply'], now=sunday) == 0)
    check('file is smaller', db.stat().st_size < before)
    check('no rows lost', count() == nifty_before)


if __name__ == '__main__':
    for t in [v for k, v in list(globals().items()) if k.startswith('test_')]:
        t()
    print('-' * 40)
    print('ALL PASS' if not FAILED else f'{len(FAILED)} FAILED: {FAILED}')
    sys.exit(1 if FAILED else 0)
