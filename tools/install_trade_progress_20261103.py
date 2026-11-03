#!/usr/bin/env python3
"""TRADE PROGRESS v1 installer (trader 2026-11-03 09:16 IST). Display only. See kadima/trade_progress.py.

control_api.py   /api/kadima/top20 positions gain peak_price + progress (target, achieved %, remaining %,
                 status) computed every refresh.
dashboard.html   animated progress card under the live trade's Top 20 row and its Open Positions row.
Anchor-based; nothing written unless every anchor matches once. Backups *.bak-trade-progress-20261103.
Usage: --root ~/kadima-py --web /var/www/html [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'TRADE PROGRESS v1'
SUFFIX = '.bak-trade-progress-20261103'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API = [(
"""                    protection='engine_managed_not_broker_hosted'))
""",
"""                    protection='engine_managed_not_broker_hosted'))
                try:   # TRADE PROGRESS v1 (display only)
                    from . import trade_progress
                    positions[-1].update(peak_price=p.peak_price, progress=trade_progress.for_position(p, ltp))
                except Exception:  # noqa: BLE001
                    pass
""")]
WEB = [("legacy.renderGamma();syncControls()}",
        "(window.renderTradeProgress||(()=>{}))(data);legacy.renderGamma();syncControls()}")]


def patch(path, items, tail_before=None, tail=None):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in items:
        n = src.count(old)
        if n != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {n} times: {old.strip()[:60]!r}; nothing changed')
        src = src.replace(old, new)
    if tail_before:
        if src.count(tail_before) != 1:
            sys.exit(f'{os.path.basename(path)}: {tail_before} not found once; nothing changed')
        src = src.replace(tail_before, tail + tail_before)
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--web', default='/var/www/html')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    api, mod, dash = os.path.join(k, 'control_api.py'), os.path.join(k, 'trade_progress.py'), os.path.join(a.web, 'dashboard.html')
    if a.rollback:
        for p in (api, dash):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        if os.path.exists(mod):
            os.remove(mod); print('removed', mod)
        return
    new_mod = open(os.path.join(HERE, 'kadima', 'trade_progress.py')).read()
    compile(new_mod, mod, 'exec')
    t_api = patch(api, API)
    compile(t_api, api, 'exec')
    snippet = open(os.path.join(HERE, 'web', 'trade_progress_snippet.html')).read()
    t_dash = patch(dash, WEB, '</body>', snippet)
    if a.check:
        print('check ok: control_api.py 1/1, dashboard.html 1/1 + snippet, trade_progress.py compiles'); return
    for p in (api, dash):
        shutil.copy2(p, p + SUFFIX)
    open(mod, 'w').write(new_mod); print('written', mod)
    open(api, 'w').write(t_api); print('written', api)
    open(dash, 'w').write(t_dash); print('written', dash)


if __name__ == '__main__':
    main()
