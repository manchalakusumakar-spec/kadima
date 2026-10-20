"""Offline tests for the 2026-10-20 forecast scoring-integrity fix. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_scoring_20261020.py

Only a closed candle starting exactly at the target bar may be scored; a provisional
(unclosed) value is shown but never scored; a slot that gives up with a required actual
missing or unverified stays incomplete and unscored; the original forecast is never changed.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF, storage                      # noqa: E402
from kadima.config import IST                                         # noqa: E402

FAILS = []
TOKEN = '99926000'
DAY = datetime(2026, 10, 21, tzinfo=IST).date()


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def at(h, m, s=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=IST)


def candle(start, close, closed=True, interval=5):
    storage.upsert_candle(TOKEN, interval, start.isoformat(), close, close, close, close, 0, close, closed)


class Case:
    """One 3-point slot 15:15-15:30 in a fresh forecast.db."""
    n = 0

    def __init__(self):
        Case.n += 1
        self.db = os.path.join(os.environ['KADIMA_DATA_DIR'], f'forecast_{Case.n}.db')
        self.f = HF.HourlyForecaster(None, db=self.db)
        self.c = HF.conn(self.db)
        self.slot = f'S{Case.n}'
        with self.c:
            self.c.execute('INSERT INTO hourly_forecasts (day,index_name,slot,slot_end,steps,generated_at,'
                           'late,anchor,sigma,direction,prob_up,projected_pct,projected_pts,band_pts,inputs,'
                           'status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (DAY.isoformat(), 'NIFTY', self.slot, '15:30', 3, at(15, 15).isoformat(), 1,
                            25000.0, 0.001, 'UP', 0.6, 0.1, 25.0, 20.0, json.dumps({'sd': [1, 1.4, 1.7]}),
                            'live'))
            for k, m in enumerate((20, 25, 30), 1):
                self.c.execute('INSERT INTO hourly_forecast_points (day,index_name,slot,k,ts,forecast,upper,'
                               'lower) VALUES (?,?,?,?,?,?,?,?)',
                               (DAY.isoformat(), 'NIFTY', self.slot, k, at(15, m).isoformat(),
                                25000.0 + 8 * k, 25010.0 + 8 * k, 24990.0 + 8 * k))
        self.original = self.points_snapshot()

    def points_snapshot(self):
        return [tuple(r) for r in self.c.execute(
            'SELECT k, ts, forecast, upper, lower FROM hourly_forecast_points WHERE slot=? ORDER BY k',
            (self.slot,))]

    def row(self):
        return self.c.execute('SELECT * FROM hourly_forecasts WHERE slot=?', (self.slot,)).fetchone()

    def points(self):
        return self.c.execute('SELECT * FROM hourly_forecast_points WHERE slot=? ORDER BY k',
                              (self.slot,)).fetchall()

    def run(self, now, give_up=False):
        self.f.update_actuals('NIFTY', now, token=TOKEN, give_up=give_up)


def reset_candles():
    c = storage.conn()
    with c:
        c.execute('DELETE FROM candles WHERE token=?', (TOKEN,))


def main():
    # 1. All three targets closed and exactly aligned: scored once the slot ends.
    reset_candles()
    for m, px in ((15, 25006.0), (20, 25020.0), (25, 25030.0)):
        candle(at(15, m), px)
    t = Case()
    t.run(at(15, 30, 10))
    r = t.row()
    check('all verified actuals: slot final and scored', r['status'] == 'final' and r['mae_pts'] is not None)
    check('all verified actuals: sources recorded as closed_5m',
          [p['actual_source'] for p in t.points()] == ['closed_5m'] * 3)
    check('original forecast unchanged after scoring', t.points_snapshot() == t.original)

    # 2. Endpoint bar never closes (no next tick): provisional value shown, slot not scored.
    reset_candles()
    candle(at(15, 15), 25006.0)
    candle(at(15, 20), 25020.0)
    candle(at(15, 25), 25030.0, closed=False)
    t = Case()
    t.run(at(15, 31, 30))
    pts = t.points()
    check('unclosed endpoint: value shown as provisional',
          pts[-1]['actual'] == 25030.0 and pts[-1]['actual_source'] == HF.PROVISIONAL)
    check('unclosed endpoint: slot stays live, not scored', t.row()['status'] == 'live' and t.row()['mae_pts'] is None)
    t.run(at(15, 45, 5))
    r = t.row()
    check('unclosed endpoint at 15-min give-up: incomplete, unscored',
          r['status'] == 'incomplete' and r['dir_hit'] is None and r['mae_pts'] is None)
    check('incomplete slot excluded from calibration samples',
          not [x for x in HF.samples_for('NIFTY', db=t.db) if x.get('slot') == t.slot])
    check('original forecast unchanged after give-up', t.points_snapshot() == t.original)

    # 3. Provisional value later upgraded when the bar does close (before give-up): scored.
    reset_candles()
    candle(at(15, 15), 25006.0)
    candle(at(15, 20), 25020.0)
    candle(at(15, 25), 25029.0, closed=False)
    t = Case()
    t.run(at(15, 31, 30))
    candle(at(15, 25), 25031.0, closed=True)
    t.run(at(15, 32))
    pts, r = t.points(), t.row()
    check('provisional upgraded to closed value and scored',
          pts[-1]['actual'] == 25031.0 and pts[-1]['actual_source'] == 'closed_5m' and r['status'] == 'final')

    # 4. Missing intermediate actual: never scored, incomplete at give-up.
    reset_candles()
    candle(at(15, 15), 25006.0)
    candle(at(15, 25), 25030.0)
    t = Case()
    t.run(at(15, 30, 10))
    check('missing intermediate: not scored at slot end', t.row()['status'] == 'live')
    t.run(at(15, 45, 0))
    check('missing intermediate at give-up: incomplete, unscored',
          t.row()['status'] == 'incomplete' and t.row()['mae_pts'] is None)

    # 5. Missing endpoint actual: incomplete at give-up.
    reset_candles()
    candle(at(15, 15), 25006.0)
    candle(at(15, 20), 25020.0)
    t = Case()
    t.run(at(15, 45, 0))
    check('missing endpoint at give-up: incomplete, unscored', t.row()['status'] == 'incomplete')

    # 6. Timestamp mismatch: a closed candle not starting exactly at the target bar is ignored.
    reset_candles()
    candle(at(15, 15), 25006.0)
    candle(at(15, 20), 25020.0)
    candle(at(15, 26), 25030.0)          # misaligned start, 5-min interval
    candle(at(15, 24), 25030.0, interval=1)  # 1-min bar 15:24 (ends 15:25), wrong target for 15:30
    t = Case()
    t.run(at(15, 31))
    check('misaligned candle never used for the 15:30 target', t.points()[-1]['actual'] is None)
    # Exactly aligned 1-min fallback (15:29 bar ends at 15:30) is closed and scorable.
    candle(at(15, 29), 25031.0, interval=1)
    t.run(at(15, 31, 10))
    check('aligned closed 1-min bar accepted as closed_1m',
          t.points()[-1]['actual_source'] == 'closed_1m' and t.row()['status'] == 'final')

    # 7. Shutdown give-up (service stop) with an unverified point: incomplete, never final.
    reset_candles()
    candle(at(15, 15), 25006.0)
    candle(at(15, 20), 25020.0)
    candle(at(15, 25), 25030.0, closed=False)
    t = Case()
    t.run(at(15, 31, 0), give_up=True)
    check('give-up on stop with provisional endpoint: incomplete', t.row()['status'] == 'incomplete')

    # 8. Direct finalize call on unverified points is refused (defence in depth).
    reset_candles()
    t = Case()
    with t.c:
        t.c.execute("UPDATE hourly_forecast_points SET actual=25010, actual_source='provisional' WHERE slot=?",
                    (t.slot,))
    t.f.finalize(t.row(), t.points())
    check('finalize refuses provisional actuals', t.row()['status'] == 'incomplete' and t.row()['mae_pts'] is None)

    # 9. Schema migration on an existing pre-fix database adds the column, keeps the rows.
    old = os.path.join(os.environ['KADIMA_DATA_DIR'], 'forecast_old.db')
    import sqlite3
    s = sqlite3.connect(old)
    s.executescript(HF.SCHEMA.replace('PRAGMA journal_mode=WAL;', ''))
    s.execute("INSERT INTO hourly_forecast_points VALUES ('2026-10-16','NIFTY','14:30',1,'x',1,2,0,1.5)")
    s.commit()
    s.close()
    cols = {r['name'] for r in HF.conn(old).execute('PRAGMA table_info(hourly_forecast_points)')}
    n = HF.conn(old).execute('SELECT COUNT(*) FROM hourly_forecast_points').fetchone()[0]
    check('migration adds actual_source and keeps existing rows', 'actual_source' in cols and n == 1)

    # 10. View exposes the source so the panel can label provisional values.
    v = HF.view('NIFTY', day=DAY, now=at(15, 50), db=t.db)
    check('view returns actual_source per point', all('actual_source' in p for s in v['slots']
                                                      for p in s.get('points', [])))

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
