import sqlite3, json, math, os, sys, csv, io, statistics as st
from datetime import datetime
sys.path.insert(0, '/home/ubuntu/kadima-py')
from kadima import hourly_forecast_model as M
from kadima.config import IST
NSE = '/home/ubuntu/kadima-research/nse'
f = sqlite3.connect('file:/var/lib/kadima/forecast.db?mode=ro', uri=True)
rows = []
for s, o, h, l, c in f.execute("select start_ts,open,high,low,close from history_candles where token='99926000' order by start_ts"):
    d = datetime.fromisoformat(s); rows.append((d if d.tzinfo else d.replace(tzinfo=IST), o, h, l, c))
days = [d for d in M.build_days(rows) if sum(c is not None for c in d.closes) >= 60]
print('days', len(days), days[0].day, days[-1].day)
def num(x):
    try: return float(str(x).replace(',', '').strip())
    except Exception: return None
def rd(p):
    try: return open(p, errors='replace').read()
    except Exception: return None
nse = {}
for d in days:
    k = str(d.day); e = {}
    t = rd(f'{NSE}/idx_{k}.csv')
    if t:
        for r in csv.reader(io.StringIO(t)):
            if r and r[0].strip().lower() == 'india vix': e['vix_o'], e['vix_c'] = num(r[2]), num(r[5])
            if r and r[0].strip().lower() == 'nifty 50': e['n_o'], e['n_c'] = num(r[2]), num(r[5])
    t = rd(f'{NSE}/poi_{k}.csv')
    if t:
        rr = list(csv.reader(io.StringIO(t)))
        hdr = [h.strip().lower() for h in rr[1]] if len(rr) > 1 else []
        for r in rr[2:]:
            if r and r[0].strip().upper() in ('FII', 'CLIENT', 'DII', 'PRO'):
                v = {h: num(x) for h, x in zip(hdr, r)}
                who = r[0].strip().upper()
                try:
                    fl, fs = v['future index long'], v['future index short']
                    cl, pl, cs, ps = v['option index call long'], v['option index put long'], v['option index call short'], v['option index put short']
                    e[who + '_fut'] = (fl - fs) / (fl + fs)
                    e[who + '_opt'] = ((cl - cs) - (pl - ps)) / (cl + cs + pl + ps)
                except Exception: pass
    t = rd(f'{NSE}/ma_{k}.csv')
    if t:
        adv = dec = None
        for r in csv.reader(io.StringIO(t)):
            j = ','.join(r).upper()
            if 'ADVANCES' in j and adv is None: adv = next((num(x) for x in r[1:] if num(x) is not None), None)
            if 'DECLINES' in j and dec is None: dec = next((num(x) for x in r[1:] if num(x) is not None), None)
        if adv and dec: e['ad'] = adv / (adv + dec)
    nse[k] = e
cov = {key: sum(1 for e in nse.values() if key in e) for key in ('vix_c', 'FII_fut', 'CLIENT_fut', 'FII_opt', 'ad')}
print('coverage', cov)
# predictors for day di from day di-1 (and di-2)
def pred(di):
    a = nse.get(str(days[di - 1].day), {}); b = nse.get(str(days[di - 2].day), {}) if di >= 2 else {}
    p = {}
    if 'vix_c' in a: p['vix'] = a['vix_c']
    if 'vix_c' in a and 'vix_c' in b: p['dvix'] = math.log(a['vix_c'] / b['vix_c'])
    for w in ('FII', 'CLIENT', 'PRO', 'DII'):
        if w + '_fut' in a: p[w.lower() + '_fut'] = a[w + '_fut']
        if w + '_fut' in a and w + '_fut' in b: p['d_' + w.lower() + '_fut'] = a[w + '_fut'] - b[w + '_fut']
        if w + '_opt' in a: p[w.lower() + '_opt'] = a[w + '_opt']
    if 'ad' in a: p['ad'] = a['ad'] - 0.5
    pc = M.prev_close(days, di); o = days[di].opens[0]
    if pc and o: p['gap'] = math.log(o / pc)
    return p
def corr(xs, ys):
    n = len(xs)
    if n < 30: return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0: return None
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sx / sy
    return round(r, 3), round(r * math.sqrt((n - 2) / max(1e-9, 1 - r * r)), 2), n
# targets
T = {}
for di in range(2, len(days)):
    d = days[di]; cl = [c for c in d.closes if c is not None]
    if d.opens[0] is None or d.closes[0] is None: continue
    t = dict(day=math.log(cl[-1] / d.opens[0]), rest=math.log(cl[-1] / d.closes[0]), absday=abs(math.log(cl[-1] / d.opens[0])))
    hi = max(x for x in d.highs if x); lo = min(x for x in d.lows if x); t['range'] = math.log(hi / lo)
    for start, end in M.SLOTS:
        i, n = M.bar_index(start), M.slot_steps(start, end)
        if d.opens[i] and d.closes[i + n - 1]: t['s' + M.slot_label(start)] = math.log(d.closes[i + n - 1] / d.opens[i])
    T[di] = (pred(di), t)
keys = ['vix', 'dvix', 'fii_fut', 'd_fii_fut', 'client_fut', 'd_client_fut', 'pro_fut', 'fii_opt', 'client_opt', 'ad', 'gap']
tg = ['rest', 'day', 's09:15', 's10:15', 's12:15', 's14:15', 's15:15', 'range', 'absday']
print('CORR (r, t, n) predictor(day-1) vs target(day); halves for stability')
for kk in keys:
    line = [kk]
    for tt in tg:
        xs = [(p[kk], t[tt]) for di, (p, t) in T.items() if kk in p and tt in t]
        c = corr([a for a, _ in xs], [b for _, b in xs])
        h1 = corr([a for a, _ in xs[:len(xs)//2]], [b for _, b in xs[:len(xs)//2]]); h2 = corr([a for a, _ in xs[len(xs)//2:]], [b for _, b in xs[len(xs)//2:]])
        line.append(f"{tt}:{c[0] if c else '-'}/{c[1] if c else '-'}[{h1[1] if h1 else '-'},{h2[1] if h2 else '-'}]")
    print(' '.join(line))
# VIX as volatility predictor vs realized prev-day: which predicts today's range better
xs = [(p.get('vix'), t['range']) for di, (p, t) in T.items() if 'vix' in p]
print('VIX vs range', corr([a for a, _ in xs], [b for _, b in xs]))
pr = []
for di, (p, t) in T.items():
    d0 = days[di - 1]; hi = max(x for x in d0.highs if x); lo = min(x for x in d0.lows if x)
    pr.append((math.log(hi / lo), t['range']))
print('prevRange vs range', corr([a for a, _ in pr], [b for _, b in pr]))
json.dump({str(days[di].day): p for di, (p, t) in T.items()}, open('/home/ubuntu/kadima-research/nse_pred.json', 'w'))
