#!/usr/bin/env python3
"""FOCUS LIST v1 installer (trader 2026-10-27 08:36). See kadima/focus_list.py.
Whole files, installed only if each live copy has the expected md5. Writes today's focus file.
Backups *.bak-focus-20261027. Usage: --root ~/kadima-py --data /var/lib/kadima [--check | --rollback]
"""
import argparse, hashlib, json, os, shutil, sys
SUFFIX = '.bak-focus-20261027'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHOLE = {'move_capture.py': '05ad2ba2ab36bd9013e9a136e3bdd072',
         'move_capture_execution.py': '82700fb5d803c2d6269968385fe1b0ad',
         'stock_blast.py': '9d8038578337e5935bd7fc8922f532a2',
         'stock_universe_scanner.py': 'dc535bf446c3de449d294d9279919845'}
NEW = ['focus_list.py']
FOCUS = dict(day='2026-10-27', trigger_score=60, max_trades=5,
             symbols=['LT', 'MARUTI', 'BAJAJ-AUTO', 'TATASTEEL', 'SBILIFE', 'CIPLA', 'INDUSTOWER',
                      'MAZDOCK', 'MFSL', 'PGEL', 'RADICO', 'SAIL', 'WAAREEENER'])


def md5(p):
    return hashlib.md5(open(p, 'rb').read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True); ap.add_argument('--data', default='/var/lib/kadima')
    ap.add_argument('--check', action='store_true'); ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    if a.rollback:
        for n in WHOLE:
            p = os.path.join(k, n)
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        json.dump(dict(day='', symbols=[]), open(os.path.join(a.data, 'focus_symbols.json'), 'w'))
        print('focus list emptied'); return
    new = {}
    for n, exp in WHOLE.items():
        got = md5(os.path.join(k, n))
        if got != exp:
            sys.exit(f'{n}: live md5 {got} != expected {exp}; nothing changed')
    for n in list(WHOLE) + NEW:
        src = open(os.path.join(HERE, 'kadima', n)).read(); compile(src, n, 'exec'); new[n] = src
    if a.check:
        print(f'check ok: {len(WHOLE)} whole files match, all compile'); return
    for n in WHOLE:
        shutil.copy2(os.path.join(k, n), os.path.join(k, n) + SUFFIX)
    for n, src in new.items():
        open(os.path.join(k, n), 'w').write(src); print('written', n)
    json.dump(FOCUS, open(os.path.join(a.data, 'focus_symbols.json'), 'w'), indent=1)
    print('focus file written:', len(FOCUS['symbols']), 'symbols')


if __name__ == '__main__':
    main()
