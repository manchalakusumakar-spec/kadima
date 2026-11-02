"""Offline tests for the 2-Nov night release: dynamic maximum stop-loss on the PULSE plan, and the
forecast kept visible after market hours. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_pulse_sl_afterhours_20261102.py
"""
import os
import sys
import tempfile
from collections import deque
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF, live_forecast as LF, pulse_forecast as PF   # noqa: E402
from kadima.config import IST                                                       # noqa: E402

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


at = lambda h, m, s=0: datetime(2026, 11, 2, h, m, s, tzinfo=IST)  # noqa: E731


def tape(start, minutes, f, step=2):
    return [(start + timedelta(seconds=step * k), f(step * k / 60.0)) for k in range(int(minutes * 60 / step))]


def main():
    wig = lambda m: 23300 + 3 * ((int(m) % 2) * 2 - 1)               # noqa: E731  +/-3 every minute
    sp = tape(at(12, 0), 60, wig)
    sig = PF.sigma_1m(sp, at(13, 0), 23300)
    check('typical 1-minute move measured from the tape', 6 < sig < 9)
    check('typical move falls back when there is no history', abs(PF.sigma_1m([], at(9, 16), 25000) - 10.0) < 1e-9)

    # ---------------- trailing max SL for a PE plan (falling line)
    st = {}
    now = at(13, 0)
    line = [(now + timedelta(minutes=k), 23300 - 4 * k) for k in range(0, 21)]
    plan = dict(side='PE', entry=23300, stop=23306, target=23220, target_ts=line[-1][0].isoformat())
    s0 = PF.trail_sl(st, now, 23300, line, plan, 4.0)
    check('PE stop starts above the price', s0['side'] == 'PE' and s0['level'] > 23300)
    check('stop keeps at least 3 typical moves of room', s0['level'] - 23300 >= 3 * 4.0 - 1e-9)
    check('stop covers the forecast worst case + 1.5 typical moves', s0['level'] >= 23300 + 1.5 * 4.0)
    lv = s0['level']
    # price falls 15 pts over 3 minutes along the line -> stop trails down
    for k in range(1, 4):
        t = now + timedelta(minutes=k)
        fwd = [(a, b) for a, b in line if a >= t]
        s = PF.trail_sl(st, t, 23300 - 5 * k, fwd, plan, 4.0)
    check('stop trails down as the price falls', s['level'] < lv and s['trailed'] > 0)
    lv2 = s['level']
    t = now + timedelta(minutes=4)
    s = PF.trail_sl(st, t, 23292, [(a, b) for a, b in line if a >= t], plan, 4.0)
    check('a bounce never loosens the stop', s['level'] == lv2)
    check('history of the trail kept for the chart', len(s['hist']) >= 2 and s['hist'][0][1] == lv)
    # stop-out
    t = now + timedelta(minutes=5)
    s = PF.trail_sl(st, t, lv2 + 0.5, [(a, b) for a, b in line if a >= t], plan, 4.0)
    check('price through the stop marks it hit', s['hit'] == t.isoformat())
    t2 = t + timedelta(minutes=2)
    check('a hit stop stays shown', PF.trail_sl(st, t2, 23290, [(a, b) for a, b in line if a >= t2], plan, 4.0)['hit'] is not None)
    t3 = t + timedelta(minutes=6)
    s = PF.trail_sl(st, t3, 23290, [(t3, 23290), (t3 + timedelta(minutes=10), 23250)], plan, 4.0)
    check('5 minutes after a stop-out a fresh stop starts', s['hit'] is None and s['since'] == t3.isoformat())

    # ---------------- side change must hold 10 s; CE mirror
    st = {}
    up = [(now + timedelta(minutes=k), 23300 + 4 * k) for k in range(0, 21)]
    ce = dict(side='CE', entry=23300, stop=23294, target=23380, target_ts=up[-1][0].isoformat())
    s = PF.trail_sl(st, now, 23300, line, plan, 4.0)
    s = PF.trail_sl(st, now + timedelta(seconds=3), 23300, up, ce, 4.0)
    check('a flicker to CE does not reset the PE stop', s['side'] == 'PE' and s['pending'] == 'CE')
    s = PF.trail_sl(st, now + timedelta(seconds=14), 23300, up, ce, 4.0)
    check('CE held 10 s: the stop resets below the price', s['side'] == 'CE' and s['level'] < 23300 - 3 * 4.0 + 1e-9)
    s = PF.trail_sl(st, now + timedelta(seconds=20), 23300, up, dict(side='WAIT'), 4.0)
    check('plan WAIT keeps showing the stop for a while', s is not None and s['side'] == 'CE')
    check('plan WAIT for over 2 minutes drops it', PF.trail_sl(st, now + timedelta(minutes=4), 23300, up, dict(side='WAIT'), 4.0) is None)

    # ---------------- through pulse.update
    PF._S.clear()
    spots = deque(tape(at(12, 40), 20, lambda m: 23300 - 1.2 * m))
    snap = dict(meter={}, hour=dict(line=[dict(ts=(at(13, 0) + timedelta(minutes=5 * k)).isoformat(), v=23276 - 8 * k) for k in range(0, 4)]))
    p = PF.update('NIFTY', at(13, 0), spots[-1][1], snap, spots)
    check('pulse carries the dynamic max SL with its typical move', 'max_sl' in p and p['sigma_1m'] > 0)
    if p['plan'] and p['plan'].get('side') in ('CE', 'PE'):
        check('max SL on the plan side', p['max_sl'] and p['max_sl']['side'] == p['plan']['side'])
    else:
        check('no side, no stop', p['max_sl'] is None)

    # ---------------- after hours: saved snapshot is served
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'ah.db')

    class St:
        is_live_tick, spot, chain = True, 23300.0, None
        last_updated = at(15, 29, 59)

    class Eng:
        states = {'NIFTY': St()}

    hourly = type('Hourly', (), {})()
    hourly.engine, hourly.db = Eng(), db
    lf = LF.LiveForecaster(hourly)
    lf.tick(at(15, 30, 0))
    check('last snapshot saved at the close', HF.conn(db).execute('SELECT COUNT(*) FROM live_snapshot').fetchone()[0] == 1)
    live = LF.snapshot('NIFTY', db=db, now=at(15, 30, 1))
    check('in market hours the live snapshot is served', live.get('market_open') is True and live.get('saved') is None)
    eve = LF.snapshot('NIFTY', db=db, now=at(22, 30))
    check('after the close the same forecast stays visible', eve.get('spot') == 23300.0 and eve['market_open'] is False)
    LF.LIVE.clear()                                                  # engine restarted in the evening
    eve = LF.snapshot('NIFTY', db=db, now=at(22, 31))
    check('after a restart the saved forecast is served', eve.get('saved') is True and eve['day'] == '2026-11-02'
          and eve.get('pulse') is not None)
    nxt = LF.snapshot('NIFTY', db=db, now=datetime(2026, 11, 3, 8, 50, tzinfo=IST))
    check('next morning before the open: last session, marked with its day', nxt['day'] == '2026-11-02' and nxt['market_open'] is False)
    check('nothing saved: clear reason', LF.snapshot('BANKNIFTY', db=db, now=at(22, 0)).get('available') is False)

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
