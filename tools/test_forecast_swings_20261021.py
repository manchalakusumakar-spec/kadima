"""Offline tests for the expected 5-minute swings on the original forecast. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_swings_20261021.py

The original forecast carries an expected path with 5-minute ups and downs (the closest past
match's shape, detrended onto the average path). It ends on the average path, is stored with the
forecast, and is never scored. Saving a new forecast works on a database that already has the
columns added after the table was first created (actual_source, swing).
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
sys.path.insert(0, 'tools')
from kadima import hourly_forecast as HF                                # noqa: E402
from kadima import hourly_forecast_model as M                           # noqa: E402
from kadima.config import IST                                         # noqa: E402
from test_hourly_forecast import synthetic                            # noqa: E402

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def turns(seq):
    return sum(1 for k in range(1, len(seq) - 1) if (seq[k] - seq[k - 1]) * (seq[k + 1] - seq[k]) < 0)


def main():
    rows = synthetic(n_days=41)
    days = M.build_days(rows)
    di, i, n = 40, 24, 12                       # 11:15 slot on the last day
    f = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day)
    check('forecast made', f is not None)
    check('swing path has one price per milestone', len(f.swing) == n)
    check('swing path ends on the average path', abs(f.swing[-1] - f.points[-1][1]) < 0.05)
    check('swing path has ups and downs', turns([f.anchor] + f.swing) >= 1)
    check('swing path differs from the average path', any(abs(a - p[1]) > 0.5 for a, p in zip(f.swing, f.points)))
    check('closest past match recorded', bool(f.inputs.get('swing_from')))
    g = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day)
    check('swing path is deterministic', g.swing == f.swing and g.points == f.points)

    # No past matches (too little history): swing falls back to the average path.
    f3 = M.make_forecast(days[:3], 2, 24, 12, days[2].opens[24], as_of=days[2].day)
    if f3 is not None:
        check('no matches: swing equals the average path',
              all(abs(a - p[1]) < 0.01 for a, p in zip(f3.swing, f3.points)))

    # Saving: a database created before actual_source/swing existed, then migrated by conn().
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'swing_old.db')
    s = sqlite3.connect(db)
    s.executescript("""CREATE TABLE hourly_forecast_points (
        day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL,
        k INTEGER NOT NULL, ts TEXT NOT NULL,
        forecast REAL NOT NULL, upper REAL NOT NULL, lower REAL NOT NULL,
        actual REAL, PRIMARY KEY (day, index_name, slot, k));""")
    s.close()
    fc = HF.HourlyForecaster(None, db=db)
    d = days[di].day
    day = d if hasattr(d, 'year') else datetime.fromisoformat(str(d)).date()
    hist = [r for r in rows if r[0].date() < day]
    now = datetime(day.year, day.month, day.day, 11, 15, 30, tzinfo=IST)
    out = fc.generate('NIFTY', '99926000', day, ((11, 15), (12, 15)), days[di].opens[i], 0, now,
                      rows=[(r[0].replace(tzinfo=IST),) + tuple(r[1:]) for r in hist])
    c = HF.conn(db)
    pts = c.execute("SELECT k, forecast, swing, actual_source FROM hourly_forecast_points WHERE slot='11:15' "
                    "ORDER BY k").fetchall()
    check('new forecast saved on a migrated database', out is not None and len(pts) == 12)
    check('swing saved with each milestone', pts and all(p['swing'] is not None for p in pts))

    # View returns the swing per point and where the shape came from.
    v = HF.view('NIFTY', day=day, now=now + timedelta(minutes=10), db=db)
    sl = next(x for x in v['slots'] if x['slot'] == '11:15')
    check('view returns swing per point', all('swing' in p for p in sl['points']))
    check('view returns the closest match', 'swing_from' in sl)

    # Reforecast paths carry the swing shape, shifted.
    pts = c.execute("SELECT * FROM hourly_forecast_points WHERE slot='11:15' ORDER BY k").fetchall()
    fake = [dict(p) for p in pts]
    fake[2]['actual'], fake[2]['actual_source'] = fake[2]['forecast'] + 7.0, 'closed_5m'
    r = c.execute("SELECT * FROM hourly_forecasts WHERE slot='11:15'").fetchone()
    fc.reforecast(r, fake, now + timedelta(minutes=16))
    rv = HF.revisions(day.isoformat(), 'NIFTY', '11:15', db)
    import json
    path = json.loads(rv[0]['points']) if rv else []
    check('reforecast path carries the shifted swing',
          path and all(abs(q['swing'] - (fake[q['k'] - 1]['swing'] + 7.0)) < 0.01 for q in path))

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
