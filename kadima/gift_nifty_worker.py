"""GIFT Nifty market-context worker (trader request 2026-10-25). Display only.

The broker contract master has no NSE IX instruments, so the price comes from
NSE's public market-status JSON (the "giftnifty" block on nseindia.com): near-month
GIFT Nifty future, last price, day change and NSE's own timestamp.

Stand-alone process (systemd kadima-gift-nifty), stdlib only, imports nothing from
the engine, so a failure here can never touch trading. It writes
~/kadima-py/state/gift_nifty.json atomically; control_api adds it to the
/macro_snapshot response and the dashboard shows it beside crude oil.

Politeness: one cookie-priming visit to the NSE home page, re-used for 15 minutes;
one JSON call per minute during GIFT hours (Mon 06:25 to Sat 02:50 IST), every 30
minutes otherwise; 5-minute back-off after 3 failures in a row. A failure keeps the
last good price (with its NSE time) and records the error; nothing is ever invented.
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import time
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
OUT = os.path.expanduser('~/kadima-py/state/gift_nifty.json')
URL = 'https://www.nseindia.com/api/marketStatus'
HOME = 'https://www.nseindia.com'
FAST, SLOW, BACKOFF, PRIME_TTL = 60, 30 * 60, 5 * 60, 15 * 60
HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,'
              'image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-language': 'en-US,en;q=0.9,en-IN;q=0.8',
    'accept-encoding': 'gzip, deflate',
    'cache-control': 'max-age=0',
    'sec-ch-ua': '"Chromium";v="129", "Not=A?Brand";v="8"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
    'user-agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'),
}


def gift_hours(now):
    """GIFT Nifty trades ~06:30-02:45 IST, Monday morning to Saturday early morning."""
    wd, hm = now.weekday(), (now.hour, now.minute)
    if hm >= (6, 25):
        return wd <= 4
    if hm <= (2, 50):
        return 1 <= wd <= 5
    return False


def _body(resp):
    raw = resp.read()
    enc = resp.headers.get('Content-Encoding', '')
    if 'gzip' in enc:
        raw = gzip.decompress(raw)
    elif 'deflate' in enc:
        raw = zlib.decompress(raw)
    return raw.decode('utf-8', errors='replace')


def _num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float('inf') else None
    except (TypeError, ValueError):
        return None


def parse(data, fetched_at):
    """The giftnifty block -> our record, or None if it is missing or malformed."""
    g = data.get('giftnifty') if isinstance(data, dict) else None
    if not isinstance(g, dict) or _num(g.get('LASTPRICE')) is None:
        return None
    try:
        src = datetime.strptime(str(g.get('TIMESTMP')), '%d-%b-%Y %H:%M').replace(tzinfo=IST).isoformat()
    except ValueError:
        src = None
    return dict(symbol='GIFT NIFTY', underlying=g.get('SYMBOL'), instrument=g.get('INSTRUMENTTYPE'),
                expiry=g.get('EXPIRYDATE'), last_price=_num(g.get('LASTPRICE')),
                day_change=_num(g.get('DAYCHANGE')), pct_change=_num(g.get('PERCHANGE')),
                contracts=_num(g.get('CONTRACTSTRADED')), source_time=src, fetched_at=fetched_at,
                source='nseindia.com marketStatus')


class Worker:
    def __init__(self):
        self.opener, self.primed = None, 0.0
        self.state = dict(status='starting', gift=None, error=None, checked_at=None, display_only=True)
        try:
            with open(OUT) as f:
                self.state['gift'] = json.load(f).get('gift')   # keep last good price across restarts
        except (OSError, ValueError, AttributeError):
            pass

    def _prime(self):
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.opener.open(urllib.request.Request(HOME, headers=HEADERS), timeout=10).read()
        self.primed = time.monotonic()
        time.sleep(1.5)

    def fetch(self):
        if self.opener is None or time.monotonic() - self.primed > PRIME_TTL:
            self._prime()
        with self.opener.open(urllib.request.Request(URL, headers=HEADERS), timeout=10) as r:
            return json.loads(_body(r))

    def poll(self, now):
        stamp = now.isoformat()
        try:
            rec = parse(self.fetch(), stamp)
            if rec is None:
                raise ValueError('giftnifty block missing')
            self.state.update(status='ok', gift=rec, error=None, checked_at=stamp)
            ok = True
        except Exception as exc:  # noqa: BLE001 - keep last good price, record why
            self.opener = None
            self.state.update(status='fetch_failed', error=f'{type(exc).__name__}: {str(exc)[:160]}',
                              checked_at=stamp)
            ok = False
        self.state['gift_hours'] = gift_hours(now)
        self.write()
        return ok

    def write(self):
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        tmp = OUT + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.state, f)
        os.replace(tmp, OUT)


def main():
    w, fails = Worker(), 0
    while True:
        now = datetime.now(IST)
        fails = 0 if w.poll(now) else fails + 1
        wait = BACKOFF if fails >= 3 else FAST if gift_hours(now) else SLOW
        time.sleep(wait)


if __name__ == '__main__':
    main()
