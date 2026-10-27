"""Offline tests for the 27-Oct forecast fixes. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_fixes_20261027.py

1. A tick that lands while the day forecast is computing no longer makes the hourly slot "late".
2. Finished sessions missing 1-2 five-minute bars are repaired, so the similar-days search can use them;
   the session being forecast is never touched.
3. Self-tuning: no reweighting after a single slot mid-session, a confidence floor, and a clear
   (2%) improvement needed before new weights are adopted.
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
from kadima import hourly_forecast_model as M                          # noqa: E402
from kadima.config import IST                                         # noqa: E402
from test_hourly_forecast import synthetic                            # noqa: E402

FAILS = []
TOKEN = '99926000'


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


class St:
    def __init__(self, spot, ts):
        self.spot, self.last_updated, self.is_live_tick = spot, ts, True
        self.chain, self.direction, self.score = None, None, None


class Eng:
    def __init__(self, st):
        self.states = {'NIFTY': st}


def put(start, o, h, lo, c, closed=True):
    storage.upsert_candle(TOKEN, 5, start.replace(tzinfo=IST).isoformat(), o, h, lo, c, 0, c, closed)


def main():
    rows = synthetic(n_days=41)
    days_ = sorted({r[0].date() for r in rows})
    today = days_[-1]
    for r in rows:
        if r[0].date() < today:
            put(*r)
    at = lambda h, m, s=0: datetime(today.year, today.month, today.day, h, m, s, tzinfo=IST)  # noqa: E731
    # today's bars up to 10:15
    for r in rows:
        if r[0].date() == today and r[0].hour * 60 + r[0].minute < 10 * 60 + 15:
            put(*r)
    spot = [r for r in rows if r[0].date() == today][12][1]

    # ------------------------------------------------ 1. late-slot bug
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'fix1.db')
    now = at(10, 15, 2)
    st = St(spot, now - timedelta(seconds=1))
    hf = HF.HourlyForecaster(Eng(st), db=db)
    real_day_tick = hf.day.tick

    def slow_day_tick(name, s, n):                  # a new tick lands while the day forecast computes
        real_day_tick(name, s, n)
        s.last_updated = n + timedelta(seconds=2)
        s.spot = spot + 7
    hf.day.tick = slow_day_tick
    hf.tick(now)
    r = HF.conn(db).execute("SELECT late, anchor FROM hourly_forecasts WHERE slot='10:15'").fetchone()
    check('slot made in the same loop as the day forecast is on time', r is not None and r['late'] == 0)
    check('slot anchored on the live price read at the start of the loop', r is not None and abs(r['anchor'] - spot) < 0.01)
    st2 = St(spot, now + timedelta(seconds=3))
    check('a tick a few seconds newer than the loop clock counts as fresh', hf._read_live_inputs(st2, now)[0] == spot)
    st3 = St(spot, now - timedelta(seconds=45))
    check('a price 45 s old is still treated as stale', hf._read_live_inputs(st3, now)[0] is None)
    check('late reason no longer claims the engine was down', 'not running' not in M.REASONS['engine_down'])

    # ------------------------------------------------ 2. missing-bar repair
    def day_rows(d, drop):
        return [r for r in rows if r[0].date() == d and
                ((r[0].hour * 60 + r[0].minute) - 555) // 5 not in drop]
    d1, d2, d3 = days_[-4], days_[-3], days_[-2]
    sub = day_rows(d1, {20}) + day_rows(d2, {0, 74}) + day_rows(d3, {10, 11, 12})
    sub += [r for r in rows if r[0].date() == today and r[0].hour < 10]
    plain = M.build_days(sub)
    filled = M.build_days(sub, fill_before=today)
    by = {d.day: d for d in filled}
    check('without repair those sessions are incomplete', sum(all(c is not None for c in d.closes) for d in plain) == 0)
    check('one missing bar mid-session is repaired', all(c is not None for c in by[d1].closes))
    ok = by[d1].opens[20] == by[d1].closes[19] and min(by[d1].closes[19], by[d1].opens[21]) <= by[d1].closes[20] <= max(by[d1].closes[19], by[d1].opens[21])
    check('repaired bar sits on the line between its neighbours', ok)
    check('missing first and last bars are repaired flat', all(c is not None for c in by[d2].closes)
          and by[d2].closes[74] == by[d2].closes[73] and by[d2].closes[0] == by[d2].opens[1])
    check('three missing bars are left alone', sum(c is None for c in by[d3].closes) == 3)
    check('the session being forecast is never filled', sum(c is None for c in by[today].closes) > 0)
    check('high/low of a repaired bar contain open and close',
          by[d1].highs[20] >= max(by[d1].opens[20], by[d1].closes[20]) and by[d1].lows[20] <= min(by[d1].opens[20], by[d1].closes[20]))

    # ------------------------------------------------ 3. self-tuning guards
    db3 = os.path.join(os.environ['KADIMA_DATA_DIR'], 'fix3.db')
    HF.save_params('NIFTY', dict(M.DEFAULT_PARAMS, prob_c=0.25, beta_shield=0.1), 'test', db3)
    check('confidence below the floor is lifted to 0.40', HF.load_params('NIFTY', db3)['prob_c'] == M.PROB_C_MIN)
    a = dict(n=40, calls=30, hit=0.6, coverage=0.68, logloss=0.703)
    check('a 1.7% improvement is not enough to adopt', M._better(a, dict(a, logloss=0.691)) == '')
    check('a 3% improvement is adopted', M._better(a, dict(a, logloss=0.68)) != '')
    fit = M.fit_params([dict(x_reg=0.1, y=0.0, p_raw=0.9, up=False, zs=[1.0])] * 40)
    check('fitted confidence never goes below the floor', fit['prob_c'] >= M.PROB_C_MIN)
    # an on-time slot finishing mid-session must not change the weights
    hf3 = HF.HourlyForecaster(None, db=db3)
    before = HF.conn(db3).execute("SELECT params FROM hourly_forecast_model WHERE index_name='NIFTY'").fetchone()['params']
    c = HF.conn(db3)
    with c:
        c.execute("INSERT INTO hourly_forecasts (day,index_name,slot,slot_end,steps,generated_at,late,anchor,sigma,"
                  "direction,prob_up,projected_pct,projected_pts,band_pts,inputs,status) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (today.isoformat(), 'NIFTY', '13:15', '14:15', 2, at(13, 15).isoformat(), 0, 25000.0, 0.001,
                   'UP', 0.6, 0.1, 20.0, 50.0, json.dumps({'sd': [1.0, 1.4], 'mean': [0.1, 0.2]}), 'live'))
        for k in (1, 2):
            c.execute("INSERT INTO hourly_forecast_points (day,index_name,slot,k,ts,forecast,upper,lower,actual,actual_source) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (today.isoformat(), 'NIFTY', '13:15', k,
                      (at(13, 15) + timedelta(minutes=5 * k)).isoformat(), 25010.0, 25040.0, 24980.0, 25005.0, 'closed_5m'))
    r = c.execute("SELECT * FROM hourly_forecasts WHERE slot='13:15'").fetchone()
    pts = c.execute("SELECT * FROM hourly_forecast_points WHERE slot='13:15' ORDER BY k").fetchall()
    hf3.finalize(r, pts)
    after = c.execute("SELECT params FROM hourly_forecast_model WHERE index_name='NIFTY'").fetchone()['params']
    a3 = c.execute("SELECT modification FROM hourly_forecast_audit WHERE slot='13:15'").fetchone()
    check('a finished slot mid-session leaves the weights unchanged', before == after)
    check('audit says the hour joins the daily retune', a3 is not None and 'daily retune' in a3['modification'])

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
