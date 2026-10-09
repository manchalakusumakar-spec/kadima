"""Offline tests for the hourly forecast model. No broker, no network.

    python3 tools/test_hourly_forecast.py
"""
import math
import random
import sys
import time
from datetime import datetime, timedelta, date

sys.path.insert(0, '.')
from kadima import hourly_forecast_model as M

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def synthetic(n_days=40, seed=7, drift_persist=0.3, day_drift=0.0):
    """Random walk: mild bar momentum, optional per-day trend, open-heavy volatility."""
    rng = random.Random(seed)
    rows, price, d = [], 25000.0, date(2026, 8, 3)
    while n_days:
        if d.weekday() < 5:
            price *= math.exp(rng.gauss(0, 0.004))       # overnight gap
            prev = 0.0
            mu = rng.gauss(0, day_drift)
            for j in range(M.BARS_PER_DAY):
                vol = 0.0012 * (1.6 if j < 3 else 1.0)
                r = mu + drift_persist * prev + rng.gauss(0, vol)
                o = price
                c = o * math.exp(r)
                h, lo = max(o, c) * 1.0003, min(o, c) * 0.9997
                start = datetime(d.year, d.month, d.day, 9, 15) + timedelta(minutes=5 * j)
                rows.append((start, o, h, lo, c))
                price, prev = c, r
            n_days -= 1
        d += timedelta(days=1)
    return rows


def main():
    rows = synthetic()
    days = M.build_days(rows)
    check('40 sessions built', len(days) == 40)
    check('bars complete', all(d.complete_until(M.BARS_PER_DAY) for d in days))

    check('slot steps 12 for hour slots', M.slot_steps((9, 15), (10, 15)) == 12)
    check('last slot has 3 steps', M.slot_steps((15, 15), (15, 30)) == 3)
    from datetime import time as T
    check('slot_for 10:42 -> 10:15', M.slot_for(T(10, 42))[0] == (10, 15))
    check('slot_for 15:31 -> None', M.slot_for(T(15, 31)) is None)

    di = len(days) - 1
    tod = M.tod_profile(days[:di])
    check('tod profile measures the open as more volatile', tod[0] > tod[30])

    # Forecast every slot of the last day, timing the slowest.
    worst = 0.0
    for start, end in M.SLOTS:
        i, n = M.bar_index(start), M.slot_steps(start, end)
        t0 = time.perf_counter()
        f = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day,
                            tod=tod, atm_iv=12.0, shield_dir='CE', shield_score=70)
        worst = max(worst, time.perf_counter() - t0)
        ok = (f is not None and len(f.points) == n
              and all(p[3] < p[1] < p[2] for p in f.points)
              and 0.05 <= f.prob_up <= 0.95 and f.direction in ('UP', 'DOWN', 'FLAT'))
        check(f'forecast {M.slot_label(start)} well-formed', ok)
        if f:
            widths = [p[2] - p[3] for p in f.points]
            check(f'band widens over {M.slot_label(start)}', widths[-1] >= widths[0])
    check(f'forecast fast enough for a t3.micro ({worst*1000:.0f} ms worst)', worst < 3.0)

    # No look-ahead: changing today's bars at/after the slot must not change the forecast.
    i, n = M.bar_index((11, 15)), 12
    a = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day, tod=tod)
    for j in range(i, M.BARS_PER_DAY):
        days[di].closes[j] *= 1.05
    days[di]._rets = None
    b = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day, tod=tod)
    check('no look-ahead into the slot being forecast', a.points == b.points and a.prob_up == b.prob_up)
    days = M.build_days(rows)

    # Thin history: analogs off, model still produces a cautious forecast.
    f = M.make_forecast(days[:3], 2, 24, 12, days[2].opens[24], as_of=days[2].day)
    check('works without analog history', f is not None and f.n_eff == 0.0)

    # Scoring.
    pts = [(1, 100.0, 101.0, 99.0), (2, 100.5, 102.0, 99.0)]
    s = M.score(pts, 100.0, [100.2, 103.0], 'UP', 0.001, 1.0)
    check('score: direction hit', s['dir_hit'] is True)
    check('score: coverage counts the breakout', s['coverage'] == 0.5)
    check('score: no score before the slot completes', M.score(pts, 100.0, [100.2, None], 'UP', 0.001, 1.0) is None)

    # Calibration: with few samples the prior dominates, with many the data does.
    rng = random.Random(1)
    def samples(k, true_beta):
        out = []
        for _ in range(k):
            xa = rng.gauss(0, 0.5)
            y = true_beta * xa + rng.gauss(0, 1)
            out.append(dict(x_reg=rng.gauss(0, 0.5), x_analog=xa, x_tech=rng.gauss(0, 0.3), y=y,
                            p_raw=M.norm_cdf(0.5 * xa), up=y > 0,
                            zs=[abs(rng.gauss(0, 1.3)) for _ in range(12)]))
        return out
    few = M.fit_params(samples(5, 1.2))
    many = M.fit_params(samples(2000, 1.2))
    check('few samples stay near prior beta', abs(few['beta_analog'] - 0.3) < 0.35)
    check('many samples learn the true beta', abs(many['beta_analog'] - 1.2) < 0.15)
    check('band widens when errors are wider than modelled', many['band_z'] > 1.15)

    # Walk-forward skill check on trending-day data: direction hit rate above coin flip.
    rows = synthetic(n_days=60, seed=11, drift_persist=0.1, day_drift=0.0004)
    days = M.build_days(rows)
    hits = total = 0
    for di in range(20, len(days)):
        tod = M.tod_profile(days[:di])
        for start, end in M.SLOTS[2:6]:
            i, n = M.bar_index(start), M.slot_steps(start, end)
            f = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day, tod=tod)
            act = [days[di].closes[i + k] for k in range(n)]
            sc = M.score(f.points, f.anchor, act, f.direction, f.sigma, f.inputs['sd'][-1])
            if f.direction != 'FLAT':
                total += 1
                hits += sc['dir_hit']
    rate = hits / total if total else 0
    print(f'  synthetic walk-forward: {hits}/{total} directional hits ({rate:.0%})')
    check('model finds a real intraday trend when one exists', total > 20 and rate > 0.58)

    # Pure random walk: no edge to find, so probabilities must stay modest
    # and the band should hold roughly 68% of actual points.
    rows = synthetic(n_days=50, seed=5, drift_persist=0.0)
    days = M.build_days(rows)
    conf, cov = [], []
    for di in range(20, len(days)):
        tod = M.tod_profile(days[:di])
        for start, end in M.SLOTS[1:6]:
            i, n = M.bar_index(start), M.slot_steps(start, end)
            f = M.make_forecast(days, di, i, n, days[di].opens[i], as_of=days[di].day, tod=tod)
            act = [days[di].closes[i + k] for k in range(n)]
            sc = M.score(f.points, f.anchor, act, f.direction, f.sigma, f.inputs['sd'][-1])
            conf.append(abs(f.prob_up - 0.5))
            cov.append(sc['coverage'])
    mc, mv = sum(conf) / len(conf), sum(cov) / len(cov)
    print(f'  random walk: mean |p-0.5| = {mc:.3f}, band coverage = {mv:.0%}')
    check('no false confidence on a random walk', mc < 0.12)
    check('band coverage near 68% on a random walk', 0.55 <= mv <= 0.85)

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
