"""Offline tests for the 2-Nov night PRECISION release of PULSE and PULSE on every index.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_pulse_precision_20261102.py
"""
import json
import os
import sys
import tempfile
import time
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


at = lambda h, m, s=0: datetime(2026, 11, 3, h, m, s, tzinfo=IST)  # noqa: E731


def quiet(start, end, p0, drift_per_min, amp=4.0):
    """2-s ticks: slow drift with a +/-amp zigzag every 30 s (5-min ranges ~2*amp)."""
    out, t, k = [], start, 0
    while t < end:
        m = (t - start).total_seconds() / 60
        out.append((t, p0 + drift_per_min * m + (amp if (k // 15) % 2 else -amp)))
        t += timedelta(seconds=2)
        k += 1
    return out


def ramp(start, secs, p0, p1):
    return [(start + timedelta(seconds=2 * i), p0 + (p1 - p0) * (2 * i) / secs) for i in range(secs // 2)]


def run(name, ticks, snap=None):
    PF._S.pop(name, None)
    sp, res = deque(maxlen=20000), []
    for t, p in ticks:
        sp.append((t, p))
        res.append((t, PF.update(name, t, p, dict(snap or {}, meter=(snap or {}).get('meter', {})), sp)))
    return res


def circuits(res):
    return [(t, r['circuit']) for t, r in res if r['mode'] == 'circuit']


def main():
    base = quiet(at(9, 15), at(11, 0), 23000, 0.3)                  # day drifting up ~30 pts, quiet bars
    p = base[-1][1]
    # ---- 1. surge AND Marubozu with the day: fires
    up = base + ramp(at(11, 0), 240, p, p + 48) + quiet(at(11, 4), at(11, 20), p + 48, 0.0, 2.0)
    r = run('NIFTY', up)
    c = circuits(r)
    check('surge + Marubozu + day up: big-move alarm fires UP', bool(c) and c[0][1]['dir'] == 'UP')
    first = c[0][1] if c else {}
    med = r[-1][1]['median_5m_range']
    check('projected move ~1x the median 5-min range (was 4x+)', c and 0.9 * med <= first['move'] <= 1.6 * med + 1e-6)
    check('stretch target 1.8x of the projection', c and abs(first['stretch'] - first['move'] * 1.8) < 0.11)
    check('stop room = max(trigger, 1x median 5-min range)', c and abs(first['room'] - round(max(PF.trigger_for(p), med), 1)) < 0.11)
    pl = next((rr['plan'] for t, rr in r if rr['mode'] == 'circuit'), {})
    check('circuit plan stop uses that room', pl and abs(abs(pl['entry'] - pl['stop']) - first['room']) < 0.6)
    check('alarm recorded with reasons', r[-1][1]['alarm_stats']['n'] >= 1 and 'Marubozu' in ' '.join(r[-1][1]['alarms'][0]['why']))
    # ---- 2. surge alone (choppy, no Marubozu shape): no alarm
    # spike +62 in 30 s, then a pull-back to +40: the surge is there, but the candle closes far from its high
    chop = list(base) + ramp(at(11, 0), 30, p, p + 62) + ramp(at(11, 0, 30), 60, p + 62, p + 40)
    chop += quiet(at(11, 1, 30), at(11, 15), p + 40, 0.0, 2.0)
    check('surge without a Marubozu (long upper wick): no alarm', not circuits(run('NIFTY', chop)))
    # ---- 3. against the day: no alarm
    down_day = quiet(at(9, 15), at(11, 0), 23000, -0.6)              # ~ -63 by 11:00, still down after +48
    q = down_day[-1][1]
    against = down_day + ramp(at(11, 0), 240, q, q + 48) + quiet(at(11, 4), at(11, 15), q + 48, 0.0, 2.0)
    r3 = run('NIFTY', against)
    check('surge + Marubozu UP on a down day: no alarm', not circuits(r3))
    # ---- 4. first 10 minutes: no alarm
    opening = ramp(at(9, 15), 240, 23000, 23060) + quiet(at(9, 19), at(9, 24), 23060, 0.0, 2.0)
    check('no alarm in the first 10 minutes', not [x for x in circuits(run('NIFTY', opening)) if x[0] < at(9, 25)])
    # ---- 5. scoring
    st = dict(alarms=[dict(ts=at(11, 0).isoformat(), dir='UP', spot=100.0, expected=10.0, stretch=18.0, room=8.0,
                           why=[], fav=0.0, adv=0.0, outcome=None, resolved=None)])
    PF._score_alarms(st, at(11, 1), 104.0)
    PF._score_alarms(st, at(11, 2), 110.5)
    check('alarm RIGHT when the projection is reached first', st['alarms'][0]['outcome'] == 'RIGHT')
    st['alarms'].append(dict(ts=at(12, 0).isoformat(), dir='DOWN', spot=100.0, expected=10.0, stretch=18.0, room=8.0,
                             why=[], fav=0.0, adv=0.0, outcome=None, resolved=None))
    PF._score_alarms(st, at(12, 1), 108.5)
    check('alarm WRONG when the stop room comes first', st['alarms'][1]['outcome'] == 'WRONG')
    st['alarms'].append(dict(ts=at(13, 0).isoformat(), dir='UP', spot=100.0, expected=10.0, stretch=18.0, room=8.0,
                             why=[], fav=0.0, adv=0.0, outcome=None, resolved=None))
    PF._score_alarms(st, at(13, 16), 103.0)
    check('alarm EXPIRED after 15 minutes with neither', st['alarms'][2]['outcome'] == 'EXPIRED')
    s = PF.alarm_stats(st['alarms'])
    check('precision = right / scored', s['right'] == 1 and s['wrong'] == 1 and s['expired'] == 1 and abs(s['precision'] - 1 / 3) < 1e-3)
    # ---- 6. max SL keeps at least one median 5-min range of room
    msl = next((rr['max_sl'] for t, rr in reversed(r) if rr.get('max_sl')), None)
    spot_then = next((pp for (t, rr), (_, pp) in zip(reversed(r), reversed(up)) if rr.get('max_sl')), None)
    check('max SL never closer than one median 5-min range', msl is None or msl['hit'] or msl['room'] >= med - 0.6)
    # ---- 7. constant cost per second (incremental candles)
    day = quiet(at(9, 15), at(15, 15), 23000, 0.05)
    t1 = time.perf_counter()
    run('NIFTY', day)
    per = (time.perf_counter() - t1) / len(day) * 1000
    check('per-update cost stays small over a full day (< 1.5 ms)', per < 1.5)
    print('   per update %.3f ms over %d updates' % (per, len(day)))

    # ---- 8. PULSE on another index through the live thread (no hourly model, no meter), alarms saved
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'multi.db')

    class St:
        def __init__(self, p):
            self.is_live_tick, self.spot, self.chain, self.last_updated = True, p, None, None

    class Eng:
        states = {'NIFTY': St(23000.0), 'BANKNIFTY': St(52000.0)}

    hourly = type('Hourly', (), {})()
    hourly.engine, hourly.db = Eng(), db
    lf = LF.LiveForecaster(hourly)
    PF._S.clear()
    bn = [(t, v * 52000 / 23000) for t, v in up]
    for (t, v), (_, b) in zip(up[::3], bn[::3]):
        Eng.states['NIFTY'].spot, Eng.states['NIFTY'].last_updated = v, t
        Eng.states['BANKNIFTY'].spot, Eng.states['BANKNIFTY'].last_updated = b, t
        lf.tick(t)
    sb = LF.snapshot('BANKNIFTY', db=db, now=up[-1][0])
    check('BANKNIFTY gets a PULSE snapshot', sb.get('pulse') is not None and sb['index'] == 'BANKNIFTY')
    check('marked PULSE-only (no hourly/day model, no meter)', sb.get('models') == 'pulse_only' and sb.get('hour') is None)
    check('short price trace sent for the cards', 20 <= len(sb.get('tape') or []) <= 200)
    check('BANKNIFTY alarm fires and is scaled to its own price', any(a['dir'] == 'UP' for a in sb['pulse']['alarms'])
          and sb['pulse']['alarms'][0]['room'] >= PF.trigger_for(52000) - 1e-6)
    tl = up[-1][0] + timedelta(minutes=16)
    for st in Eng.states.values():
        st.last_updated = tl
    lf.tick(tl)
    n_saved = HF.conn(db).execute('SELECT COUNT(*) FROM pulse_alarms').fetchone()[0]
    check('scored alarms stored for the 30-day precision', n_saved >= 1)
    check('30-day precision attached to the snapshot', LF.snapshot('NIFTY', db=db, now=tl)['pulse'].get('alarm_stats_30d') is not None)
    check('index not enabled for PULSE is refused upstream list', 'FINNIFTY' not in LF.PULSE_INDICES)

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
