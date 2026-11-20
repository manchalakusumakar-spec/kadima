"""Offline tests for the 20-Nov day-forecast scoring change. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_day_flat_no_call_20261120.py

A FLAT day call is 'no direction call': dir_hit stays NULL (not a miss), the old 0.10% rule is
kept in reasons as flat_rule_hit, and band / path error / end error are scored as before.
UP/DOWN calls are scored exactly as before. record() counts UP/DOWN calls only.
"""
import json
import os
import sys
import tempfile
from datetime import date

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import audit                                               # noqa: E402
audit.log = lambda *a, **k: None
from kadima import hourly_forecast as HF                               # noqa: E402
from kadima import day_forecast as DF                                  # noqa: E402

FAILS = []
SRC = sorted(HF.VERIFIED_SOURCES)[0]


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def make(c, day, direction, anchor, end):
    d = day.isoformat()
    with c:
        c.execute("INSERT INTO day_forecasts (day,index_name,generated_at,late,anchor,sigma,direction,prob_up,"
                  "projected_pct,projected_pts,band_pts,inputs,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (d, 'NIFTY', d + 'T09:20:06+05:30', 0, anchor, 0.0008, direction, 0.5, 0.0, 0.0, 100.0,
                   json.dumps(dict(official='official_0920', anchor_k=1)), 'live'))
        for k in range(1, DF.STEPS + 1):
            a = anchor + (end - anchor) * (k - 1) / (DF.STEPS - 1)
            c.execute("INSERT INTO day_forecast_points (day,index_name,k,ts,forecast,upper,lower,actual,actual_source) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (d, 'NIFTY', k, DF.ts_of(day, k).isoformat(), anchor,
                                                     anchor + 100, anchor - 100, a, SRC))
    row = c.execute('SELECT * FROM day_forecasts WHERE day=?', (d,)).fetchone()
    pts = c.execute('SELECT * FROM day_forecast_points WHERE day=? ORDER BY k', (d,)).fetchall()
    return row, pts


def main():
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'flat.db')
    hf = HF.HourlyForecaster(None, db=db)
    dfc, c = hf.day, DF.conn(db)

    # FLAT on a +345 trend day: no call, not a miss
    row, pts = make(c, date(2026, 11, 20), 'FLAT', 25456.0, 25800.6)
    sc = dfc.finalize(row, pts)
    r = c.execute("SELECT * FROM day_forecasts WHERE day='2026-11-20'").fetchone()
    why = json.loads(r['reasons'])
    check('FLAT day: dir_hit is NULL (no direction call, not a miss)', r['dir_hit'] is None and sc['dir_hit'] is None)
    check('FLAT day: old 0.10% rule kept as flat_rule_hit=False', why.get('flat_rule_hit') is False
          and why.get('direction_call') == 'none')
    check('FLAT day: tagged no_direction_call, band breach still explained',
          'no_direction_call' in why['tags'] and 'band_breach' in why['tags'])
    check('FLAT day: end error and path error still scored', abs(r['end_error_pts'] - 344.6) < 0.01
          and r['mae_pts'] > 100 and r['coverage'] < 0.6)

    # FLAT on a quiet day: still no call (the old rule would have said hit)
    row, pts = make(c, date(2026, 11, 19), 'FLAT', 25000.0, 25010.0)
    dfc.finalize(row, pts)
    r = c.execute("SELECT * FROM day_forecasts WHERE day='2026-11-19'").fetchone()
    check('FLAT quiet day: dir_hit NULL, flat_rule_hit=True kept',
          r['dir_hit'] is None and json.loads(r['reasons']).get('flat_rule_hit') is True)

    # UP / DOWN calls are scored exactly as before
    row, pts = make(c, date(2026, 11, 18), 'UP', 25000.0, 25100.0)
    dfc.finalize(row, pts)
    row, pts = make(c, date(2026, 11, 17), 'DOWN', 25000.0, 25100.0)
    dfc.finalize(row, pts)
    up = c.execute("SELECT * FROM day_forecasts WHERE day='2026-11-18'").fetchone()
    dn = c.execute("SELECT * FROM day_forecasts WHERE day='2026-11-17'").fetchone()
    check('UP call that went up: hit', up['dir_hit'] == 1 and 'direction_call' not in json.loads(up['reasons']))
    check('DOWN call that went up: miss', dn['dir_hit'] == 0)

    rec = DF.record('NIFTY', '2026-11-01', db)
    check('record counts UP/DOWN calls only', rec == dict(days=4, calls=2, hits=1, no_calls=2, hit_rate=0.5))

    v = DF.view('NIFTY', date(2026, 11, 20), DF.ts_of(date(2026, 11, 20), 80), db=db)
    check('panel: FLAT day shows no HIT/MISS and explains why',
          v['score']['dir_hit'] is None and v['score']['direction_call'] == 'none'
          and any('no direction call' in x for x in v['score']['reasons']))

    # switch off = 05/13-Nov behaviour (FLAT scored by the 0.10% rule)
    DF.SCORE_FLAT_AS_NO_CALL = False
    row, pts = make(c, date(2026, 11, 16), 'FLAT', 25000.0, 25300.0)
    dfc.finalize(row, pts)
    r = c.execute("SELECT * FROM day_forecasts WHERE day='2026-11-16'").fetchone()
    check('switch off: FLAT on a big move is a miss again', r['dir_hit'] == 0)
    DF.SCORE_FLAT_AS_NO_CALL = True

    print(f'\n{len(FAILS)} failed' if FAILS else '\nall passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
