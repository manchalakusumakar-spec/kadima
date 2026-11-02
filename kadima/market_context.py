"""Light market-context service (trader 2026-10-25): crude oil, India VIX, FII/DII, news.

Replaces kadima-macro-watch, which was disabled on 2026-10-20 during an out-of-memory
event: it parsed the whole broker contract master (159k rows, ~220 MB peak) just to find
a handful of tokens. This service scans the cached master file in 1 MB pieces and keeps
only the CRUDEOIL MCX rows and the India VIX row, so it stays small.

Same output as before: ~/kadima-py/state/macro_snapshot.json, the same keys
(india_vix, fii_dii, news, economic_calendar, commodities), so control_api and the
dashboard work unchanged. Display only: nothing here places orders or feeds a score.

Sources and cadences (unchanged from macro_watch):
  crude futures  broker MCX quotes, every 30 s, 09:00-23:30 IST weekdays (commodity_watch)
  India VIX      broker LTP, every 60 s in market hours
  FII/DII        nseindia.com, every 30 min        news  Marketaux, every 30 min
Every value keeps its real source time; a failed fetch keeps the last good value.

  python -m kadima.market_context              run forever (systemd kadima-market-context)
  python -m kadima.market_context --selftest   scan the master, print counts and memory, exit
"""
from __future__ import annotations

import json
import os
import re
import resource
import sys
import time

from . import audit
from . import macro_watch as mw            # fetch helpers only; MacroWatcher is never built
from .commodity_watch import CommodityWatch, monitoring_window
from .rate_limiter import QUOTE
from .scrip_master import CACHE
from .session import SESSION
from .utils import in_market_hours, now_ist

KEEP_ROOTS = (b'"CRUDEOIL"',)
OBJ = re.compile(rb'\{[^{}]*\}')
CHUNK = 1 << 20


def light_rows(path=CACHE):
    """CRUDEOIL MCX rows and the India VIX row from the cached master, without parsing
    the whole file. Returns (crude_rows, vix_ref or None)."""
    crude, vix, carry = [], None, b''
    with open(path, 'rb') as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            data = carry + block
            last = 0
            for m in OBJ.finditer(data):
                last = m.end()
                text = m.group(0)
                if any(k in text for k in KEEP_ROOTS) and b'"MCX"' in text:
                    try:
                        row = json.loads(text)
                    except ValueError:
                        continue
                    if row.get('exch_seg') == 'MCX' and row.get('name') == 'CRUDEOIL':
                        crude.append(row)
                elif vix is None and b'VIX' in text.upper():
                    try:
                        row = json.loads(text)
                    except ValueError:
                        continue
                    name = (row.get('name') or '').upper().replace(' ', '')
                    symbol = (row.get('symbol') or '').upper().replace(' ', '')
                    if (name == 'INDIAVIX' or symbol == 'INDIAVIX') and row.get('exch_seg') and row.get('token'):
                        vix = (str(row['exch_seg']), str(row['token']))
            carry = data[last:]
            if len(carry) > CHUNK:            # no object boundary for 1 MB: not this format
                raise ValueError('scrip_master_format')
    return crude, vix


class MarketContext:
    def __init__(self):
        self.crude, self.vix_ref, self.master_mtime = [], None, None
        self.commodities = CommodityWatch()
        self.vix = self.fiidii = None
        self.news = []
        try:                                   # keep last good values across restarts
            with open(mw.SNAPSHOT_PATH) as f:
                old = json.load(f)
            self.vix, self.fiidii, self.news = old.get('india_vix'), old.get('fii_dii'), old.get('news') or []
        except (OSError, ValueError, AttributeError):
            pass

    def refresh_master(self):
        try:
            mtime = os.path.getmtime(CACHE)
        except OSError:
            return
        if mtime == self.master_mtime:
            return
        try:
            self.crude, vix = light_rows()
            self.vix_ref = vix or self.vix_ref
            self.master_mtime = mtime
            audit.log('market_context_master', crude_rows=len(self.crude), vix=bool(self.vix_ref))
        except Exception as exc:  # noqa: BLE001
            audit.log('market_context_master_failed', error=f'{type(exc).__name__}: {str(exc)[:120]}')

    def poll_crude(self, stamp):
        def fetch(tokens):
            if not QUOTE.wait_and_allow(timeout_sec=3.0):
                raise RuntimeError('quote_rate_limited')
            SESSION.ensure()
            return SESSION.api.getMarketData('FULL', {'MCX': tokens})
        self.commodities.poll(self.crude, fetch, stamp)

    def poll_vix(self):
        if not self.vix_ref or not QUOTE.wait_and_allow(timeout_sec=3.0):
            return
        exch, token = self.vix_ref
        try:
            SESSION.ensure()
            resp = SESSION.api.getMarketData(mode='LTP', exchangeTokens={exch: [token]})
            fetched = (resp or {}).get('data', {}).get('fetched', [])
            if fetched and fetched[0].get('ltp') is not None:
                self.vix = {'value': float(fetched[0]['ltp']), 'last_updated': now_ist().isoformat()}
        except Exception as exc:  # noqa: BLE001
            audit.log('market_context_vix_failed', error=str(exc)[:200])

    def save(self):
        payload = {'ts': now_ist().isoformat(), 'india_vix': self.vix, 'fii_dii': self.fiidii,
                   'news': self.news, 'economic_calendar': mw._upcoming_events(now_ist().date()),
                   'commodities': self.commodities.snapshot(), 'source': 'market_context'}
        try:
            os.makedirs(os.path.dirname(mw.SNAPSHOT_PATH), exist_ok=True)
            tmp = mw.SNAPSHOT_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(payload, f)
            os.replace(tmp, mw.SNAPSHOT_PATH)
        except Exception as exc:  # noqa: BLE001
            audit.log('market_context_write_failed', error=str(exc)[:200])

    def run(self):
        last = dict(master=0.0, crude=0.0, vix=0.0, fiidii=0.0, news=0.0)
        self.save()
        while True:
            now, stamp = time.monotonic(), now_ist()
            changed = False
            if now - last['master'] >= 600:
                self.refresh_master(); last['master'] = now
            if monitoring_window(stamp) and now - last['crude'] >= mw.COMMODITY_POLL_SEC:
                self.poll_crude(stamp); last['crude'] = now; changed = True
            if in_market_hours() and now - last['vix'] >= mw.VIX_POLL_SEC:
                self.poll_vix(); last['vix'] = now; changed = True
            if now - last['fiidii'] >= mw.FIIDII_POLL_SEC:
                rows = mw._fetch_fiidii()
                if rows:
                    self.fiidii = {'rows': rows, 'fetched_at': now_ist().isoformat()}
                last['fiidii'] = now; changed = True
            if now - last['news'] >= mw.NEWS_POLL_SEC:
                articles = mw._fetch_news()
                if articles:
                    self.news = articles
                last['news'] = now; changed = True
            if changed or int(now) % 60 < mw.LOOP_TICK_SEC:
                self.save()
            time.sleep(mw.LOOP_TICK_SEC)


def main(argv):
    if '--selftest' in argv:
        t = time.time()
        crude, vix = light_rows()
        fut = [r for r in crude if r.get('instrumenttype') == 'FUTCOM']
        print(json.dumps(dict(crude_rows=len(crude), crude_futures=sorted(r['symbol'] for r in fut)[:4],
                              vix=vix, seconds=round(time.time() - t, 1),
                              max_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1))))
        return 0
    audit.log('market_context_starting')
    MarketContext().run()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
