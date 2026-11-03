#!/usr/bin/env python3
"""TRADE PROGRESS v1.3 installer: live 'paper' progress for today's CONFIRMED suggestions that were not
traded (trader 2026-11-03 10:28). Display only.
control_api.py   /api/kadima/top20 response gains paper=[...] (trade_progress.paper()).
trade_progress.py replaced with the v1.3 module (adds paper(); v1 functions unchanged).
dashboard.html   snippet block replaced (paper cards under SUGGESTED rows in Trade Audit).
Backups *.bak-trade-progress-v13. Usage: --root ~/kadima-py --web /var/www/html [--check | --rollback]
"""
import argparse, os, shutil, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK, SUFFIX = 'TRADE PROGRESS v1.3', '.bak-trade-progress-v13'
API_OLD = "        readiness_note='Candidate prechecks only; execution rechecks alignment, structure, sizing, slots and broker state.',"
API_NEW = "        paper=_tp_paper(),   # TRADE PROGRESS v1.3\n" + API_OLD
API_TAIL = '''

def _tp_paper():  # TRADE PROGRESS v1.3 (display only)
    try:
        from . import trade_progress
        return trade_progress.paper()
    except Exception:  # noqa: BLE001
        return []
'''
START = '<!-- TRADE PROGRESS v1 '


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--root', required=True); ap.add_argument('--web', default='/var/www/html')
    ap.add_argument('--check', action='store_true'); ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args(); k = os.path.join(os.path.expanduser(a.root), 'kadima')
    api, mod, dash = os.path.join(k, 'control_api.py'), os.path.join(k, 'trade_progress.py'), os.path.join(a.web, 'dashboard.html')
    if a.rollback:
        for p in (api, mod, dash):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    src = open(api).read()
    if MARK in src:
        sys.exit('control_api.py already contains ' + MARK + '; nothing changed')
    if src.count(API_OLD) != 1:
        sys.exit('control_api.py anchor not found once; nothing changed')
    t_api = src.replace(API_OLD, API_NEW) + API_TAIL
    compile(t_api, api, 'exec')
    new_mod = open(os.path.join(HERE, 'kadima', 'trade_progress.py')).read(); compile(new_mod, mod, 'exec')
    d = open(dash).read()
    if d.count(START) != 1:
        sys.exit('dashboard.html: installed progress snippet not found once; nothing changed')
    i = d.index(START); j = d.index('</script>', d.index('<script id="tp-script">', i)) + len('</script>')
    t_dash = d[:i] + open(os.path.join(HERE, 'web', 'trade_progress_snippet.html')).read().rstrip('\n') + d[j:]
    if 'data.paper' not in t_dash and 'last.paper' not in t_dash:
        sys.exit('snippet lacks paper support; nothing changed')
    if a.check:
        print('check ok: control_api.py 1/1, trade_progress.py compiles, dashboard snippet replaced'); return
    for p in (api, mod, dash):
        shutil.copy2(p, p + SUFFIX)
    open(mod, 'w').write(new_mod); open(api, 'w').write(t_api); open(dash, 'w').write(t_dash)
    print('written', mod, api, dash)


if __name__ == '__main__':
    main()
