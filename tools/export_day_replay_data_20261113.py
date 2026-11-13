"""Read-only export of what the day-forecast replay needs (13-Nov anchor change).

Run on EC2 from ~/kadima-py:  nice -n 19 venv/bin/python -I tools/export_day_replay_data_20261113.py OUTDIR
Opens kadima.db read-only, copies forecast.db with SQLite's online backup, writes only to OUTDIR.
No engine import beyond kadima.config (paths and index tokens). No trading, no writes to live data.
"""
import json
import os
import sqlite3
import sys
import tarfile

sys.path.insert(0, os.getcwd())
from kadima import config  # noqa: E402

SINCE = '2026-10-01'
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
idx = config.INDICES['NIFTY']
token = str(idx.token)

src = sqlite3.connect(f'file:{os.path.join(config.DATA_DIR, "forecast.db")}?mode=ro', uri=True)
dst = sqlite3.connect(os.path.join(out, 'forecast.db'))
src.backup(dst)
dst.close()
src.close()

k = sqlite3.connect(f'file:{config.DB_PATH}?mode=ro', uri=True)
o = sqlite3.connect(os.path.join(out, 'kadima_subset.db'))
for table, where, args in (('candles', 'token=? AND interval IN (1,5) AND start_ts>=?', (token, SINCE)),
                           ('oi_snapshots', 'index_name=? AND ts>=?', ('NIFTY', SINCE))):
    ddl = k.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not ddl:
        continue
    o.execute(ddl[0])
    cols = [r[1] for r in k.execute(f'PRAGMA table_info({table})')]
    cur = k.execute(f'SELECT {",".join(cols)} FROM {table} WHERE {where}', args)
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        o.executemany(f'INSERT INTO {table} VALUES ({",".join("?" * len(cols))})', rows)
    o.commit()
o.close()
k.close()

# Only the forecast code path (no config/session/credentials).
FILES = ('day_forecast.py', 'hourly_forecast.py', 'hourly_forecast_model.py', 'forecast_chain_data.py',
         'storage.py', 'audit.py', 'utils.py')
with tarfile.open(os.path.join(out, 'code.tar.gz'), 'w:gz') as t:
    for f in FILES:
        if os.path.exists(os.path.join('kadima', f)):
            t.add(os.path.join('kadima', f))
json.dump(dict(token=token, strike_step=getattr(idx, 'strike_step', 50), since=SINCE,
               data_dir=config.DATA_DIR), open(os.path.join(out, 'meta.json'), 'w'))
print('export ok', {f: os.path.getsize(os.path.join(out, f)) for f in os.listdir(out)})
