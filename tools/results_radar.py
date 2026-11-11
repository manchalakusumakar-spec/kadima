#!/usr/bin/env python3
"""KADIMA RESULTS RADAR v1 (Claude, 2026-11-11) -- display only, never places orders.

Celestial "Results Radar" tab: F&O stocks with quarterly results today / next trading day,
their profit and revenue change, live price, opening gap and an expected next-session range.

Sources (all read-only):
  * NSE event calendar          -> who reports today / next trading day
  * NSE integrated-filing feed  -> which results are declared, with the XBRL file
  * results XBRL                -> quarter profit + revenue (QoQ from the half-year in the same file)
  * last year's Sep-quarter XBRL (NSE financial-results feed) -> YoY, fetched once per company
  * KADIMA price_history.db     -> stock price, previous close, open, and ATM option prices
Forecast range = options-implied 1-day move from the ATM straddle (68% band); falls back to the
stock's own recent daily moves when no option prices are recorded. A direction lean is shown only
after results are declared AND price agrees with the profit change. Every forecast is frozen and
checked against the actual close (evening audit) so its hit rate is visible.

Writes only /var/lib/kadima/results_radar/ and /var/www/html/nb/results_radar.json.
Run from cron every 5 minutes (flock). Exits quietly on any network error, keeping the last output.
"""
import datetime as dt
import http.cookiejar
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PH = '/var/lib/kadima/price_history.db'
STATE_DIR = '/var/lib/kadima/results_radar'
STATE = STATE_DIR + '/state.json'
AUDIT = STATE_DIR + '/audit.json'
OUT = os.environ.get('RESULTS_RADAR_OUT', '/var/www/html/nb/results_radar.json')
HOLIDAYS = {'2026-10-02', '2026-10-20', '2026-11-10', '2026-11-24', '2026-12-25'}   # NSE weekday holidays (2026)
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
API = 'https://www.nseindia.com/api/'
CAL_TTL = 30 * 60
FILING_TTL = 4 * 60
OPEN, CLOSE = (9, 15), (15, 30)
AUDIT_AFTER = (15, 45)

_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
_last_call = [0.0]


def now():
    return dt.datetime.now(IST)


def log(*a):
    print(now().strftime('%Y-%m-%d %H:%M:%S'), *a, flush=True)


def fetch(url, as_json=True, timeout=20):
    wait = 0.6 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://www.nseindia.com/',
                                               'Accept': 'application/json, text/xml, */*'})
    try:
        with _op.open(req, timeout=timeout) as f:
            host = urllib.parse.urlparse(f.url).hostname or ''
            if not host.endswith('nseindia.com'):
                raise ValueError('unexpected host ' + host)
            body = f.read(8_000_000)
    finally:
        _last_call[0] = time.time()
    return json.loads(body) if as_json else body.decode('utf-8', 'replace')


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, separators=(',', ':'))
    os.replace(tmp, path)


def is_trading(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def next_trading(d):
    d += dt.timedelta(days=1)
    while not is_trading(d):
        d += dt.timedelta(days=1)
    return d


def prev_trading(d):
    d -= dt.timedelta(days=1)
    while not is_trading(d):
        d -= dt.timedelta(days=1)
    return d


def nse_date(d):
    return d.strftime('%d-%m-%Y')


def parse_nse_dt(s):
    for fmt in ('%d-%b-%Y %H:%M:%S', '%d-%b-%Y %H:%M', '%d-%b-%Y'):
        try:
            return dt.datetime.strptime(s.strip(), fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None


def session_of(t):
    hm = (t.hour, t.minute)
    return 'Pre-market' if hm < OPEN else ('Market hours' if hm < CLOSE else 'After close')


# ---------------------------------------------------------------- price store (read-only)
def ph():
    return sqlite3.connect('file:%s?mode=ro' % PH, uri=True, timeout=15)


def fno_symbols(c):
    day = c.execute("SELECT max(day) FROM price_days WHERE asset='STOCK'").fetchone()[0]
    return {r[0] for r in c.execute("SELECT DISTINCT symbol FROM price_days WHERE day=? AND asset='STOCK'", (day,))}


def stock_row(c, sym, upto):
    r = c.execute("SELECT day,payload FROM price_days WHERE asset='STOCK' AND symbol=? AND day<=? "
                  "ORDER BY day DESC LIMIT 1", (sym, upto)).fetchone()
    if not r:
        return None, None
    try:
        return r[0], json.loads(r[1])
    except ValueError:
        return r[0], None


def daily_moves(c, sym, upto, n=10):
    out = []
    for (pl,) in c.execute("SELECT payload FROM price_days WHERE asset='STOCK' AND symbol=? AND day<? "
                           "ORDER BY day DESC LIMIT ?", (sym, upto, n)):
        try:
            p = json.loads(pl)
            if p.get('previous_close') and p.get('last'):
                out.append(abs(p['last'] / p['previous_close'] - 1))
        except Exception:
            pass
    return out


def straddle(c, sym, day, spot, today):
    best = {}
    for (pl,) in c.execute("SELECT payload FROM price_days WHERE day=? AND asset='OPTION' AND underlying=?", (day, sym)):
        try:
            p = json.loads(pl)
            exp = dt.datetime.strptime(p['expiry'], '%d%b%Y').date()
            side = (p.get('side') or p.get('symbol', '')[-2:]).upper()[-2:]
            if exp < today or side not in ('CE', 'PE') or not p.get('last'):
                continue
            best.setdefault((exp, float(p['strike'])), {})[side] = float(p['last'])
        except Exception:
            continue
    pairs = [(k, v) for k, v in best.items() if 'CE' in v and 'PE' in v]
    if not pairs or not spot:
        return None
    exp0 = min(k[0] for k, _ in pairs)
    (exp, strike), v = min(((k, v) for k, v in pairs if k[0] == exp0), key=lambda kv: abs(kv[0][1] - spot))
    cal_days = max(1, (exp - today).days)
    tdays = max(1.0, cal_days * 5 / 7)
    sigma_exp = (v['CE'] + v['PE']) / 0.8                      # ATM straddle ~ 0.8 sigma
    sigma_1d = sigma_exp / math.sqrt(tdays)
    return {'pct': round(sigma_1d / spot * 100, 2), 'source': 'options', 'expiry': exp.isoformat(),
            'strike': strike, 'straddle': round(v['CE'] + v['PE'], 2)}


# ---------------------------------------------------------------- results XBRL
def xbrl_numbers(text):
    ctx = {}
    for m in re.finditer(r'<(?:xbrli:)?context id="([^"]+)">(.*?)</(?:xbrli:)?context>', text, re.S):
        body = m.group(2)
        if 'segment' in body or 'scenario' in body:
            continue
        sd = re.search(r'startDate>([^<]+)', body)
        ed = re.search(r'endDate>([^<]+)', body)
        if sd and ed:
            try:
                a = dt.date.fromisoformat(sd.group(1).strip()[:10])
                b = dt.date.fromisoformat(ed.group(1).strip()[:10])
                ctx[m.group(1)] = (a, b, round((b - a).days / 30.4))
            except ValueError:
                pass

    def val(tags, months):
        for tag in tags:
            for cref, v in re.findall(r'<[\w-]+:%s\b[^>]*contextRef="([^"]+)"[^>]*>\s*([-\d.Ee+]+)\s*<' % tag, text):
                if cref in ctx and ctx[cref][2] == months:
                    try:
                        return float(v), ctx[cref]
                    except ValueError:
                        pass
        return None, None

    ptags = ('ProfitOrLossAttributableToOwnersOfParent', 'ProfitLossForPeriod', 'ProfitLossForThePeriod',
             'NetProfitLossForThePeriod')
    rtags = ('RevenueFromOperations', 'TotalRevenueFromOperations', 'Income', 'TotalIncome')
    pq, cq = val(ptags, 3)
    rq, _ = val(rtags, 3)
    ph_, _ = val(ptags, 6)
    rh, _ = val(rtags, 6)
    return {'profit_q': pq, 'rev_q': rq, 'profit_h': ph_, 'rev_h': rh,
            'q_end': cq[1].isoformat() if cq else None}


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100, 1)


def rescale(prev, cur):
    """Older XBRL files may be in lakhs/crores; bring prev to cur's unit using revenue magnitude."""
    if not prev or not cur or not prev.get('rev_q') or not cur.get('rev_q'):
        return prev
    r = cur['rev_q'] / prev['rev_q']
    for f in (1e5, 1e7, 1e3, 1e6):
        if 0.05 < r / f < 20:
            return {k: (v * f if isinstance(v, float) else v) for k, v in prev.items()}
    return prev


def last_year_quarter(sym, q_end, consolidated):
    """Same quarter last year, from the NSE financial-results feed (fetched once per company)."""
    try:
        q = dt.date.fromisoformat(q_end)
    except Exception:
        return None
    ly = q.replace(year=q.year - 1)
    want_to = ly.strftime('%d-%b-%Y').upper()
    want_cons = 'Consolidated' if consolidated else 'Non-Consolidated'
    try:
        rows = fetch(API + 'corporates-financial-results?index=equities&symbol=%s&period=Quarterly'
                     % urllib.parse.quote(sym))
    except Exception as e:
        log('ly list', sym, e)
        return None
    cand = [r for r in rows if isinstance(r, dict) and r.get('symbol') == sym
            and (r.get('toDate') or '').upper() == want_to and r.get('xbrl')]
    cand.sort(key=lambda r: (r.get('consolidated') != want_cons,))
    for r in cand[:2]:
        try:
            n = xbrl_numbers(fetch(r['xbrl'], as_json=False))
            if n.get('profit_q') is not None or n.get('rev_q') is not None:
                n['basis'] = r.get('consolidated')
                return n
        except Exception as e:
            log('ly xbrl', sym, e)
    return None


# ---------------------------------------------------------------- main
def run():
    os.makedirs(STATE_DIR, exist_ok=True)
    t = now()
    today = t.date()
    if not is_trading(today):
        today = next_trading(today)                     # weekend/holiday: show the next session
    nxt = next_trading(today)
    prv = prev_trading(today)
    st = load(STATE, {})
    if st.get('day') != today.isoformat():
        st = {'day': today.isoformat(), 'filings': st.get('filings', {}), 'ly': st.get('ly', {}),
              'frozen': st.get('frozen', {})}
    errors = []

    c = ph()
    fno = fno_symbols(c)

    # 1. calendar ----------------------------------------------------------------------
    if time.time() - st.get('cal_at', 0) > CAL_TTL or not st.get('cal'):
        try:
            ev = fetch(API + 'event-calendar?index=equities&from_date=%s&to_date=%s' % (nse_date(today), nse_date(nxt)))
            st['cal'] = [{'symbol': e['symbol'], 'company': e.get('company'), 'date': e.get('date'),
                          'purpose': e.get('purpose')}
                         for e in ev if e.get('symbol') in fno and 'result' in (e.get('purpose') or '').lower()]
            st['cal_at'] = time.time()
        except Exception as e:
            errors.append('calendar: %s' % e)
    cal = st.get('cal') or []

    # 2. declared filings (previous session after close .. now) -----------------------------
    if time.time() - st.get('fil_at', 0) > FILING_TTL:
        try:
            d = fetch(API + 'integrated-filing-results?index=equities&from_date=%s&to_date=%s&size=500'
                      % (nse_date(prv), nse_date(today)))
            rows = d.get('data', []) if isinstance(d, dict) else d
            st['fil_list'] = [{'symbol': r['symbol'], 'company': r.get('smName') or r.get('cmName'),
                               'at': r.get('broadcast_Date'), 'cons': r.get('consolidated'),
                               'xbrl': r.get('xbrl'), 'seq': r.get('seq_Id'), 'qe': r.get('qe_Date')}
                              for r in rows if r.get('symbol') in fno and 'financ' in (r.get('type') or '').lower()]
            st['fil_at'] = time.time()
        except Exception as e:
            errors.append('filings: %s' % e)
    cutoff = dt.datetime.combine(prv, dt.time(*CLOSE), IST)
    chosen = {}
    for r in st.get('fil_list', []):
        at = parse_nse_dt(r.get('at') or '')
        if not at or at < cutoff:
            continue
        cur = chosen.get(r['symbol'])
        if cur is None or (cur['cons'] != 'Consolidated' and r['cons'] == 'Consolidated'):
            chosen[r['symbol']] = dict(r, at_dt=at)
    for sym, r in chosen.items():
        key = str(r.get('seq') or r.get('xbrl'))
        if key in st['filings'] or not r.get('xbrl'):
            continue
        try:
            n = xbrl_numbers(fetch(r['xbrl'], as_json=False))
            n['basis'] = r['cons']
            st['filings'][key] = n
        except Exception as e:
            errors.append('xbrl %s: %s' % (sym, e))
            continue
        if sym not in st['ly'] and n.get('q_end'):
            st['ly'][sym] = last_year_quarter(sym, n['q_end'], r['cons'] == 'Consolidated') or {}

    # 3. rows ----------------------------------------------------------------------------
    def build(sym, company, date_label, declared):
        day, p = stock_row(c, sym, today.isoformat())
        live = p if day == today.isoformat() else None
        prev_close = (live or {}).get('previous_close') or ((p or {}).get('last') if p else None)
        ltp = (live or {}).get('last')
        opening = (live or {}).get('opening_price')
        spot = ltp or prev_close
        row = {'symbol': sym, 'company': company, 'date': date_label, 'status': 'Due',
               'ltp': ltp, 'prev_close': prev_close,
               'chg': pct(ltp, prev_close), 'gap': pct(opening, prev_close) if opening else None}
        rng = straddle(c, sym, day, spot, today) if day else None
        if not rng:
            mv = daily_moves(c, sym, today.isoformat())
            if len(mv) >= 5:
                rng = {'pct': round(sum(mv) / len(mv) * 125, 2), 'source': 'history'}
        if rng and spot:
            rng['lo'] = round(spot * (1 - rng['pct'] / 100), 2)
            rng['hi'] = round(spot * (1 + rng['pct'] / 100), 2)
            rng['ref'] = spot
        row['range'] = rng
        if declared:
            n = st['filings'].get(str(declared.get('seq') or declared.get('xbrl'))) or {}
            ly = rescale(st['ly'].get(declared['symbol']) or {}, n)
            row.update({'status': 'Declared', 'declared_at': declared['at_dt'].strftime('%d-%b %H:%M'),
                        'session': session_of(declared['at_dt']), 'basis': n.get('basis'),
                        'profit_q_cr': round(n['profit_q'] / 1e7, 2) if n.get('profit_q') is not None else None,
                        'profit_yoy': pct(n.get('profit_q'), ly.get('profit_q')),
                        'rev_yoy': pct(n.get('rev_q'), ly.get('rev_q')),
                        'profit_qoq': pct(n.get('profit_q'),
                                          (n['profit_h'] - n['profit_q']) if n.get('profit_h') is not None and n.get('profit_q') is not None else None),
                        'rev_qoq': pct(n.get('rev_q'),
                                       (n['rev_h'] - n['rev_q']) if n.get('rev_h') is not None and n.get('rev_q') is not None else None)})
            if n.get('profit_q') is not None and ly.get('profit_q') is not None and (n['profit_q'] < 0) != (ly['profit_q'] < 0):
                row['turn'] = 'turned profit' if n['profit_q'] >= 0 else 'turned loss'
            # direction lean: only when the profit change and the price move agree
            pchg = row['profit_yoy'] if row['profit_yoy'] is not None else row['profit_qoq']
            after_decl = declared['at_dt'] <= t and (t.hour, t.minute) >= OPEN and live
            move = row['chg'] if after_decl else None
            if pchg is not None and move is not None and abs(move) >= 0.3:
                if pchg > 0 and move > 0:
                    row['lean'] = 'UP'
                elif pchg < 0 and move < 0:
                    row['lean'] = 'DOWN'
                else:
                    row['lean'] = 'MIXED'
            else:
                row['lean'] = 'WAIT'
        return row

    today_rows, tom_rows, seen = [], [], set()
    for e in cal:
        d = dt.datetime.strptime(e['date'], '%d-%b-%Y').date() if e.get('date') else None
        sym = e['symbol']
        if d == today:
            today_rows.append(build(sym, e.get('company'), 'Today', chosen.get(sym)))
            seen.add(sym)
        elif d == nxt:
            tom_rows.append(build(sym, e.get('company'), nxt.strftime('%d-%b'), None))
    for sym, r in chosen.items():                       # declared since last close but not on today's calendar
        if sym not in seen:
            row = build(sym, r.get('company'), 'Declared ' + r['at_dt'].strftime('%d-%b'), r)
            today_rows.append(row)
            seen.add(sym)
    order = {'Declared': 0, 'Due': 1}
    today_rows.sort(key=lambda r: (order.get(r['status'], 2), -(abs(r['chg'] or 0))))
    tom_rows.sort(key=lambda r: r['symbol'])

    # 4. freeze forecasts + evening audit --------------------------------------------------
    fz = st.setdefault('frozen', {})
    hm = (t.hour, t.minute)
    for r in today_rows:                                # results already known: freeze once price reacts
        k = today.isoformat() + ':' + r['symbol']
        if r['status'] == 'Declared' and r.get('range') and r.get('lean') in ('UP', 'DOWN', 'MIXED') and k not in fz:
            fz[k] = {'day': today.isoformat(), 'symbol': r['symbol'], 'ref': r['range']['ref'], 'lo': r['range']['lo'],
                     'hi': r['range']['hi'], 'lean': r['lean'], 'at': t.strftime('%H:%M')}
    if hm >= (15, 25) and hm < (15, 40):               # tomorrow's names + after-close reporters, judged next session
        for r in tom_rows + [x for x in today_rows if x['status'] == 'Due']:
            k = nxt.isoformat() + ':' + r['symbol']
            if r.get('range') and k not in fz:
                fz[k] = {'day': nxt.isoformat(), 'symbol': r['symbol'], 'ref': r['range']['ref'], 'lo': r['range']['lo'],
                         'hi': r['range']['hi'], 'lean': None, 'at': t.strftime('%d-%b %H:%M')}
    au = load(AUDIT, {'checked': []})
    if hm >= AUDIT_AFTER:
        done = {x['key'] for x in au['checked']}
        for k, f in list(fz.items()):
            if f['day'] != today.isoformat() or k in done:
                continue
            day, p = stock_row(c, f['symbol'], today.isoformat())
            if day != today.isoformat() or not p or not p.get('last'):
                continue
            close = p['last']
            hit_rng = f['lo'] <= close <= f['hi']
            hit_lean = None
            if f.get('lean') in ('UP', 'DOWN'):
                hit_lean = (close > f['ref']) == (f['lean'] == 'UP')
            au['checked'].append({'key': k, 'day': f['day'], 'symbol': f['symbol'], 'close': close, 'lo': f['lo'],
                                  'hi': f['hi'], 'lean': f.get('lean'), 'range_hit': hit_rng, 'lean_hit': hit_lean,
                                  'move_pct': pct(close, f['ref'])})
        au['checked'] = au['checked'][-400:]
        save(AUDIT, au)
    for k in [k for k, f in fz.items() if f['day'] < prv.isoformat()]:
        del fz[k]
    ch = au['checked']
    rn = [x for x in ch if x.get('range_hit') is not None]
    ln = [x for x in ch if x.get('lean_hit') is not None]
    audit = {'range_n': len(rn), 'range_hit': round(sum(x['range_hit'] for x in rn) / len(rn) * 100) if rn else None,
             'lean_n': len(ln), 'lean_hit': round(sum(x['lean_hit'] for x in ln) / len(ln) * 100) if ln else None,
             'recent': ch[-8:]}

    keep = set(str(r.get('seq') or r.get('xbrl')) for r in chosen.values())
    st['filings'] = {k: v for k, v in st['filings'].items() if k in keep} or st['filings']
    save(STATE, st)
    out = {'updated': t.strftime('%H:%M:%S'), 'day': today.isoformat(), 'next_day': nxt.isoformat(),
           'today': today_rows, 'tomorrow': tom_rows, 'audit': audit, 'errors': errors[-5:],
           'note': 'Display only. Profit = quarter net profit (consolidated where filed) vs same quarter last year '
                   '(QoQ from the same filing). Range = options-implied 1-day move (68%), or recent daily moves when no '
                   'options are recorded. Lean shown only after results, when price agrees with the profit change.'}
    save(OUT, out)
    log('ok today=%d tomorrow=%d declared=%d errors=%d' % (len(today_rows), len(tom_rows), len(chosen), len(errors)))


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:                            # never crash-loop; keep last good output
        log('FAILED', repr(exc))
        sys.exit(0)
