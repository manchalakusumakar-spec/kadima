"""Offline tests for the 13-Nov day-forecast anchor change. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_day_anchor_20261113.py

09:15 path is provisional; the verified 09:20 close re-anchors the official path (scored from
09:25, direction from the 09:20 close); 09:15 copy kept. Until 10:15 one candle at least
max(30, half the 09:15 range) off reforecasts at once. Late start / unverified 09:20 keep 09:15.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
sys.path.insert(0, 'tools')
from kadima import hourly_forecast as HF, storage                      # noqa: E402
from kadima import day_forecast as DF                                  # noqa: E402
from kadima.config import IST                                         # noqa: E402
from test_hourly_forecast import synthetic                            # noqa: E402

FAILS = []
TOKEN = '99926000'


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def put(start, o, h, lo, c, closed=True):
    storage.upsert_candle(TOKEN, 5, start.replace(tzinfo=IST).isoformat(), o, h, lo, c, 0, c, closed)


ROWS = synthetic(n_days=41)
DAYS = sorted({r[0].date() for r in ROWS})


def setup(today, name):
    for r in ROWS:
        if r[0].date() < today:
            put(*r)
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], f'{name}.db')
    hf = HF.HourlyForecaster(None, db=db)
    return hf.day, db, DF.conn(db)


def bar(today, k, o, h, lo, c, closed=True):
    put(DF.ts_of(today, k - 1).replace(tzinfo=None), o, h, lo, c, closed)


def main():
    # ---- day A: opening rally inside the first candle
    today = DAYS[-1]
    dfc, db, c = setup(today, 'a')
    o = 25000.0
    bar(today, 1, o, o + 3, o - 3, o, closed=False)
    dfc.tick('NIFTY', None, DF.t0_of(today) + timedelta(seconds=20))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    check('09:15 path made', row is not None and row['anchor'] == o)
    v = DF.view('NIFTY', today, DF.t0_of(today) + timedelta(seconds=60), db=db)
    check('09:15 path shown as provisional', v.get('provisional') is True and v.get('official') is None)
    prov_pts = [tuple(p) for p in c.execute('SELECT k, forecast FROM day_forecast_points ORDER BY k')]

    # 09:20 not closed yet: nothing decided, no reforecast
    dfc.tick('NIFTY', None, DF.ts_of(today, 1) + timedelta(seconds=2))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    check('waits for the 09:20 close', DF.official_state(row) is None)

    # first candle rallies 130 pts and closes there
    bar(today, 1, o, o + 135, o - 5, o + 130)
    dfc.tick('NIFTY', None, DF.ts_of(today, 1) + timedelta(seconds=10))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    inp = json.loads(row['inputs'])
    check('official path anchored on the 09:20 close', inp.get('official') == 'official_0920'
          and abs(row['anchor'] - (o + 130)) < 1e-6 and inp.get('anchor_k') == 1)
    check('09:15 anchor kept for review', abs(inp.get('provisional_anchor') - o) < 1e-6)
    pv = c.execute('SELECT * FROM day_forecast_provisional').fetchone()
    check('09:15 path copied to day_forecast_provisional', pv is not None and len(json.loads(pv['points'])) == 75
          and [(q['k'], q['forecast']) for q in json.loads(pv['points'])] == prov_pts)
    pts = {p['k']: p for p in c.execute('SELECT * FROM day_forecast_points ORDER BY k')}
    check('k=1 point equals the 09:20 close', abs(pts[1]['forecast'] - (o + 130)) < 0.01)
    check('path rebuilt from 09:20 (k=2 near the new anchor)', abs(pts[2]['forecast'] - (o + 130)) < 40)
    check('no reforecast at the re-anchor', not DF.revisions(today.isoformat(), 'NIFTY', db))
    v = DF.view('NIFTY', today, DF.ts_of(today, 1) + timedelta(seconds=20), db=db)
    check('view: official, not provisional, 09:15 copy attached', v['official'] == 'official_0920'
          and v['provisional'] is False and v['provisional_0915'] and v['anchor_k'] == 1)

    # fast threshold: max(30, half of 140 range) = 70
    thr = DF.fast_threshold(TOKEN, today)
    check('fast threshold = max(30, half the 09:15 range)', abs(thr - 70.0) < 0.01)
    # 09:25 candle 50 off: below fast limit, and only one candle -> nothing
    bar(today, 2, pts[2]['forecast'], pts[2]['forecast'] + 52, pts[2]['forecast'], pts[2]['forecast'] + 50)
    dfc.tick('NIFTY', None, DF.ts_of(today, 2) + timedelta(seconds=10))
    check('50 pts < fast limit 70: no fast reforecast on one candle', not DF.revisions(today.isoformat(), 'NIFTY', db))
    # 09:30 candle back on path -> nothing
    bar(today, 3, pts[3]['forecast'], pts[3]['forecast'] + 2, pts[3]['forecast'] - 2, pts[3]['forecast'])
    dfc.tick('NIFTY', None, DF.ts_of(today, 3) + timedelta(seconds=10))
    check('back on the path: nothing', not DF.revisions(today.isoformat(), 'NIFTY', db))
    # 09:35 candle 90 below -> fast reforecast at once
    f4 = pts[4]['forecast']
    bar(today, 4, f4, f4 + 2, f4 - 95, f4 - 90)
    dfc.tick('NIFTY', None, DF.ts_of(today, 4) + timedelta(seconds=10))
    revs = DF.revisions(today.isoformat(), 'NIFTY', db)
    check('one candle beyond the fast limit reforecasts at once', len(revs) == 1 and revs[0]['kind'] == 'fast'
          and revs[0]['trigger_k'] == 4 and abs(revs[0]['threshold_pts'] - 70) < 0.01)

    # after 10:15 the fast rule is off: a single 90-pt candle at 10:20 does not reforecast on its own
    act = DF.active_path(list(c.execute('SELECT k, forecast FROM day_forecast_points')), revs)
    for k in range(5, 13):
        bar(today, k, act[k], act[k] + 2, act[k] - 2, act[k])
        dfc.tick('NIFTY', None, DF.ts_of(today, k) + timedelta(seconds=10))
    revs = DF.revisions(today.isoformat(), 'NIFTY', db)
    check('10:15 milestone still reforecasts', revs[-1]['kind'] == 'milestone' and revs[-1]['trigger_k'] == 12)
    act = DF.active_path(list(c.execute('SELECT k, forecast FROM day_forecast_points')), revs)
    bar(today, 13, act[13], act[13] + 92, act[13], act[13] + 90)
    dfc.tick('NIFTY', None, DF.ts_of(today, 13) + timedelta(seconds=10))
    check('after 10:15 one candle does not reforecast (2-candle rule only)',
          len(DF.revisions(today.isoformat(), 'NIFTY', db)) == len(revs))

    # rest of day on the official path, then score
    pts = {p['k']: p for p in c.execute('SELECT * FROM day_forecast_points ORDER BY k')}
    for k in range(14, 76):
        bar(today, k, pts[k]['forecast'], pts[k]['forecast'] + 2, pts[k]['forecast'] - 2, pts[k]['forecast'])
        dfc.tick('NIFTY', None, DF.ts_of(today, k) + timedelta(seconds=10))
    for _ in range(10):
        dfc.tick('NIFTY', None, DF.ts_of(today, 75) + timedelta(seconds=60))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    rs = json.loads(row['reasons'])
    check('scored at the close from 09:25 (74 points, anchor_k 1)', row['status'] == 'final'
          and rs['scored_points'] == 74 and rs['anchor_k'] == 1)
    check('direction measured from the 09:20 close', abs(row['actual_pct'] - ((row['actual_end'] / (o + 130) - 1) * 100)) < 0.01)

    # ---- day B: engine starts at 10:00 -> 09:15 (late) path stays official
    today = DAYS[-2]
    dfc, db, c = setup(today, 'b')
    for k in range(1, 10):
        bar(today, k, o, o + 3, o - 3, o)
    dfc.tick('NIFTY', None, DF.ts_of(today, 9) + timedelta(seconds=10))
    dfc.tick('NIFTY', None, DF.ts_of(today, 9) + timedelta(seconds=11))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    inp = json.loads(row['inputs'])
    check('late start keeps the 09:15 path', inp.get('official') == 'kept_0915' and inp.get('anchor_k') == 0)

    # ---- day C: 09:20 never verified by 09:25 -> keep 09:15
    today = DAYS[-3]
    dfc, db, c = setup(today, 'c')
    bar(today, 1, o, o + 3, o - 3, o, closed=False)
    dfc.tick('NIFTY', None, DF.t0_of(today) + timedelta(seconds=20))
    dfc.tick('NIFTY', None, DF.ts_of(today, 1) + timedelta(seconds=120))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    check('still waiting at 09:22 when 09:20 is unverified', DF.official_state(row) is None)
    dfc.tick('NIFTY', None, DF.ts_of(today, 1) + timedelta(seconds=301))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    check('09:20 unverified by 09:25: keeps the 09:15 path', DF.official_state(row) == 'kept_0915')

    # ---- switch off: 05-Nov behaviour
    DF.ANCHOR_0920 = False
    today = DAYS[-4]
    dfc, db, c = setup(today, 'd')
    bar(today, 1, o, o + 3, o - 3, o, closed=False)
    dfc.tick('NIFTY', None, DF.t0_of(today) + timedelta(seconds=20))
    a0 = c.execute('SELECT anchor FROM day_forecasts').fetchone()['anchor']
    bar(today, 1, o, o + 135, o - 5, o + 130)
    dfc.tick('NIFTY', None, DF.ts_of(today, 1) + timedelta(seconds=10))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    check('switch off: 09:15 path untouched', row['anchor'] == a0 and DF.official_state(row) is None)
    DF.ANCHOR_0920 = True

    print(f'\n{len(FAILS)} failure(s)')
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
