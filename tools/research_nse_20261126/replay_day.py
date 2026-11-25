import sqlite3, json, math, os, sys, time
from datetime import datetime
sys.path.insert(0, '/home/ubuntu/kadima-py')
from kadima import hourly_forecast_model as M
from kadima.config import IST
NDAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 250
POOL = 120
f = sqlite3.connect('file:/var/lib/kadima/forecast.db?mode=ro', uri=True)
params = json.loads(f.execute("select params from hourly_forecast_model where index_name='NIFTY'").fetchone()[0])
rows = []
for s, o, h, l, c in f.execute("select start_ts,open,high,low,close from history_candles where token='99926000' order by start_ts"):
    d = datetime.fromisoformat(s); rows.append((d if d.tzinfo else d.replace(tzinfo=IST), o, h, l, c))
days = [d for d in M.build_days(rows) if sum(c is not None for c in d.closes) >= 60]
pred = json.load(open('/home/ubuntu/kadima-research/nse_pred.json'))
z = params['band_z']; alpha = 2 * (1 - M.norm_cdf(z))
res = {'base': [], 'vix': []}
start = len(days) - NDAYS
for di in range(start, len(days)):
    lo_ = max(0, di - POOL); sub = days[lo_:di + 1]; dj = len(sub) - 1; d = sub[-1]
    tod = M.tod_profile(sub[:-1]); vix = pred.get(str(d.day), {}).get('vix')
    k = 1; n = M.BARS_PER_DAY - k
    if d.closes[0] is None or any(d.closes[k + j] is None for j in range(n)): continue
    anchor = d.closes[0]
    act = [d.closes[k + j] for j in range(n)]
    for v, iv in (('base', None), ('vix', vix)):
        fc = M.make_forecast(sub, dj, k, n, anchor, as_of=d.day, tod=tod, params=params, atm_iv=iv)
        if fc is None: continue
        sc = M.score(fc.points, fc.anchor, act, fc.direction, fc.sigma, fc.inputs['sd'][-1])
        U, L, y = fc.points[-1][2], fc.points[-1][3], act[-1]
        IS = (U - L) + (2 / alpha) * max(0, L - y) + (2 / alpha) * max(0, y - U)
        res[v].append(dict(day=str(d.day), cov=sc['coverage'], mae=sc['mae_pts'], end=abs(sc['end_error_pts']), band=(U - L) / 2, IS=IS, endin=L <= y <= U))
def summ(rs):
    n = len(rs)
    return f"n={n} cov={sum(r['cov'] for r in rs)/n:.3f} endIn={sum(r['endin'] for r in rs)/n:.3f} mae={sum(r['mae'] for r in rs)/n:.1f} end={sum(r['end'] for r in rs)/n:.1f} band={sum(r['band'] for r in rs)/n:.1f} IS={sum(r['IS'] for r in rs)/n:.1f}"
for v in res:
    rs = res[v]; h = len(rs) // 2
    print('DAY', v, summ(rs), '| h1', summ(rs[:h]), '| h2', summ(rs[h:]), '| last60', summ(rs[-60:]))
for v in res:
    print('LAST5', v, [(r['day'], round(r['band']), round(r['cov'], 2), round(r['IS'])) for r in res[v][-5:]])
