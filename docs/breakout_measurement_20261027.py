import warnings; warnings.filterwarnings("ignore")
import sqlite3, glob, json, os, sys, bisect, statistics as st
from datetime import datetime, timedelta
K = "/var/lib/kadima/kadima.db"; F = "/var/lib/kadima/forecast.db"
def con(db): return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
def em(s):  # epoch minutes (float) from ISO with tz
    return datetime.fromisoformat(s).timestamp() / 60.0
def hm(m): return datetime.utcfromtimestamp(m * 60 + 19800).strftime("%m-%d %H:%M")  # IST display (server tz agnostic)
def tod(m): d = datetime.utcfromtimestamp(m * 60 + 19800); return d.hour * 60 + d.minute
def day(m): return datetime.utcfromtimestamp(m * 60 + 19800).strftime("%Y-%m-%d")
OUT = {}
# ---------------- inventory extras ----------------
inv = {}
try:
    c = con("/var/lib/kadima/price_history.db")
    inv["price_history_tables"] = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
except Exception as e: inv["price_history_tables"] = str(e)
inv["live_inputs"] = sorted(os.listdir("/var/lib/kadima-live-inputs"))[:20] if os.path.isdir("/var/lib/kadima-live-inputs") else None
try:
    sm = json.load(open("/var/lib/kadima/scrip_master.json"))
    rows = sm if isinstance(sm, list) else sm.get("data", [])
    futs = [r for r in rows if isinstance(r, dict) and r.get("name") == "NIFTY" and r.get("instrumenttype") == "FUTIDX"]
    ftok = [str(r.get("token")) for r in futs]
    vix = [str(r.get("token")) for r in rows if isinstance(r, dict) and "VIX" in str(r.get("symbol", "")).upper()][:3]
except Exception as e:
    ftok, vix = [], []; inv["scrip_err"] = str(e)[:100]
kc = con(K)
if ftok:
    q = ",".join("'%s'" % t for t in ftok)
    inv["fut_candles"] = kc.execute(f"select token,interval,count(*) from candles where token in ({q}) group by 1,2").fetchall()
inv["vix_tokens"] = vix
if vix:
    q = ",".join("'%s'" % t for t in vix)
    inv["vix_candles"] = kc.execute(f"select token,interval,count(*) from candles where token in ({q}) group by 1,2").fetchall()
print("INV", json.dumps(inv, default=str)[:1500])

# ---------------- NIFTY 5-min history ----------------
fc = con(F)
rows = fc.execute("select start_ts,open,high,low,close from history_candles where token='99926000' order by start_ts").fetchall()
bars = [(em(r[0]), r[1], r[2], r[3], r[4]) for r in rows if r[1]]
days = sorted({day(b[0]) for b in bars})
use_days = set(days[-60:])
bars = [b for b in bars if day(b[0]) in use_days]
clean, dropped = [], 0
for b in bars:
    if clean and day(clean[-1][0]) == day(b[0]):
        pc = clean[-1][4]
        if abs(b[1] - pc) / pc > 0.006 or (b[2] - b[3]) / pc > 0.02 or not (b[3] <= b[1] <= b[2] and b[3] <= b[4] <= b[2]):
            dropped += 1; continue
    clean.append(b)
bars = clean
byday = {}
for b in bars: byday.setdefault(day(b[0]), []).append(b)
print("HIST days", len(byday), min(byday), max(byday), "bars", len(bars), "dropped_bad", dropped)
# ---------------- events ----------------
cand = []
for d, bs in byday.items():
    for i, b in enumerate(bs):
        if b[2] - b[3] >= 35: cand.append((b[0], 1 if b[4] >= b[1] else -1, round(b[2] - b[3], 1), "bar"))
        if i >= 2 and bs[i][0] - bs[i - 2][0] == 10:
            mv = bs[i][4] - bs[i - 2][1]
            if abs(mv) >= 40: cand.append((bs[i - 2][0], 1 if mv > 0 else -1, round(abs(mv), 1), "15m"))
cand.sort()
events = []
for e in cand:
    if events and e[0] - events[-1][0] < 30: continue
    events.append(e)
open_ev = [e for e in events if tod(e[0]) < 10 * 60 + 15]
print("EVENTS", len(events), "opening(09:15-10:15)", len(open_ev), "up", sum(e[1] > 0 for e in events), "median_size", st.median([e[2] for e in events]))
print("EV_LAST", [(hm(e[0]), e[1], e[2], e[3]) for e in events if day(e[0]) >= "2026-10-20"])
evt = [e[0] for e in events]
def near_event(t, w=30):
    i = bisect.bisect_left(evt, t - w); return i < len(evt) and evt[i] <= t + w
# quiet anchors: every bar start 09:45-15:10 with no event within 30 min
quiet = [b[0] for b in bars if 9 * 60 + 45 <= tod(b[0]) <= 15 * 60 + 10 and not near_event(b[0])]
print("QUIET anchors", len(quiet))

# ---------------- evaluation harness ----------------
LEADS = (15, 10, 5)
def evaluate(name, fn, cover_days, note=""):
    """fn(check_time) -> None (no data) | 0 (not firing) | 'U' (fires, no dir) | +1/-1 (fires with dir)"""
    ev = [e for e in events if day(e[0]) in cover_days]
    qa = [q for q in quiet if day(q) in cover_days]
    hit = dirok = dirn = cov = 0; leads = []
    for t, dr, sz, kind in ev:
        vals = [(L, fn(t - L)) for L in LEADS]
        if all(v is None for _, v in vals): continue
        cov += 1
        f = [(L, v) for L, v in vals if v not in (None, 0)]
        if f:
            hit += 1; leads.append(f[0][0])
            ds = [v for _, v in f if v in (1, -1)]
            if ds:
                dirn += 1; dirok += (ds[-1] == dr)
    qcov = qf = 0
    for q in qa:
        vals = [fn(q - L) for L in LEADS]
        if all(v is None for v in vals): continue
        qcov += 1; qf += any(v not in (None, 0) for v in vals)
    r = dict(days=len(cover_days), events=cov, hit=round(100 * hit / cov, 1) if cov else None,
             dir_ok=f"{dirok}/{dirn}" if dirn else "-", fa=round(100 * qf / qcov, 1) if qcov else None,
             quiet=qcov, med_lead=st.median(leads) if leads else None, note=note)
    if r["hit"] is not None and r["fa"]: r["lift"] = round(r["hit"] / r["fa"], 2)
    OUT[name] = r; print("RES", name, json.dumps(r))
    return r

ALL = set(byday)
# ---------------- d. volatility squeeze (60 days) ----------------
sq = {}  # bar-close time -> fire flag
for d, bs in byday.items():
    hist = []
    for i in range(len(bs)):
        if i < 7: continue
        r6 = st.mean(b[2] - b[3] for b in bs[i - 5:i + 1])
        base = [b[2] - b[3] for b in bs[max(0, i - 23):i + 1]]
        ratio = r6 / st.mean(base)
        hist.append(ratio)
        if len(hist) >= 6:
            thr = sorted(hist)[max(0, int(0.2 * len(hist)) - 1)]
            sq[bs[i][0] + 5] = 'U' if ratio <= thr else 0
def f_sq(c):
    v = sq.get(c); return v
evaluate("d_squeeze", f_sq, ALL, "6-bar mean range / 24-bar mean range <= day-so-far P20 (needs 7 bars, so none before ~09:50)")

# ---------------- spot 1-min (kadima.db, ~6 days) ----------------
def mins(token):
    m = {}
    for s, cl in kc.execute("select start_ts, close from candles where token=? and interval=1 order by start_ts", (token,)):
        m[em(s)] = cl
    return m
n1 = mins("99926000"); b1 = mins("99926009")
n1k = sorted(n1)
def spot(c):
    i = bisect.bisect_right(n1k, c - 1) - 1  # last 1-min bar closed by c
    if i < 0 or c - n1k[i] > 6: return None
    return n1[n1k[i]]
live_days = {day(t) for t in n1k}
print("LIVE 1-min days", sorted(live_days))

# ---------------- f. BANKNIFTY lead ----------------
def ret(m, c, w=5):
    a, b = m.get(c - 1), m.get(c - 1 - w)
    if a is None or b is None: return None
    return (a - b) / b * 100
diffs = [abs(ret(b1, c) - ret(n1, c)) for c in n1k if ret(b1, c) is not None and ret(n1, c) is not None]
BNT = sorted(diffs)[int(0.9 * len(diffs))] if diffs else 1
def f_bn(c):
    rb, rn = ret(b1, c), ret(n1, c)
    if rb is None or rn is None: return None
    dd = rb - rn
    return (1 if dd > 0 else -1) if abs(dd) >= BNT else 0
evaluate("f_banknifty_lead", f_bn, live_days, f"|BN 5m ret% - NIFTY 5m ret%| >= P90 ({BNT:.3f}%), dir = sign(BN-NIFTY)")

# ---------------- OI snapshots ----------------
snaps = {}
for ts, strike, ot, ltp, oi in kc.execute("select ts,strike,opt_type,ltp,oi from oi_snapshots where index_name='NIFTY' order by ts"):
    snaps.setdefault(em(ts), {})[(strike, ot)] = (ltp, oi)
sk = sorted(snaps)
def snap_at(c, maxage=2.0):
    i = bisect.bisect_right(sk, c) - 1
    if i < 0 or c - sk[i] > maxage: return None, None
    return sk[i], snaps[sk[i]]
def walls(s, sp):
    ce = [(v[1] or 0, k[0]) for k, v in s.items() if k[1] == "CE" and k[0] >= sp]
    pe = [(v[1] or 0, k[0]) for k, v in s.items() if k[1] == "PE" and k[0] <= sp]
    cea = max(((v[1] or 0, k[0]) for k, v in s.items() if k[1] == "CE"), default=None)
    pea = max(((v[1] or 0, k[0]) for k, v in s.items() if k[1] == "PE"), default=None)
    return (max(ce) if ce else None), (max(pe) if pe else None), cea, pea
def f_wall_unwind(c):
    sp = spot(c); t, s = snap_at(c)
    if sp is None or s is None: return None
    t0, s0 = snap_at(c - 10, 2.5)
    if s0 is None: return None
    ce, pe, _, _ = walls(s, sp)
    out = 0
    if ce:
        o0 = (s0.get((ce[1], "CE")) or (0, 0))[1] or 0
        if o0 and (o0 - ce[0]) / o0 >= 0.15: out = 1
    if pe:
        o0 = (s0.get((pe[1], "PE")) or (0, 0))[1] or 0
        if o0 and (o0 - pe[0]) / o0 >= 0.15: out = -1 if out == 0 else 'U'
    return out
def f_wall_pin(c):
    sp = spot(c); t, s = snap_at(c)
    if sp is None or s is None: return None
    _, _, cea, pea = walls(s, sp)
    if cea and pea and cea[1] == pea[1] and abs(sp - cea[1]) / sp <= 0.002: return 'U'
    return 0
oi_days = {day(t) for t in sk} & live_days
evaluate("a1_wall_unwind15", f_wall_unwind, oi_days, "nearest CE wall above / PE wall below loses >=15% OI in 10 min; CE unwind=up, PE unwind=down")
evaluate("a2_wall_pin", f_wall_pin, oi_days, "max-OI CE strike == max-OI PE strike and spot within 0.2% of it (direction-less)")

# ---------------- c. option premium lead ----------------
def prem_res(c):
    sp0 = spot(c - 5); sp1 = spot(c)
    t0, s0 = snap_at(c - 5); t1, s1 = snap_at(c)
    if None in (sp0, sp1, s0, s1): return None
    k = min({kk[0] for kk in s0}, key=lambda x: abs(x - sp0))
    try:
        dce = s1[(k, "CE")][0] - s0[(k, "CE")][0]; dpe = s1[(k, "PE")][0] - s0[(k, "PE")][0]
    except (KeyError, TypeError): return None
    return (dce - dpe) - (sp1 - sp0)
resid = [abs(r) for r in (prem_res(c) for c in sk[::4]) if r is not None]
PRT = sorted(resid)[int(0.9 * len(resid))] if resid else 10
def f_prem(c):
    r = prem_res(c)
    if r is None: return None
    return (1 if r > 0 else -1) if abs(r) >= PRT else 0
evaluate("c_premium_lead", f_prem, oi_days, f"ATM (dCE-dPE) - dSpot over 5 min, |resid| >= P90 ({PRT:.1f} pts), dir = sign")

# ---------------- e. breadth ----------------
br = {}
for ts, b, mv in fc.execute("select ts,breadth,move_pct from hourly_forecast_breadth where index_name='NIFTY' order by ts"):
    br[em(ts)] = (b, mv)
bk = sorted(br)
def br_at(c, maxage=3):
    i = bisect.bisect_right(bk, c) - 1
    if i < 0 or c - bk[i] > maxage: return None
    return br[bk[i]][0]
bd = [abs(br_at(c) - br_at(c - 10)) for c in bk if br_at(c) is not None and br_at(c - 10) is not None]
BRT = sorted(bd)[int(0.9 * len(bd))] if bd else 0.3
print("BREADTH rows", len(bk), "range", hm(bk[0]) if bk else None, hm(bk[-1]) if bk else None, "P90|d10|", round(BRT, 3))
def f_br(c):
    a, b = br_at(c), br_at(c - 10); sp, sp0 = spot(c), spot(c - 10)
    if a is None or b is None: return None
    dd = a - b
    return (1 if dd > 0 else -1) if abs(dd) >= BRT else 0
def f_brdiv(c):
    a, b = br_at(c), br_at(c - 10); sp, sp0 = spot(c), spot(c - 10)
    if a is None or b is None or sp is None or sp0 is None: return None
    dd = a - b; ds = (sp - sp0) / sp0 * 100
    if abs(dd) >= BRT * 0.7 and abs(ds) < 0.05: return 1 if dd > 0 else -1
    return 0
br_days = {day(t) for t in bk}
evaluate("e_breadth_jump", f_br, br_days, f"|breadth change over 10 min| >= P90 ({BRT:.3f}), dir = sign")
evaluate("e_breadth_divergence", f_brdiv, br_days & live_days, "breadth moves >=0.7*P90 in 10 min while NIFTY moves <0.05%")

# ---------------- b. gamma (audit) ----------------
gs, gnd = [], []
for f in sorted(glob.glob("/var/lib/kadima/audit/*.jsonl")):
    with open(f, errors="ignore") as fh:
        for line in fh:
            if '"gamma_' not in line or 'NIFTY"' not in line: continue
            if '"event":"gamma_score"' in line or '"event":"gamma_activity_no_direction"' in line:
                try: r = json.loads(line)
                except Exception: continue
                if r.get("name") != "NIFTY": continue
                t = em(r["ts"])
                if r["event"] == "gamma_score":
                    gs.append((t, r.get("score") or 0, 1 if r.get("direction") == "CE" else -1))
                else: gnd.append((t, r.get("raw_score") or 0))
gs.sort(); gnd.sort(); gk = [g[0] for g in gs]; gndk = [g[0] for g in gnd]
g_days = {day(t) for t in gk} | {day(t) for t in gndk}
print("GAMMA events", len(gs), "nodir", len(gnd), "days", len(g_days), min(g_days) if g_days else None, "score>=50:", sum(g[1] >= 50 for g in gs))
def f_gamma(c, thr=50):
    i, j = bisect.bisect_right(gk, c - 5), bisect.bisect_right(gk, c)
    k0, k1 = bisect.bisect_right(gndk, c - 5), bisect.bisect_right(gndk, c)
    if i == j and k0 == k1: return None if not (day(c) in g_days) else 0
    w = [g for g in gs[i:j] if g[1] >= thr]
    if w: return max(w, key=lambda g: g[1])[2]
    return 0
def f_gamma_any(c):
    v = f_gamma(c, 40)
    if v in (1, -1): return v
    k0, k1 = bisect.bisect_right(gndk, c - 5), bisect.bisect_right(gndk, c)
    if any(g[1] >= 40 for g in gnd[k0:k1]): return 'U'
    return v
gd = g_days & ALL
evaluate("b_gamma50", f_gamma, gd, "gamma_score>=50 (NIFTY) in the 5 min before check; dir CE=up PE=down")
evaluate("b_gamma40_or_nodir40", f_gamma_any, gd, "looser: directional score>=40 or no-direction raw_score>=40")

# ---------------- combined ----------------
comp = {"sq": f_sq, "bn": f_bn, "unw": f_wall_unwind, "pin": f_wall_pin, "prem": f_prem, "br": f_br, "g": f_gamma}
def mk_comb(n):
    def f(c):
        vs = [fn(c) for fn in comp.values()]
        if all(v is None for v in vs): return None
        fired = [v for v in vs if v not in (None, 0)]
        if len(fired) >= n:
            d = sum(v for v in fired if v in (1, -1))
            return (1 if d > 0 else -1) if d else 'U'
        return 0
    return f
cd = oi_days & br_days & gd
print("COMBINED days", sorted(cd))
for n in (1, 2, 3): evaluate(f"combo_ge{n}", mk_comb(n), cd, f">= {n} of {list(comp)} firing at a check point")


print("PRECISION (all 5-min check points 09:30-15:10 on covered days; 'next15' = an event starts within 15 min after the check)")
def precision(name, fn, cover):
    n = f = fh = base = 0; dirok = dirn = 0
    for b in bars:
        c = b[0]
        if day(c) not in cover or not (9*60+30 <= tod(c) <= 15*60+10): continue
        v = fn(c)
        if v is None: continue
        i = bisect.bisect_right(evt, c); nxt = i < len(evt) and evt[i] <= c + 15
        n += 1; base += nxt
        if v not in (0,):
            f += 1; fh += nxt
            if nxt and v in (1, -1):
                e = events[i]; dirn += 1; dirok += (e[1] == v)
    if n:
        print("PREC", name, json.dumps(dict(checks=n, base_rate=round(100*base/n,1), fires=f, fire_pct=round(100*f/n,1),
              precision=round(100*fh/f,1) if f else None, dir_ok=f"{dirok}/{dirn}")))
precision("d_squeeze", f_sq, ALL)
precision("f_banknifty_lead", f_bn, live_days)
precision("a1_wall_unwind15", f_wall_unwind, oi_days)
precision("a2_wall_pin", f_wall_pin, oi_days)
precision("c_premium_lead", f_prem, oi_days)
precision("e_breadth_jump", f_br, br_days)
precision("e_breadth_divergence", f_brdiv, br_days & live_days)
precision("b_gamma50", f_gamma, gd)
precision("b_gamma40_or_nodir40", f_gamma_any, gd)
for n in (1, 2, 3): precision(f"combo_ge{n}", mk_comb(n), cd)
# event-size buckets for squeeze (60d)
big = [e for e in events if e[2] >= 60]
print("BIG>=60 events", len(big), "opening", sum(tod(e[0]) < 615 for e in big))
# inventory: price_history + futures basis on 27-Oct
ph = con("/var/lib/kadima/price_history.db")
for t in ("price_days", "price_minutes", "price_writers"):
    try:
        print("PH", t, ph.execute(f"select sql from sqlite_master where name='{t}'").fetchone()[0][:300].replace("\n"," "))
        print("PH cnt", t, ph.execute(f"select count(*) from {t}").fetchone())
    except Exception as e: print("PH err", t, e)
try:
    cols = [r[1] for r in ph.execute("pragma table_info(price_minutes)")]
    kcol = [c for c in cols if c in ("symbol", "token", "name", "key", "instrument")]
    tcol = [c for c in cols if c in ("ts", "minute", "start_ts", "t")]
    if kcol:
        print("PH syms", ph.execute(f"select {kcol[0]},count(*)"+(f",min({tcol[0]}),max({tcol[0]})" if tcol else "")+f" from price_minutes where {kcol[0]} like '%VIX%' or {kcol[0]} like '%GIFT%' or {kcol[0]} like '%NIFTY%' or {kcol[0]} like '9992600%' or {kcol[0]} like '%SGX%' group by 1 limit 20").fetchall())
except Exception as e: print("PH sym err", e)
for tok in ("48704", "61471"):
    r = kc.execute("select min(start_ts),max(start_ts),count(*) from candles where token=? and interval=1", (tok,)).fetchone(); print("FUT", tok, r)
fut = {em(s): c for s, c in kc.execute("select start_ts, close from candles where token='61471' and interval=1")}
fut2 = {em(s): c for s, c in kc.execute("select start_ts, close from candles where token='48704' and interval=1")}
if fut and fut2 and st.mean(fut.values()) > st.mean(fut2.values()): fut = fut2  # use near month (lower premium)
bas = [(t, fut[t] - n1[t]) for t in sorted(fut) if t in n1]
for t, bv in bas:
    if 14*60 <= tod(t) <= 14*60+45 and int(tod(t)) % 5 == 0: print("BASIS", hm(t), round(bv, 1), "fut", fut[t], "spot", n1[t])
