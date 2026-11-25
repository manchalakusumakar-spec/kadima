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
OB = 1.4
# variant: (iv source for 09:15, iv source for other slots, open mult)
VARS = {'base': (None, None, 1.0), 'live_now': (None, None, OB), 'vix_all': ('vix', 'vix', 1.0),
        'vix09': ('vix', None, 1.0), 'vix09_x1.4': ('vix', None, OB), 'vix_all_x1.4': ('vix', 'vix', OB)}
res = {v: {} for v in VARS}
t0 = time.time()
start = len(days) - NDAYS
def widen(pts, mult):
    return [(k, m, m + (u - m) * mult, m - (m - lo) * mult) for k, m, u, lo in pts]
for di in range(start, len(days)):
    lo_ = max(0, di - POOL); sub = days[lo_:di + 1]; dj = len(sub) - 1
    d = sub[-1]; tod = M.tod_profile(sub[:-1]); p = pred.get(str(d.day), {})
    for s_, e_ in M.SLOTS:
        i, n = M.bar_index(s_), M.slot_steps(s_, e_)
        if d.opens[i] is None or any(d.closes[i + k] is None for k in range(n)): continue
        slot = M.slot_label(s_)
        act = [d.closes[i + k] for k in range(n)]
        cache = {}
        for v, (s09, soth, mult) in VARS.items():
            src = s09 if slot == '09:15' else soth
            iv = p.get('vix') if src == 'vix' else None
            if iv not in cache:
                cache[iv] = M.make_forecast(sub, dj, i, n, d.opens[i], as_of=d.day, tod=tod, params=params, atm_iv=iv)
            fc = cache[iv]
            if fc is None: continue
            pts = widen(fc.points, mult) if (slot == '09:15' and mult != 1.0) else fc.points
            sc = M.score(pts, fc.anchor, act, fc.direction, fc.sigma, fc.inputs['sd'][-1])
            U, L, y = pts[-1][2], pts[-1][3], act[-1]
            IS = (U - L) + (2 / alpha) * max(0, L - y) + (2 / alpha) * max(0, y - U)
            res[v][(di, slot)] = dict(hit=sc['dir_hit'], cov=sc['coverage'], mae=sc['mae_pts'], end=abs(sc['end_error_pts']), band=(U - L) / 2, IS=IS, endin=L <= y <= U)
    if (di - start) % 25 == 0: print('progress', di - start, round(time.time() - t0), flush=True)
print('days', NDAYS, 'secs', round(time.time() - t0))
def summ(rs):
    n = len(rs)
    if not n: return '-'
    return f"n={n} hit={sum(r['hit'] for r in rs)/n:.3f} cov={sum(r['cov'] for r in rs)/n:.3f} endIn={sum(r['endin'] for r in rs)/n:.3f} mae={sum(r['mae'] for r in rs)/n:.1f} band={sum(r['band'] for r in rs)/n:.1f} IS={sum(r['IS'] for r in rs)/n:.1f}"
keys = sorted(res['base']); h = len(keys) // 2
for v in VARS:
    print('==', v, summ(list(res[v].values())))
    for slot in ('09:15', '10:15', '12:15', '15:15'):
        print('   ', slot, summ([r for (di, s), r in res[v].items() if s == slot]))
    print('   halves', summ([res[v][k] for k in keys[:h] if k in res[v]]), '|', summ([res[v][k] for k in keys[h:] if k in res[v]]))
    print('   last120d', summ([r for (di, s), r in res[v].items() if di >= len(days) - 120]))
