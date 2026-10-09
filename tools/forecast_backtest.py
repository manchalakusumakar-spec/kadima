"""Walk-forward backtest and calibration of the hourly forecast on real candles.

Replays every past session in kadima.db slot by slot, forecasting each slot
from data before it only, exactly as the live thread does (minus ATM IV and
Bull Shield, which are not stored historically). Prints how the model would
have scored and, with --save, writes the calibrated coefficients to
forecast.db so the live panel starts tuned instead of from defaults.

Run on the EC2 box, any time (it never touches the broker):

    cd ~/kadima-py && python tools/forecast_backtest.py --index NIFTY   (KADIMA venv active)
    (same command with --save to store the tuned values)

Options: --min-days N (history before the first scored day, default 10),
--db PATH (alternate candles database with the same `candles` table).
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kadima import hourly_forecast_model as M  # noqa: E402
from kadima.config import DB_PATH, INDICES, IST  # noqa: E402


def load(db, token):
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    rows = []
    for start, o, h, lo, cl in c.execute(
            'SELECT start_ts, open, high, low, close FROM candles WHERE token=? AND interval=5 '
            'AND closed=1 ORDER BY start_ts', (token,)):
        d = datetime.fromisoformat(start)
        rows.append((d if d.tzinfo else d.replace(tzinfo=IST), o, h, lo, cl))
    return rows


def run(days, min_days, params):
    out = []
    for di in range(min_days, len(days)):
        tod = M.tod_profile(days[:di])
        d = days[di]
        for start, end in M.SLOTS:
            i, n = M.bar_index(start), M.slot_steps(start, end)
            if d.opens[i] is None or any(d.closes[i + k] is None for k in range(n)):
                continue
            f = M.make_forecast(days, di, i, n, d.opens[i], as_of=d.day, tod=tod, params=params)
            if f is None:
                continue
            act = [d.closes[i + k] for k in range(n)]
            sc = M.score(f.points, f.anchor, act, f.direction, f.sigma, f.inputs['sd'][-1])
            inp = f.inputs
            zs = [abs(math.log(a / f.anchor) / f.sigma - inp['mean'][k]) / inp['sd'][k]
                  for k, a in enumerate(act) if inp['sd'][k] > 0]
            out.append(dict(day=d.day, slot=M.slot_label(start), f=f, sc=sc, sample=dict(
                x_reg=inp['x_reg'], x_analog=inp['x_analog'], x_tech=inp['x_tech'],
                p_raw=inp['p_raw'], y=math.log(act[-1] / f.anchor) / f.sigma,
                up=act[-1] > f.anchor, zs=zs)))
    return out


def report(res, title):
    print(f'\n== {title}: {len(res)} slots')
    if not res:
        return
    by = {}
    for r in res:
        by.setdefault(r['slot'], []).append(r)
    print(f"{'slot':>6} {'n':>4} {'dir hit':>8} {'non-flat hit':>13} {'in band':>8} {'avg err':>8} {'end err':>8}")
    for slot in sorted(by) + ['ALL']:
        rs = res if slot == 'ALL' else by[slot]
        nf = [r for r in rs if r['f'].direction != 'FLAT']
        print(f"{slot:>6} {len(rs):>4} "
              f"{sum(r['sc']['dir_hit'] for r in rs) / len(rs):>8.0%} "
              f"{(sum(r['sc']['dir_hit'] for r in nf) / len(nf)) if nf else 0:>13.0%} "
              f"{sum(r['sc']['coverage'] for r in rs) / len(rs):>8.0%} "
              f"{sum(r['sc']['mae_pts'] for r in rs) / len(rs):>8.1f} "
              f"{sum(abs(r['sc']['end_error_pts']) for r in rs) / len(rs):>8.1f}")
    # Probability calibration: does "UP 65%" come true ~65% of the time?
    buckets = {}
    for r in res:
        p = r['f'].prob_up
        b = min(9, int(p * 10))
        buckets.setdefault(b, []).append(1 if r['sample']['up'] else 0)
    print('  prob_up bucket -> observed up-rate:', ', '.join(
        f"{b/10:.1f}-{(b+1)/10:.1f}: {sum(v)/len(v):.0%} (n={len(v)})" for b, v in sorted(buckets.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index', default='NIFTY')
    ap.add_argument('--db', default=DB_PATH)
    ap.add_argument('--min-days', type=int, default=10)
    ap.add_argument('--save', action='store_true')
    a = ap.parse_args()
    token = INDICES[a.index].token
    days = M.build_days(load(a.db, token))
    full = [d for d in days if sum(c is not None for c in d.closes) >= 60]
    print(f'{a.index}: {len(days)} sessions in {a.db}, {len(full)} with >=60 of 75 bars')
    if len(full) <= a.min_days + 2:
        print('Not enough history to backtest yet.')
        return 1
    base = run(full, a.min_days, dict(M.DEFAULT_PARAMS))
    report(base, 'default parameters')
    # Fit on the first 2/3 of slots, check on the last 1/3 (out of sample).
    cut = int(len(base) * 2 / 3)
    fitted = M.fit_params([r['sample'] for r in base[:cut]])
    print('\nfitted on first 2/3:', json.dumps({k: fitted[k] for k in
          ('beta_reg', 'beta_analog', 'beta_tech', 'band_z', 'prob_c', 'n_samples')}))
    test_days = sorted({r['day'] for r in base[cut:]})
    start = next(i for i, d in enumerate(full) if d.day == test_days[0])
    report(run(full, start, fitted), 'out-of-sample with fitted parameters')
    report([r for r in base[cut:]], 'same out-of-sample slots with defaults')
    if a.save:
        final = M.fit_params([r['sample'] for r in base])
        from kadima import hourly_forecast as HF
        c = HF.conn()
        with c:
            for key in (a.index + ':prior', a.index):
                c.execute('INSERT OR REPLACE INTO hourly_forecast_model VALUES (?,?,?)',
                          (key, json.dumps(final), datetime.now(IST).isoformat()))
        print('\nsaved to', HF.DB_PATH, json.dumps({k: final[k] for k in
              ('beta_reg', 'beta_analog', 'beta_tech', 'band_z', 'prob_c', 'n_samples')}))
        print('Live slots now refit around these backtest values (prior worth '
              f'{M.MIN_SAMPLES} slots) instead of the built-in defaults.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
