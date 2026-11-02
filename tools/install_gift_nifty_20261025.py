#!/usr/bin/env python3
"""GIFT NIFTY v1 (trader 2026-10-25): GIFT Nifty beside crude oil on the dashboard. Display only.

1. kadima/gift_nifty_worker.py (new): stand-alone NSE reader -> state/gift_nifty.json
   (run by the new systemd unit kadima-gift-nifty, installed by the deploy script).
2. control_api.py: /macro_snapshot response gains a `gift_nifty` block read from that file
   (any read error gives None; the macro data itself is untouched).
3. dashboard.html: panel renamed "Crude Oil & GIFT Nifty · Market Context"; a GIFT row
   (price, change, GIFT minus NIFTY spot, NSE time, freshness) above the crude table.

Anchor-based, nothing written unless every anchor matches once. Backups *.bak-gift-20261025.
Usage: --root ~/kadima-py --html /var/www/html/dashboard.html [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'GIFT NIFTY v1'
SUFFIX = '.bak-gift-20261025'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API = [
    ('''        with open(MACRO_SNAPSHOT_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:''',
     '''        with open(MACRO_SNAPSHOT_PATH, "r") as f:
            return _with_gift(json.load(f))  # GIFT NIFTY v1
    except FileNotFoundError:'''),
    ('''        return {"ts": None, "india_vix": None, "fii_dii": None, "news": [],
                "economic_calendar": []}''',
     '''        return _with_gift({"ts": None, "india_vix": None, "fii_dii": None, "news": [],
                "economic_calendar": []})  # GIFT NIFTY v1'''),
]
API_TAIL = '''

GIFT_NIFTY_PATH = os.path.expanduser("~/kadima-py/state/gift_nifty.json")


def _with_gift(data):  # GIFT NIFTY v1 (2026-10-25): display-only market context
    try:
        with open(GIFT_NIFTY_PATH, "r") as f:
            gift = json.load(f)
    except Exception:  # noqa: BLE001 - missing/garbled file never breaks the macro feed
        gift = None
    if isinstance(data, dict):
        data["gift_nifty"] = gift
    return data
'''

RENDER = r"""function renderGift(){/* GIFT NIFTY v1: display only */const box=$('gift');if(!box)return;const g=feeds.macro.data?.gift_nifty,r=g&&g.gift;if(!r){box.innerHTML=empty('GIFT Nifty price unavailable'+(g&&g.error?' · last check failed: '+g.error:'; waiting for the first NSE reading.'));return}
const spot=feeds.state.data?.indices?.NIFTY?.spot,prem=num(r.last_price)&&num(spot)?r.last_price-spot:null,src=Date.parse(r.source_time),ageMin=Number.isFinite(src)?(Date.now()-src)/60000:Infinity;
const status=!onlineLegacy('macro')?'Last known · disconnected':g.gift_hours?(ageMin<=5?'Live · context only':'Last known · '+(Number.isFinite(ageMin)?Math.round(ageMin)+' min old':'no NSE time')):'GIFT session closed · last price';
const sign=x=>(x>0?'+':'')+fmt(x),chg=num(r.day_change)?sign(r.day_change)+(num(r.pct_change)?' ('+sign(r.pct_change)+'%)':''):'—';
box.innerHTML=table(['GIFT Nifty future','Expiry','Price','Change','GIFT − NIFTY spot','NSE time','Status'],['<tr><td>GIFT NIFTY</td><td>'+esc(r.expiry)+'</td><td>'+fmt(r.last_price)+'</td><td class="'+(num(r.day_change)?(r.day_change>=0?'good':'bad'):'')+'">'+chg+'</td><td>'+(num(prem)?sign(prem):'—')+'</td><td>'+stamp(r.source_time)+'</td><td>'+esc(status)+'</td></tr>'])+'<p class="muted">Source: NSE website (nseindia.com) · checked '+stamp(g.checked_at)+(g.status!=='ok'?' · last check failed: '+esc(g.error||g.status):'')+' · Display only: not used by any score, gate or trade.</p>';}
function renderCommodities(){renderGift();const c="""

HTML = [
    ('<h2>Crude Oil · Market Context</h2>', '<h2>Crude Oil &amp; GIFT Nifty · Market Context</h2>'),
    ('<div id="legacy-commodities"></div>', '<div id="legacy-gift"></div><div id="legacy-commodities"></div><!-- GIFT NIFTY v1 -->'),
    ('function renderCommodities(){const c=', RENDER),
]


def patch(path, items, tail=''):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in items:
        if src.count(old) != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {src.count(old)} times: {old.strip()[:60]!r}; nothing changed')
        src = src.replace(old, new)
    return src + tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--html', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    api, worker = os.path.join(k, 'control_api.py'), os.path.join(k, 'gift_nifty_worker.py')
    if a.rollback:
        for p in (api, a.html):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    new_api = patch(api, API, API_TAIL); compile(new_api, api, 'exec')
    new_html = patch(a.html, HTML)
    new_worker = open(os.path.join(HERE, 'kadima', 'gift_nifty_worker.py')).read(); compile(new_worker, worker, 'exec')
    if a.check:
        print('check ok: control_api.py, dashboard.html, gift_nifty_worker.py'); return
    for p, text in ((api, new_api), (a.html, new_html)):
        shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', p)
    open(worker, 'w').write(new_worker); print('written', worker)


if __name__ == '__main__':
    main()
