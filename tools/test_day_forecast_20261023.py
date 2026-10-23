"""Offline tests for the day path forecast (23 Oct). No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_day_forecast_20261023.py

The 09:15 day path has 75 five-minute points to 15:30. Every hourly checkpoint publishes a
reforecast; between checkpoints a closed candle 10-20 points (one typical candle) off the path
in force publishes one. Provisional prices never trigger one. The original rows never change and
are the only thing scored. The hourly forecast reads the day path as one more small input.
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
from kadima import hourly_forecast_model as M                          # noqa: E402
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


def main():
    rows = synthetic(n_days=41)
    days = sorted({r[0].date() for r in rows})
    today = days[-1]
    for r in rows:
        if r[0].date() < today:
            put(*r)
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'day.db')
    hf = HF.HourlyForecaster(None, db=db)
    dfc = hf.day
    at = lambda h, m, s=0: datetime(today.year, today.month, today.day, h, m, s, tzinfo=IST)  # noqa: E731

    # 09:15 open bar starts; no engine -> rebuilt from the open, marked late.
    o = 25000.0
    put(datetime(today.year, today.month, today.day, 9, 15), o, o + 3, o - 3, o, closed=False)
    dfc.tick('NIFTY', None, at(9, 15, 20))
    c = DF.conn(db)
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    pts = c.execute('SELECT * FROM day_forecast_points ORDER BY k').fetchall()
    check('day forecast made at 09:15', row is not None and row['anchor'] == o)
    check('75 five-minute points 09:20 .. 15:30', len(pts) == 75 and pts[0]['ts'].endswith('09:20:00+05:30')
          and pts[-1]['ts'].endswith('15:30:00+05:30'))
    check('expected swings stored', all(p['swing'] is not None for p in pts))
    original = [tuple(p) for p in c.execute('SELECT k, ts, forecast, upper, lower, swing FROM day_forecast_points ORDER BY k')]
    fc = {p['k']: p['forecast'] for p in pts}

    def bar(k, close, closed=True, rng=8.0):
        start = DF.ts_of(today, k - 1).replace(tzinfo=None)
        put(start, close - 1, close + rng / 2, close - rng / 2, close, closed)

    # Candles on the path (within 4 pts): no reforecast through 10:05.
    for k in range(1, 11):
        bar(k, fc[k] + (3 if k % 2 else -3))
        dfc.tick('NIFTY', None, DF.ts_of(today, k) + timedelta(seconds=10))
    check('no reforecast while candles stay on the path', not DF.revisions(today.isoformat(), 'NIFTY', db))
    thr = DF.threshold_pts(TOKEN, DF.ts_of(today, 11), db)
    check('threshold is one typical candle, held to 10..20', 10 <= thr <= 20)

    # Provisional (unclosed) bar far away: never triggers.
    bar(11, fc[11] + 40, closed=False)
    dfc.tick('NIFTY', None, DF.ts_of(today, 11) + timedelta(seconds=30))
    check('provisional price never triggers a reforecast', not DF.revisions(today.isoformat(), 'NIFTY', db))

    # It closes 40 pts off -> deviation reforecast from 10:10.
    bar(11, fc[11] + 40)
    dfc.tick('NIFTY', None, DF.ts_of(today, 11) + timedelta(seconds=40))
    revs = DF.revisions(today.isoformat(), 'NIFTY', db)
    check('closed candle beyond the threshold publishes a reforecast', len(revs) == 1 and revs[0]['kind'] == 'deviation')
    check('reforecast starts from that candle and runs to 15:30',
          revs and json.loads(revs[0]['points'])[0]['k'] == 12 and json.loads(revs[0]['points'])[-1]['k'] == 75)
    check('reforecast records deviation, limit and receipt',
          revs and abs(revs[0]['deviation_pts'] - 40) < 0.01 and revs[0]['threshold_pts'] >= 10 and len(revs[0]['receipt']) == 16)

    # 10:15 checkpoint: always reforecasts, even on the path.
    p1 = {q['k']: q['forecast'] for q in json.loads(revs[0]['points'])}
    bar(12, p1[12] + 1)
    dfc.tick('NIFTY', None, DF.ts_of(today, 12) + timedelta(seconds=20))
    revs = DF.revisions(today.isoformat(), 'NIFTY', db)
    check('hourly checkpoint always reforecasts', len(revs) == 2 and revs[1]['kind'] == 'milestone'
          and revs[1]['trigger_ts'].endswith('10:15:00+05:30'))
    check('model-built path at the checkpoint (direction recorded)', revs[1]['direction'] in ('UP', 'DOWN', 'FLAT'))

    # Hourly input: day path move over the 10:15 slot is a finite number.
    mv = DF.path_move('NIFTY', today, 12, 24, revs[1]['trigger_actual'], db)
    check('day path gives the hourly slot an expected move', mv is not None and abs(mv) < 0.05)

    # Restart catch-up: several marks due at once -> at most one reforecast per loop.
    p2 = {q['k']: q['forecast'] for q in json.loads(revs[1]['points'])}
    for k in range(13, 25):
        bar(k, p2[k] + (60 if k in (15, 20) else 0))
    before = len(DF.revisions(today.isoformat(), 'NIFTY', db))
    dfc.tick('NIFTY', None, DF.ts_of(today, 24) + timedelta(seconds=30))
    check('one reforecast per loop when catching up', len(DF.revisions(today.isoformat(), 'NIFTY', db)) == before + 1)
    for _ in range(6):
        dfc.tick('NIFTY', None, DF.ts_of(today, 24) + timedelta(seconds=30))
    kinds = [(v['kind'], v['trigger_k']) for v in DF.revisions(today.isoformat(), 'NIFTY', db)]
    check('catch-up reaches the 11:15 checkpoint', ('milestone', 24) in kinds)

    # Rest of the day, then the close: score the original only.
    for k in range(25, 76):
        bar(k, p2.get(k, fc[k]))
    for k in range(25, 76):
        dfc.tick('NIFTY', None, DF.ts_of(today, k) + timedelta(seconds=20))
    for _ in range(20):
        dfc.tick('NIFTY', None, at(15, 31))
    row = c.execute('SELECT * FROM day_forecasts').fetchone()
    now_orig = [tuple(p) for p in c.execute('SELECT k, ts, forecast, upper, lower, swing FROM day_forecast_points ORDER BY k')]
    check('original day rows never change', now_orig == original)
    check('day scored at the close', row['status'] == 'final' and row['mae_pts'] is not None)
    ms = [v for v in DF.revisions(today.isoformat(), 'NIFTY', db) if v['kind'] == 'milestone']
    check('one reforecast at every hourly checkpoint', sorted(v['trigger_ts'][11:16] for v in ms)
          == ['10:15', '11:15', '12:15', '13:15', '14:15', '15:15'])
    check('one reforecast per mark at most', len({v['trigger_k'] for v in DF.revisions(today.isoformat(), 'NIFTY', db)})
          == len(DF.revisions(today.isoformat(), 'NIFTY', db)))

    # View: the panel's day card, also inside the hourly payload.
    v = HF.view('NIFTY', day=today, now=at(15, 35), db=db)
    dp = v.get('day_path') or {}
    check('hourly view carries the day path', dp.get('slot') == 'Day' and len(dp.get('points', [])) == 75)
    check('day view lists revisions with kind and 6 checkpoints', len(dp['milestones']) == 6
          and all('kind' in r for r in dp['revisions']))
    check('day view has candles and score', any(p.get('bar') for p in dp['points']) and dp['score'] is not None)

    # Model: day_move is an input with a small starting weight; old saved weights keep working.
    hist = M.build_days(rows)
    di, i, n = 40, 24, 12
    f0 = M.make_forecast(hist, di, i, n, hist[di].opens[i], as_of=hist[di].day)
    f1 = M.make_forecast(hist, di, i, n, hist[di].opens[i], as_of=hist[di].day, day_move=0.004)
    check('no day path: x_day is 0 and the forecast is unchanged by it', f0.inputs['x_day'] == 0.0)
    check('day path nudges the hourly forecast upward', f1.points[-1][1] > f0.points[-1][1])
    check('nudge is small at the starting weight', f1.points[-1][1] - f0.points[-1][1] < 0.25 * f0.band_pts)
    HF.save_params('NIFTY', {k: v for k, v in M.DEFAULT_PARAMS.items() if k != 'beta_day'} | {'beta_reg': 0.4}, 'old', db)
    p = HF.load_params('NIFTY', db)
    check('saved weights without beta_day still load (no reset)', p['beta_reg'] == 0.4 and p['beta_day'] == 0.05)
    check('self-tuning can learn the day-path weight', 'beta_day' in [b[0] for b in M.BETAS]
          and 'day' in M.component_scorecard([]))

    # Empty day view before 09:15 and on a missed day.
    v2 = DF.view('NIFTY', today + timedelta(days=1), at(9, 0) + timedelta(days=1), db=db)
    check('pending before 09:15', v2['status'] == 'pending')

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
