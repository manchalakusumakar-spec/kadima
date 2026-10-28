"""Offline tests for the live forecast line and the sharp-move meter (28 Oct). No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_live_forecast_20261028.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF, live_forecast as LF          # noqa: E402
from kadima.config import IST                                         # noqa: E402

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


D = datetime(2026, 10, 28, tzinfo=IST).date()
at = lambda h, m, s=0: datetime(2026, 10, 28, h, m, s, tzinfo=IST)  # noqa: E731


def main():
    # ---------------- live line
    pts = [dict(ts=(at(14, 15) + timedelta(minutes=5 * k)).isoformat(), forecast=22650.0 + k, upper=22650 + k + 10 * k ** 0.5,
                lower=22650 + k - 10 * k ** 0.5) for k in range(1, 13)]
    rev = dict(trigger_ts=at(14, 40).isoformat(), trigger_actual=22740.0,
               points=[dict(ts=p['ts'], forecast=p['forecast'] + 85, upper=p['upper'] + 85, lower=p['lower'] + 85)
                       for p in pts if p['ts'] > at(14, 40).isoformat()])
    path = LF.path_in_force(pts, [rev])
    check('path in force uses the revision after its trigger', abs(path[at(14, 45).isoformat()][0] - (22650 + 6 + 85)) < 1e-6)
    check('path in force keeps the original before the trigger', abs(path[at(14, 30).isoformat()][0] - 22653) < 1e-6)
    check('revision starts at its trigger price', path[at(14, 40).isoformat()][0] == 22740.0)
    now = at(14, 42, 30)
    line = LF.live_line(path, at(14, 15).isoformat(), 22650.0, now, 22760.0)
    check('live line starts at the live price, now', line[0]['v'] == 22760.0 and line[0]['ts'] == now.isoformat())
    check('live line runs to the end of the hour', line[-1]['ts'] == at(15, 15).isoformat())
    base = 22740.0 + (22656 + 85 - 22740.0) * 0.5          # path value at 14:42:30 (between 14:40 and 14:45)
    check('live line keeps the expected moves of the path in force',
          abs(line[-1]['v'] - (22662 + 85 + (22760 - base))) < 0.05)
    hw_now = [p['up'] - p['v'] for p in line[1:]]
    check('band narrows with time left and grows towards the end', hw_now[0] < hw_now[-1] and all(h >= 0 for h in hw_now))
    check('no live line without a price', LF.live_line(path, at(14, 15).isoformat(), 22650.0, now, None) == [])

    # ---------------- meter
    spot = 22700.0
    days = 1.5

    def chain(ce_wall_oi, pe_wall_oi, ce_ltp=60.0, pe_ltp=55.0, put_heavy=False):
        q = []
        for k in range(-5, 6):
            strike = 22700 + 50 * k
            ce = 100000 if strike != 22800 else ce_wall_oi
            pe = (400000 if put_heavy else 100000) if strike != 22600 else pe_wall_oi
            ltp_ce = max(1.0, ce_ltp - 0.5 * (strike - 22700)) if strike >= 22700 else ce_ltp + (22700 - strike) * 0.6
            ltp_pe = max(1.0, pe_ltp - 0.5 * (22700 - strike)) if strike <= 22700 else pe_ltp + (strike - 22700) * 0.6
            q.append(dict(token=f'c{k}', strike=strike, opt_type='CE', ltp=ltp_ce, oi=ce))
            q.append(dict(token=f'p{k}', strike=strike, opt_type='PE', ltp=ltp_pe, oi=pe))
        return q
    s0 = LF.chain_state(spot, chain(900000, 900000), days)
    check('chain state computed from greeks', s0 is not None and s0['ce_wall'][0] == 22800 and s0['pe_wall'][0] == 22600)
    s_put = LF.chain_state(spot, chain(900000, 900000, put_heavy=True), days)
    check('put-heavy gamma reads as dealer short gamma (negative)', s_put['gex_norm'] < s0['gex_norm'] and s_put['gex_norm'] < 0)
    hist = [(at(14, 20), s0)]
    s1 = LF.chain_state(spot, chain(600000, 900000, ce_ltp=66.0, pe_ltp=50.0), days)
    hist.append((at(14, 30, 30), s1))
    score, lean, strength, parts = LF.meter(at(14, 30, 30), spot, s1, hist, [])
    check('CE wall unwinding 33% is seen', abs(parts['wall_stress_ce'] - 1 / 3) < 0.01)
    check('CE wall giving way + CE premium leading -> UP lean', lean == 'UP' and strength > 0)
    check('score rises with wall stress', score >= 25)
    sq, _, _, _ = LF.meter(at(14, 30, 30), spot, s0, [(at(14, 20), s0), (at(14, 30, 30), s0)], [])
    check('quiet, unchanged chain scores low', sq < 35)
    # squeeze from spot samples: wide early ranges, tiny recent ranges
    spots = []
    t = at(9, 15)
    for m in range(60):
        amp = 20 if m < 45 else 1
        for sec in range(0, 60, 2):
            spots.append((t + timedelta(minutes=m, seconds=sec), 22700 + amp * ((sec // 2) % 2)))
    _, _, _, p2 = LF.meter(at(10, 15), spot, s0, [(at(10, 15), s0)], spots)
    check('squeeze detected when recent ranges are far below usual', p2.get('squeeze_ratio', 9) < 0.5)

    # ---------------- thread tick with a fake engine and stored forecast
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'live.db')
    c = HF.conn(db)
    with c:
        c.execute("INSERT INTO hourly_forecasts (day,index_name,slot,slot_end,steps,generated_at,late,anchor,sigma,direction,"
                  "prob_up,projected_pct,projected_pts,band_pts,inputs,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (D.isoformat(), 'NIFTY', '14:15', '15:15', 12, at(14, 15).isoformat(), 0, 22650.0, 0.001, 'FLAT', 0.5,
                   0, 12, 35, json.dumps({}), 'live'))
        for k, p in enumerate(pts, start=1):
            c.execute('INSERT INTO hourly_forecast_points (day,index_name,slot,k,ts,forecast,upper,lower) VALUES (?,?,?,?,?,?,?,?)',
                      (D.isoformat(), 'NIFTY', '14:15', k, p['ts'], p['forecast'], p['upper'], p['lower']))

    class Chain:
        _forecast_sample = dict(mono=time.monotonic(), quotes=chain(900000, 900000))

    class St:
        is_live_tick, spot, chain = True, 22700.0, Chain()
        last_updated = at(14, 42, 29)

    class Eng:
        states = {'NIFTY': St()}

        def _dte(self, st):
            return 1.5

    hourly = type('Hourly', (), {})()
    hourly.engine, hourly.db = Eng(), db
    lf = LF.LiveForecaster(hourly)
    lf.tick(at(14, 42, 30))
    snap = LF.snapshot('NIFTY')
    check('snapshot has the live hour line', snap['hour'] and snap['hour']['slot'] == '14:15' and len(snap['hour']['line']) > 2)
    check('snapshot has a meter reading', snap['meter'] and snap['meter']['available'] and 0 <= snap['meter']['score'] <= 100)
    check('meter reading logged for measurement', c.execute('SELECT COUNT(*) FROM live_meter').fetchone()[0] == 1)
    check('meter marked uncalibrated', snap['meter']['calibrated'] is False)
    lf.tick(at(16, 0))
    check('nothing runs outside market hours', LF.snapshot('NIFTY')['ts'] == at(14, 42, 30).isoformat())

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
