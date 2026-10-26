#!/usr/bin/env python3
"""STOCK BLAST v1.2 installer (trader 2026-10-27 02:10 IST): loser filters. Requires v1.1.

Whole files (installed only if the live copy has the expected v1.1 md5):
  stock_blast.py              5-min stock move >= 0.2% in trade direction, no chase (option +10% in
                              3 min), main premium >= Rs 3, strike <= 0.5% ITM, 3 trades/stock/day
  move_capture_execution.py   applies the filters before any order; counts submitted trades
Backups *.bak-stock-blast-v12-20261027.  Usage: --root ~/kadima-py [--check | --rollback]
"""
import argparse, hashlib, os, shutil, sys

SUFFIX = '.bak-stock-blast-v12-20261027'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHOLE = {
    'stock_blast.py': '809c306480bbc95943d84ccd216c94ef',
    'move_capture_execution.py': 'a946bf1eb861558f9f9f0b041e31a1d2',
}


def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    if a.rollback:
        for name in WHOLE:
            p = os.path.join(k, name)
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    new = {}
    for name, expected in WHOLE.items():
        live = os.path.join(k, name)
        got = md5(live)
        if got != expected:
            sys.exit(f'{name}: live md5 {got} != expected {expected}; nothing changed')
        src = open(os.path.join(HERE, 'kadima', name)).read()
        compile(src, live, 'exec')
        new[name] = src
    if a.check:
        print('check ok: 2 whole files match expected md5, all compile'); return
    for name in WHOLE:
        p = os.path.join(k, name)
        shutil.copy2(p, p + SUFFIX)
    for name, src in new.items():
        open(os.path.join(k, name), 'w').write(src); print('written', name)


if __name__ == '__main__':
    main()
