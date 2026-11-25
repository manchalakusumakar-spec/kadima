"""NSE daily market data for the forecaster (26-Nov-2026, trader request: "use the data from NSE").

NSE's live website APIs refuse cloud servers (HTTP 403), but NSE's public archives work. After
each session this module stores, per trading day, in forecast.db table ``nse_daily``:

  vix_open/high/low/close    India VIX            (content/indices/ind_close_all_DDMMYYYY.csv)
  nifty_open/high/low/close  NIFTY 50 official    (same file)
  <who>_fut_net, <who>_opt_net  participant-wise index futures / options positioning for
                             FII, DII, PRO, CLIENT (content/nsccl/fao_participant_oi_DDMMYYYY.csv)
  adv, dec                   NSE advances / declines (archives/equities/mkt/MAddmmyy.csv)

What feeds the forecast: only India VIX, and only for the 09:15 hourly slot, which is made at
09:15:04 before any option-chain IV exists, scaled by VIX_IV_SCALE (0.9), in place of the fixed
1.4x open widening. Tested 26-Nov: VIX predicts the day's range better than the previous day's
range (r 0.41 vs 0.32, 525 sessions); replayed, the 09:15 slot scored better (interval score
-4% on 250 sessions, both halves; -9% on 29 sessions through the live code) at the same hit rate
with a ~10% narrower band. Later slots keep the ATM IV and the day forecast is unchanged (VIX
made their bands too wide in the same replay).
Participant OI, FII and advances/declines are stored for research only: none of them predicted
direction reliably on the same 525 sessions, so they do not touch the forecast.

Read-only towards trading: no orders, no engine settings. Network errors never raise.

    python -m kadima.nse_daily                  # fetch the last 5 sessions that are missing
    python -m kadima.nse_daily --backfill 600   # fetch up to 600 past weekdays
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

from .config import DATA_DIR, IST

DB_PATH = os.path.join(DATA_DIR, 'forecast.db')
MACRO_SNAPSHOT = os.path.expanduser('~/kadima-py/state/macro_snapshot.json')
HOSTS = ('https://nsearchives.nseindia.com', 'https://archives.nseindia.com')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
VIX_FALLBACK = os.environ.get('KADIMA_VIX_FALLBACK', '1') != '0'
LIVE_VIX_FRESH_SEC = 600        # live VIX older than this is not used
STORED_VIX_MAX_AGE_DAYS = 7     # a stored close older than this is not used
VIX_MIN, VIX_MAX = 5.0, 90.0
# VIX runs above the realised intraday volatility (variance premium): replayed on 250 sessions and
# on 29 sessions through the live code, 0.9 x VIX kept the 09:15 band's hit rate and scored best.
VIX_IV_SCALE = float(os.environ.get('KADIMA_VIX_IV_SCALE', '0.9'))

SCHEMA = ('CREATE TABLE IF NOT EXISTS nse_daily (day TEXT NOT NULL, key TEXT NOT NULL, value REAL, '
          'source TEXT, fetched_at TEXT, PRIMARY KEY (day, key))')


def conn(db=None):
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.execute(SCHEMA)
    return c


def _num(x):
    try:
        v = float(str(x).replace(',', '').strip())
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- parsers (pure, tested)
def parse_index_close(text):
    out = {}
    for r in csv.reader(io.StringIO(text or '')):
        if len(r) < 6:
            continue
        name = r[0].strip().lower()
        prefix = 'vix' if name == 'india vix' else 'nifty' if name == 'nifty 50' else None
        if prefix:
            for j, k in ((2, 'open'), (3, 'high'), (4, 'low'), (5, 'close')):
                v = _num(r[j])
                if v is not None:
                    out[f'{prefix}_{k}'] = v
    return out


def parse_participant_oi(text):
    rows = list(csv.reader(io.StringIO(text or '')))
    hdr_i = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == 'client type'), None)
    if hdr_i is None:
        return {}
    hdr = [h.strip().lower() for h in rows[hdr_i]]
    out = {}
    for r in rows[hdr_i + 1:]:
        who = r[0].strip().upper() if r else ''
        if who not in ('FII', 'DII', 'PRO', 'CLIENT'):
            continue
        v = {h: _num(x) for h, x in zip(hdr, r)}
        try:
            fl, fs = v['future index long'], v['future index short']
            cl, pl = v['option index call long'], v['option index put long']
            cs, ps = v['option index call short'], v['option index put short']
            if fl + fs > 0:
                out[f'{who.lower()}_fut_net'] = round((fl - fs) / (fl + fs), 6)
            if cl + cs + pl + ps > 0:
                out[f'{who.lower()}_opt_net'] = round(((cl - cs) - (pl - ps)) / (cl + cs + pl + ps), 6)
        except (KeyError, TypeError):
            continue
    return out


def parse_market_activity(text):
    adv = dec = None
    for r in csv.reader(io.StringIO(text or '')):
        j = ','.join(r).upper()
        nums = [n for n in (_num(x) for x in r[1:]) if n is not None]
        if adv is None and 'ADVANCES' in j and nums:
            adv = nums[0]
        if dec is None and 'DECLINES' in j and nums:
            dec = nums[0]
    return {k: v for k, v in (('adv', adv), ('dec', dec)) if v is not None}


# ---------------------------------------------------------------- fetch
def _get(path, timeout=20):
    for host in HOSTS:
        try:
            r = subprocess.run(['curl', '-s', '-m', str(timeout), '-A', UA, '-w', '\n%{http_code}', host + path],
                               capture_output=True, timeout=timeout + 5)
        except Exception:                                # noqa: BLE001
            continue
        body, _, code = r.stdout.rpartition(b'\n')
        if code.strip() == b'200' and len(body) > 200 and b'<html' not in body[:300].lower():
            return body.decode('utf-8', 'replace')
        time.sleep(0.3)
    return None


def fetch_day(d, db=None):
    """Download and store one trading day. Returns the number of values stored."""
    dm, dmy = d.strftime('%d%m%Y'), d.strftime('%d%m%y')
    vals = {}
    for path, parser, src in ((f'/content/indices/ind_close_all_{dm}.csv', parse_index_close, 'ind_close_all'),
                              (f'/content/nsccl/fao_participant_oi_{dm}.csv', parse_participant_oi, 'participant_oi'),
                              (f'/archives/equities/mkt/MA{dmy}.csv', parse_market_activity, 'market_activity')):
        text = _get(path)
        if text:
            for k, v in parser(text).items():
                vals[k] = (v, src)
    if not vals:
        return 0
    c = conn(db)
    now = datetime.now(IST).isoformat()
    with c:
        c.executemany('INSERT OR REPLACE INTO nse_daily VALUES (?,?,?,?,?)',
                      [(d.isoformat(), k, v, src, now) for k, (v, src) in vals.items()])
    return len(vals)


def run_nightly(now=None, days_back=5, db=None):
    """Fetch recent weekdays (today after 18:00 IST only) that have no VIX close stored yet."""
    now = now or datetime.now(IST)
    c = conn(db)
    have = {r[0] for r in c.execute("SELECT day FROM nse_daily WHERE key='vix_close'")}
    out = {}
    start = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
    d, seen = start, 0
    while seen < days_back:
        if d.weekday() < 5:
            seen += 1
            if d.isoformat() not in have:
                out[d.isoformat()] = fetch_day(d, db)
                time.sleep(0.3)
        d -= timedelta(days=1)
    return out


# ---------------------------------------------------------------- read (used by the forecast)
def stored_vix(before_day, db=None):
    """(value, day) of the latest stored VIX close strictly before `before_day`, or (None, None)."""
    try:
        c = sqlite3.connect(f'file:{db or DB_PATH}?mode=ro', uri=True, timeout=5)
        r = c.execute("SELECT value, day FROM nse_daily WHERE key='vix_close' AND day<? ORDER BY day DESC LIMIT 1",
                      (before_day.isoformat(),)).fetchone()
    except sqlite3.Error:
        return None, None
    if not r or r[0] is None:
        return None, None
    if (before_day - date.fromisoformat(r[1])).days > STORED_VIX_MAX_AGE_DAYS:
        return None, None
    return r[0], r[1]


def live_vix(now, path=None):
    """(value, source) from the market-context snapshot (broker LTP, refreshed every 60 s).

    A same-day value no older than LIVE_VIX_FRESH_SEC is 'india_vix_live'. Before the first poll of
    the day (09:15:04 is usually before it), the last value of the previous session, if it was
    taken after 15:00 within the last 4 days, is that session's close: 'india_vix_last'.
    """
    try:
        with open(path or MACRO_SNAPSHOT) as f:
            v = (json.load(f) or {}).get('india_vix') or {}
        val, ts = float(v['value']), datetime.fromisoformat(v['last_updated'])
    except Exception:                                    # noqa: BLE001
        return None, None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    age = (now - ts).total_seconds()
    if ts.date() == now.date() and -60 <= age <= LIVE_VIX_FRESH_SEC:
        return val, 'india_vix_live'
    if ts.date() < now.date() and (ts.hour, ts.minute) >= (15, 0) and (now.date() - ts.date()).days <= 4:
        return val, 'india_vix_last'
    return None, None


def vix_for_forecast(now, db=None, snapshot=None):
    """VIX_IV_SCALE x India VIX (percent) to stand in for a missing ATM IV, and where it came from."""
    if not VIX_FALLBACK:
        return None, None
    v, src = live_vix(now, snapshot)
    if v is not None and VIX_MIN <= v <= VIX_MAX:
        return round(v * VIX_IV_SCALE, 4), src
    v, day = stored_vix(now.date(), db)
    if v is not None and VIX_MIN <= v <= VIX_MAX:
        return round(v * VIX_IV_SCALE, 4), 'india_vix_close_' + day
    return None, None


def main(argv):
    if '--backfill' in argv:
        n = int(argv[argv.index('--backfill') + 1])
        print(json.dumps(run_nightly(days_back=n)))
    else:
        print(json.dumps(run_nightly()))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
