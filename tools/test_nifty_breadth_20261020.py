"""Offline tests for the NIFTY 50 breadth shadow recorder. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_nifty_breadth_20261020.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import nifty_breadth as B                                 # noqa: E402
from kadima import hourly_forecast as HF                             # noqa: E402
from kadima.config import IST                                        # noqa: E402

FAILS = []
NAMES = [f'S{i:02d}' for i in range(50)]


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def at(h, m, s=0):
    return datetime(2026, 10, 21, h, m, s, tzinfo=IST)


def weights_file(as_of='2026-10-01', n=50):
    p = os.path.join(os.environ['KADIMA_DATA_DIR'], f'w_{as_of}_{n}.json')
    with open(p, 'w') as f:
        json.dump({'as_of': as_of, 'weights': {NAMES[i]: 50 - i for i in range(n)}}, f)
    return p


def snap(now, prices, age=10):
    t = (now - timedelta(seconds=age)).isoformat()
    return {'ts': t, 'prices': {k: {'ltp': v, 'received_at': t} for k, v in prices.items()}}


def reset():
    B._state.update(last=None, base_key=None, base={})


def main():
    w = weights_file()
    db = sqlite3.connect(':memory:')
    base = {n: 100.0 for n in NAMES}

    # Pure computation.
    W, _ = B.load_weights(at(10, 0), w)
    up = {n: (101.0 if i < 25 else 99.0) for i, n in enumerate(NAMES)}
    r = B.compute(up, base, W)
    check('heavy names up, light names down: breadth positive', r['breadth'] > 0 and r['top10_vote'] == 1.0)
    check('all down: breadth -1', B.compute({n: 99.0 for n in NAMES}, base, W)['breadth'] == -1.0)
    check('fewer than 45 fresh names: no value', B.compute({n: 101.0 for n in NAMES[:40]}, base, W) is None)

    # Weights gates.
    check('stale weights file refused', B.load_weights(at(10, 0), weights_file('2026-08-01'))[0] is None)
    check('implausible weights count refused', B.load_weights(at(10, 0), weights_file('2026-10-01', 30))[0] is None)
    check('missing weights file refused', B.load_weights(at(10, 0), '/nonexistent.json')[0] is None)

    # observe(): baseline first, then one row a minute, fail closed on stale data.
    reset()
    check('first call sets the slot baseline, records nothing',
          B.observe(db, 'NIFTY', '10:15', at(10, 15, 5), snapshot=snap(at(10, 15, 5), base), weights_path=w) is None
          and B._state['base_key'] == ('2026-10-21', '10:15'))
    row = B.observe(db, 'NIFTY', '10:15', at(10, 16, 5), snapshot=snap(at(10, 16, 5), up), weights_path=w)
    check('next minute records a row', row is not None and
          db.execute('SELECT COUNT(*) FROM hourly_forecast_breadth').fetchone()[0] == 1)
    check('rate-limited to once a minute',
          B.observe(db, 'NIFTY', '10:15', at(10, 16, 30), snapshot=snap(at(10, 16, 30), up), weights_path=w) is None)
    check('stale snapshot records nothing',
          B.observe(db, 'NIFTY', '10:15', at(10, 18), snapshot=snap(at(10, 18), up, age=400), weights_path=w) is None)
    check('other indices ignored',
          B.observe(db, 'BANKNIFTY', '10:15', at(10, 20), snapshot=snap(at(10, 20), up), weights_path=w) is None)
    reset()
    B.observe(db, 'NIFTY', '11:15', at(11, 15, 5), snapshot=snap(at(11, 15, 5), {n: 1.0 for n in NAMES[:30]}),
              weights_path=w)
    check('incomplete baseline is not accepted', B._state['base_key'] is None)
    check('bad snapshot never raises', B.observe(db, 'NIFTY', '12:15', at(12, 16), snapshot={'ts': 'x'},
                                                weights_path=w) is None)
    with open(B.MODE_PATH, 'w') as f:
        json.dump({'mode': 'off'}, f)
    reset()
    check('mode off records nothing', B.observe(db, 'NIFTY', '13:15', at(13, 15, 5),
                                                snapshot=snap(at(13, 15, 5), base), weights_path=w) is None
          and B._state['base_key'] is None)
    with open(B.MODE_PATH, 'w') as f:
        json.dump({'mode': 'live'}, f)
    check('"live" is treated as shadow in this build', B.mode() == 'shadow')
    os.remove(B.MODE_PATH)

    # The forecaster hook never changes a forecast: tick() with no engine is a no-op, and the
    # hook only writes to its own table.
    f = HF.HourlyForecaster(None, db=os.path.join(os.environ['KADIMA_DATA_DIR'], 'fc.db'))
    f.tick(at(10, 30))
    tables = {r[0] for r in HF.conn(f.db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check('forecaster tick runs with the hook present', 'hourly_forecasts' in tables)

    # Report runs on an empty or populated table.
    c = HF.conn(f.db)
    c.executescript(B.SCHEMA)
    check('shadow report runs', B.shadow_report(c)['samples'] == 0)

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
