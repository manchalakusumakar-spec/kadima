#!/usr/bin/env python3
"""CONFIRM v2 (trader confirmed 2026-10-25 21:10 IST): tighter CONFIRMED rules.

1. kadima/gamma_confirm.py replaced by the v2 module: Gamma CONFIRMED = score >= threshold,
   index direction (Bull Shield) agrees with the option side, 09:45-10:10; HHI no longer
   required (min_hhi 0). Only replaced if the live file is exactly v1 (hash below).
2. kadima/scalp_v2.py: CONFIRM_SCORE (70, 90) -> (80, 90). Window 09:45-10:30 unchanged.
3. dashboard.html rules strip: "HHI >= x" text -> "index direction agrees" (HHI only if set).

Exits, sizing, loss controls, Sniper: unchanged.
Nothing is written unless every check passes. Backups *.bak-confirmv2-20261025.
Usage: --root ~/kadima-py --html /var/www/html/dashboard.html [--check | --rollback]
"""
import argparse, hashlib, os, shutil, sys

SUFFIX = '.bak-confirmv2-20261025'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V1_SHA = '35693cb81f70'
SCALP = [("CONFIRM_SCORE = (70.0, 90.0)\n",
          "CONFIRM_SCORE = (80.0, 90.0)   # CONFIRM v2 2026-10-25: 80-90 (70-80 was weaker)\n")]
HTML = [("""+', HHI ≥ '+esc(g.min_hhi??'—')""",
         """+(g.require_direction?', index direction agrees':'')+(g.min_hhi>0?', HHI ≥ '+esc(g.min_hhi):'')""")]


def sha(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()[:12]


def patch(path, items):
    src = open(path).read()
    for old, new in items:
        if src.count(old) != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {src.count(old)} times: {old.strip()[:60]!r}; nothing changed')
        src = src.replace(old, new)
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--html', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    gc, sv = os.path.join(k, 'gamma_confirm.py'), os.path.join(k, 'scalp_v2.py')
    if a.rollback:
        for p in (gc, sv, a.html):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    if sha(gc) != V1_SHA:
        sys.exit(f'gamma_confirm.py is not v1 (sha {sha(gc)}); nothing changed')
    new_gc = open(os.path.join(HERE, 'kadima', 'gamma_confirm.py')).read()
    assert 'CONFIRM v2' in new_gc
    compile(new_gc, gc, 'exec')
    new_sv = patch(sv, SCALP); compile(new_sv, sv, 'exec')
    new_html = patch(a.html, HTML)
    if a.check:
        print('check ok: gamma_confirm.py, scalp_v2.py, dashboard.html'); return
    for p, text in ((gc, new_gc), (sv, new_sv), (a.html, new_html)):
        shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', p)


if __name__ == '__main__':
    main()
