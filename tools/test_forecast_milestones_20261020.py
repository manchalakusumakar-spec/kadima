"""Offline tests for the 5-minute milestone chart and the 5-point reforecast. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_milestones_20261020.py

A closed candle 5 points or more from the path in force publishes a revised path with a receipt.
Provisional prices never trigger one. Every revision is kept. The original forecast rows and the
score are exactly what they would be without revisions. The view returns candles and revisions.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF, storage                      # noqa: E402
from kadima import hourly_forecast_model as M                          # noqa: E402
from kadima.config import IST                                         # noqa: E402

FAILS = []
TOKEN = '99926000'
DAY = datetime(2026, 10, 21, tzinfo=IST).date()
SLOT = '14:15'


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def at(h, m, s=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=IST)


T0 = at(14, 15)


def ts(k):
    return T0 + timedelta(minutes=5 * k)


def fc(k):
    return 25000.0 + 2 * k


def candle(k, close, closed=True):
    """The 5-minute bar that ends at milestone k."""
    o = close - 1
    storage.upsert_candle(TOKEN, 5, (ts(k) - timedelta(minutes=5)).isoformat(), o, close + 2, o - 2, close, 0,
                          close, closed)


class Case:
    """The real 14:15-15:15 slot (12 milestones) in a fresh forecast.db."""
    n = 0

    def __init__(self):
        Case.n += 1
        self.db = os.path.join(os.environ['KADIMA_DATA_DIR'], f'milestones_{Case.n}.db')
        self.f = HF.HourlyForecaster(None, db=self.db)
        self.c = HF.conn(self.db)
        with self.c:
            self.c.execute('INSERT INTO hourly_forecasts (day,index_name,slot,slot_end,steps,generated_at,'
                           'late,anchor,sigma,direction,prob_up,projected_pct,projected_pts,band_pts,inputs,'
                           'status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (DAY.isoformat(), 'NIFTY', SLOT, '15:15', 12, T0.isoformat(), 1,
                            25000.0, 0.001, 'UP', 0.6, 0.1, 24.0, 20.0,
                            json.dumps({'sd': [1 + k / 10 for k in range(12)]}), 'live'))
            for k in range(1, 13):
                self.c.execute('INSERT INTO hourly_forecast_points (day,index_name,slot,k,ts,forecast,upper,'
                               'lower) VALUES (?,?,?,?,?,?,?,?)',
                               (DAY.isoformat(), 'NIFTY', SLOT, k, ts(k).isoformat(), fc(k), fc(k) + 15,
                                fc(k) - 15))
        self.original = self.snapshot()

    def snapshot(self):
        return [tuple(r) for r in self.c.execute(
            'SELECT k, ts, forecast, upper, lower FROM hourly_forecast_points WHERE slot=? ORDER BY k', (SLOT,))]

    def revs(self):
        return HF.revisions(DAY.isoformat(), 'NIFTY', SLOT, self.db)

    def row(self):
        return self.c.execute('SELECT * FROM hourly_forecasts WHERE slot=?', (SLOT,)).fetchone()

    def run(self, now):
        self.f.update_actuals('NIFTY', now, token=TOKEN)


def reset_candles():
    c = storage.conn()
    with c:
        c.execute('DELETE FROM candles WHERE token=?', (TOKEN,))


def main():
    # 1. Under 5 points: no reforecast.
    reset_candles()
    for k, off in ((1, 1.0), (2, -2.5), (3, 4.99)):
        candle(k, fc(k) + off)
    t = Case()
    t.run(ts(3) + timedelta(seconds=10))
    check('deviation under 5 points: no reforecast', not t.revs())

    # 2. Closed candle 5+ points off: R1, shifted original path, verifiable receipt.
    candle(4, fc(4) + 6.0)
    t.run(ts(4) + timedelta(seconds=10))
    revs = t.revs()
    check('closed candle 6 pts off: one reforecast', len(revs) == 1 and revs[0]['trigger_k'] == 4)
    pts = json.loads(revs[0]['points']) if revs else []
    check('R1 path covers the milestones after the trigger', [p['k'] for p in pts] == list(range(5, 13)))
    check('R1 path is the original shifted through the close',
          all(abs(p['forecast'] - (fc(p['k']) + 6.0)) < 1e-6 for p in pts))
    check('R1 deviation recorded', revs and revs[0]['deviation_pts'] == 6.0)
    check('R1 receipt matches what was published',
          revs and revs[0]['receipt'] == HF.receipt(DAY.isoformat(), 'NIFTY', SLOT, 1, 4, pts))
    check('original rows unchanged after a reforecast', t.snapshot() == t.original)

    # 3. Re-running publishes nothing new (idempotent).
    t.run(ts(4) + timedelta(seconds=40))
    check('re-run: still one reforecast', len(t.revs()) == 1)

    # 4. Measured against the path in force: 8 pts off the original is only 2 off R1.
    candle(5, fc(5) + 8.0)
    t.run(ts(5) + timedelta(seconds=10))
    check('2 pts off the latest path: no new reforecast', len(t.revs()) == 1)

    # 5. A provisional (unclosed) bar 5+ points off never triggers; the close does.
    candle(6, fc(6) + 12.0, closed=False)
    t.run(ts(6) + timedelta(seconds=70))
    check('provisional bar 6 pts off R1: no reforecast', len(t.revs()) == 1)
    candle(6, fc(6) + 12.0, closed=True)
    t.run(ts(6) + timedelta(seconds=90))
    revs = t.revs()
    check('same bar once closed: R2 against R1', len(revs) == 2 and revs[1]['trigger_k'] == 6
          and revs[1]['deviation_pts'] == 6.0)
    check('R1 is kept after R2', revs[0]['rev'] == 1 and revs[0]['trigger_k'] == 4)

    # 6. Every 5-point miss adds a line; the last milestone never triggers one.
    candle(7, fc(7) + 6.0)                   # 6 below R2 (which sits at +12)
    for k in range(8, 12):
        candle(k, fc(k) + 6.0)
    candle(12, fc(12) + 30.0)
    t.run(ts(12) + timedelta(seconds=10))
    revs = t.revs()
    check('downward miss vs R2: R3', len(revs) == 3 and revs[2]['trigger_k'] == 7 and revs[2]['deviation_pts'] == -6.0)
    check('last milestone never publishes a reforecast', all(v['trigger_k'] < 12 for v in revs))

    # 7. Score is computed on the original path only, exactly as without revisions.
    r = t.row()
    actual = [fc(k) + off for k, off in ((1, 1.0), (2, -2.5), (3, 4.99), (4, 6.0), (5, 8.0), (6, 12.0),
                                          (7, 6.0), (8, 6.0), (9, 6.0), (10, 6.0), (11, 6.0), (12, 30.0))]
    want = M.score([(k, fc(k), fc(k) + 15, fc(k) - 15) for k in range(1, 13)], 25000.0, actual, 'UP', 0.001,
                   2.1)
    check('slot final and scored', r['status'] == 'final')
    check('score uses the original path (mae and coverage unchanged by revisions)',
          abs(r['mae_pts'] - want['mae_pts']) < 1e-6 and abs(r['coverage'] - want['coverage']) < 1e-6)
    check('original rows unchanged after scoring', t.snapshot() == t.original)

    # 8. View: candles per milestone and every revision for the chart.
    v = HF.view('NIFTY', day=DAY, now=at(15, 20), db=t.db)
    s = next(x for x in v['slots'] if x['slot'] == SLOT)
    check('view returns all revisions with receipts',
          [x['rev'] for x in s['revisions']] == [1, 2, 3] and all(x['receipt'] for x in s['revisions']))
    b = s['points'][3]['bar']
    check('view returns the closed 5-minute bar per milestone',
          b and b['close'] == fc(4) + 6.0 and b['high'] == fc(4) + 8.0 and b['closed'] is True)

    # 9. No revisions table rows for a slot that never deviates; view still works.
    reset_candles()
    t = Case()
    for k in range(1, 13):
        candle(k, fc(k) + 1.0)
    t.run(ts(12) + timedelta(seconds=10))
    v = HF.view('NIFTY', day=DAY, now=at(15, 20), db=t.db)
    s = next(x for x in v['slots'] if x['slot'] == SLOT)
    check('quiet slot: no revisions, scored', s['revisions'] == [] and t.row()['status'] == 'final')

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
