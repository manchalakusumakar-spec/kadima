#!/usr/bin/env python3
"""STOCK BLAST v1.1 installer (trader 2026-10-27 01:30 IST). Requires v1 installed.

Whole files (installed only if the live copy has the expected pre-change md5):
  move_capture.py            hot list 10 -> 20
  move_capture_execution.py  score >= 80 served first; lottery-grade ladder (10 strikes each
                             side); second small far-strike order on triggers >= 88
  stock_blast.py             LOTTERY tier (score >= 88, <= 15% out, Rs 0.50 floor, 20% size)
  stock_fast.py, stock_execution.py, gap_live.py
                             historical candles through one shared pacer (candle_pacer.py)
  preparation_diagnostics.py real error class and a cleaned message in rejection logs
  candle_pacer.py            new
Anchor patches:
  auto_executor.py           valid strikes follow the chain's own ladder width (index chains
                             unchanged); lottery orders sized at 20%.
  ~/kadima-audit-push.sh     also commits missed_blasts/*.json
Nothing is written unless every check passes and everything compiles.
Backups *.bak-stock-blast-v11-20261027.
Usage: --root ~/kadima-py --data /var/lib/kadima [--push-script FILE] [--check | --rollback]
"""
import argparse, hashlib, os, shutil, sys

MARK = 'STOCK BLAST v1.1'
SUFFIX = '.bak-stock-blast-v11-20261027'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WHOLE = {
    'move_capture.py': '9e5830937554192930cd5862b2901ba5',
    'move_capture_execution.py': 'f7d423fef4f123648c91f4f32c756790',
    'stock_blast.py': '974a18648dfa35f8ab58dc5f5818a94e',
    'stock_fast.py': '373f259f7fe81a6c70b35a81dd0a38b8',
    'stock_execution.py': '97e14245536a56f227fb68bf40ef0f0e',
    'gap_live.py': 'f9d604ed48968d597b4b033282e95430',
    'preparation_diagnostics.py': 'a0d25f6ba6684c3259e1bef2cdcfd01f',
}
NEW = ['candle_pacer.py']

AE = [(
"""            current_contracts = chain.scrips.strikes_around(
                chain.index, chain.atm, config.STRIKES_EACH_SIDE)
""",
"""            current_contracts = chain.scrips.strikes_around(   # STOCK BLAST v1.1: chain's own width
                chain.index, chain.atm, max(config.STRIKES_EACH_SIDE,
                                            getattr(chain, 'strikes_each_side', 0) or 0))
"""), (
"""            allocation = stock_blast.allocation(allocation, fast.get('score', 0))
""",
"""            allocation = stock_blast.allocation(allocation, fast.get('score', 0), fast)   # STOCK BLAST v1.1
""")]

PUSH = [(
"""git add audit/*.csv
""",
"""git add audit/*.csv
git add missed_blasts/*.json 2>/dev/null || true   # STOCK BLAST v1.1: daily missed-blasts report
""")]


def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


def patch(path, items, mark=MARK, compile_py=True):
    src = open(path).read()
    if mark in src:
        sys.exit(f'{os.path.basename(path)}: already contains {mark}; nothing changed')
    for old, new in items:
        n = src.count(old)
        if n != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {n} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    if compile_py:
        compile(src, path, 'exec')
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--data', default='/var/lib/kadima')
    ap.add_argument('--push-script')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    ae = os.path.join(k, 'auto_executor.py')
    targets = list(WHOLE) + ['auto_executor.py']
    if a.rollback:
        for name in targets:
            p = os.path.join(k, name)
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        for name in NEW:
            p = os.path.join(k, name)
            if os.path.exists(p):
                os.remove(p); print('removed', p)
        if a.push_script and os.path.exists(a.push_script + SUFFIX):
            shutil.copy2(a.push_script + SUFFIX, a.push_script); print('restored', a.push_script)
        return
    if MARK not in open(os.path.join(k, 'stock_blast.py')).read() and \
            'STOCK BLAST v1' not in open(ae).read():
        sys.exit('STOCK BLAST v1 is not installed; nothing changed')
    new = {}
    for name, expected in WHOLE.items():
        live = os.path.join(k, name)
        got = md5(live)
        if got != expected:
            sys.exit(f'{name}: live md5 {got} != expected {expected}; nothing changed')
        src = open(os.path.join(HERE, 'kadima', name)).read()
        compile(src, live, 'exec')
        new[name] = src
    for name in NEW:
        src = open(os.path.join(HERE, 'kadima', name)).read()
        compile(src, name, 'exec')
        new[name] = src
    t_ae = patch(ae, AE)
    t_push = patch(a.push_script, PUSH, compile_py=False) if a.push_script else None
    if a.check:
        print(f'check ok: {len(WHOLE)} whole files match expected md5, auto_executor.py '
              f'{len(AE)}/{len(AE)} anchors' + (', push script 1/1' if t_push else '') +
              ', all compile')
        return
    for name in targets:
        p = os.path.join(k, name)
        shutil.copy2(p, p + SUFFIX)
    if t_push:
        shutil.copy2(a.push_script, a.push_script + SUFFIX)
    for name, src in new.items():
        open(os.path.join(k, name), 'w').write(src); print('written', name)
    open(ae, 'w').write(t_ae); print('written auto_executor.py')
    if t_push:
        open(a.push_script, 'w').write(t_push); print('written', a.push_script)


if __name__ == '__main__':
    main()
