"""Replay the NIFTY day forecast, current (09:15 anchor) vs 13-Nov (09:20 official anchor + fast rule).

Run on EC2 AFTER 15:45 IST, from a scratch copy of the live code (see run_day_anchor_replay_20261113.sh):
  python -I replay_day_anchor_20261113.py --code DIR --arm old|new --scratch DIR --out FILE.json

Safety: kadima.db is opened read-only (SQLite mode=ro); forecast.db is copied with the online
backup API and only the copy is written; audit.log is replaced by a no-op so nothing reaches
the live audit trail. No engine, feed, order, login or broker code is imported.

Same inputs for both arms: the live 09:15 anchor price from the stored day_forecasts row, no live
IV/Shield (not stored per minute), the current model weights. So old vs new differ only by the
anchor change and the fast reforecast rule.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument('--code', required=True)
ap.add_argument('--arm', choices=('old', 'new'), required=True)
ap.add_argument('--scratch', required=True)
ap.add_argument('--out', required=True)
ap.add_argument('--since', default='2026-10-23')
ap.add_argument('--index', default='NIFTY')
a = ap.parse_args()
sys.path.insert(0, a.code)

from kadima import config  # noqa: E402
from kadima import audit, storage  # noqa: E402

audit.log = lambda *x, **k: None                      # never touch the live audit trail

_ro = {}


def _ro_conn():
    if 'c' not in _ro:
        c = sqlite3.connect(f'file:{config.DB_PATH}?mode=ro', uri=True, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _ro['c'] = c
    return _ro['c']


storage.conn = _ro_conn

from kadima import hourly_forecast as HF  # noqa: E402

live_fc = os.path.join(config.DATA_DIR, 'forecast.db')
scratch = os.path.join(a.scratch, f'forecast_{a.arm}.db')
src = sqlite3.connect(f'file:{live_fc}?mode=ro', uri=True)
src.row_factory = sqlite3.Row
live_rows = {r['day']: dict(r) for r in src.execute(
    'SELECT * FROM day_forecasts WHERE index_name=? AND day>=? ORDER BY day', (a.index, a.since))}
dst = sqlite3.connect(scratch)
src.backup(dst)
src.close()
for t in ('day_forecasts', 'day_forecast_points', 'day_forecast_revisions'):
    dst.execute(f'DELETE FROM {t} WHERE index_name=? AND day>=?', (a.index, a.since))
dst.commit()
dst.close()
HF.DB_PATH = scratch

from kadima import day_forecast as DF  # noqa: E402

token = config.INDICES[a.index].token


class FakeHourly:
    db = scratch

    def __init__(self):
        self.spot = None
        self.t0 = None

    def _live_inputs(self, st, now):
        if self.t0 is not None and abs((now - self.t0).total_seconds()) <= DF.ON_TIME_SEC:
            return self.spot, None, None, None
        return None, None, None, None


res = []
for d_s, live in live_rows.items():
    day = date.fromisoformat(d_s)
    hourly = FakeHourly()
    hourly.t0, hourly.spot = DF.t0_of(day), live['anchor']
    fc = DF.DayForecaster(hourly)
    c = DF.conn(scratch)
    marks = [DF.ts_of(day, k) + timedelta(seconds=10) for k in range(DF.STEPS + 1)]
    marks[0] = DF.t0_of(day) + timedelta(seconds=5)
    marks += [DF.ts_of(day, DF.STEPS) + timedelta(seconds=DF.GIVE_UP_SEC + 5)]
    err = None
    try:
        for now in marks:
            for _ in range(6):                      # the live loop runs every few seconds; catch up
                before = (len(DF.revisions(d_s, a.index, scratch)),
                          c.execute('SELECT inputs, status FROM day_forecasts WHERE day=? AND index_name=?',
                                    (d_s, a.index)).fetchone())
                before = (before[0], tuple(before[1]) if before[1] else None)
                fc.tick(a.index, object(), now, token)
                after = (len(DF.revisions(d_s, a.index, scratch)),
                         c.execute('SELECT inputs, status FROM day_forecasts WHERE day=? AND index_name=?',
                                   (d_s, a.index)).fetchone())
                after = (after[0], tuple(after[1]) if after[1] else None)
                if after == before:
                    break
    except Exception as e:                           # noqa: BLE001 - report, keep going
        err = repr(e)
    row = c.execute('SELECT * FROM day_forecasts WHERE day=? AND index_name=?', (d_s, a.index)).fetchone()
    pts = c.execute('SELECT k, forecast, actual, actual_source FROM day_forecast_points WHERE day=? AND '
                    'index_name=? ORDER BY k', (d_s, a.index)).fetchall()
    revs = DF.revisions(d_s, a.index, scratch)
    common = [abs(p['actual'] - p['forecast']) for p in pts if p['k'] >= 2 and HF.verified(p)]
    kinds = {}
    for v in revs:
        kinds[v['kind']] = kinds.get(v['kind'], 0) + 1
    out = dict(day=d_s, arm=a.arm, error=err,
               live=dict(anchor=live['anchor'], direction=live['direction'], dir_hit=live['dir_hit'],
                         end_error_pts=live['end_error_pts'], mae_pts=live['mae_pts'], status=live['status']))
    if row is not None:
        try:
            inp = json.loads(row['inputs'] or '{}')
        except ValueError:
            inp = {}
        out.update(status=row['status'], anchor=row['anchor'], direction=row['direction'],
                   projected_pts=row['projected_pts'], dir_hit=row['dir_hit'], mae_pts=row['mae_pts'],
                   end_error_pts=row['end_error_pts'], coverage=row['coverage'], actual_end=row['actual_end'],
                   official=inp.get('official'), revisions=len(revs), kinds=kinds,
                   mae_common_k2=round(sum(common) / len(common), 2) if common else None)
    res.append(out)
    print(json.dumps(out), flush=True)
json.dump(res, open(a.out, 'w'), indent=1)
