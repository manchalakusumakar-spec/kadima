#!/usr/bin/env python3
"""LOSS CONTROLS v1 (trader 2026-10-25, after Sniper + GAMMA CONFIRM v1).

1. kadima/daily_limit.py (new): daily loss limit = min(10,000, 30% of the morning's
   broker cash), floor 2,000, fixed 10,000 fallback on any unreadable cash.
2. auto_executor.py: the five uses of DAILY_LOSS_LIMIT_INR (entry gates, force-flatten
   check, portfolio sizing) read daily_limit.limit() instead. CAS / MCX modules keep
   the fixed 10,000 (the new limit is never higher than that).
3. sniper.py: rule 6b - after any losing Sniper trade today, no more Sniper (auto gamma)
   entries that day. Switch: sniper.json "stop_after_loss" (default true).

Anchor-based; nothing is written unless every anchor matches. Backups *.bak-losscontrols-20261025.
Usage: --root ~/kadima-py [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'LOSS CONTROLS v1'
SUFFIX = '.bak-losscontrols-20261025'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AE_COUNTS = [  # (old, new, expected count)
    ("realized_pnl_today() <= -DAILY_LOSS_LIMIT_INR",
     "realized_pnl_today() <= -_daily_loss_limit()", 3),
    ("daily_limit=DAILY_LOSS_LIMIT_INR,", "daily_limit=_daily_loss_limit(),", 2),
]
AE_TAIL = '''

def _daily_loss_limit():  # LOSS CONTROLS v1 (2026-10-25)
    from . import daily_limit
    return daily_limit.limit()
'''
SN = [
    ("""    'require_second_signal': True,
""",
     """    'require_second_signal': True,
    'stop_after_loss': True,   # LOSS CONTROLS v1 (2026-10-25): no Sniper entry after a Sniper loss today
"""),
    ("""    taken = _entries(day, entry_mode)
""",
     """    taken = _entries(day, entry_mode)
    if cfg['stop_after_loss'] and any(e['lost'] for e in taken):   # LOSS CONTROLS v1
        return 'sniper_stopped_after_loss'
"""),
]


def plan(root):
    out = {}
    ae = os.path.join(root, 'kadima', 'auto_executor.py')
    src = open(ae).read()
    if MARK in src:
        sys.exit('auto_executor.py already has ' + MARK)
    for old, new, n in AE_COUNTS:
        if src.count(old) != n:
            sys.exit(f'auto_executor.py: {old!r} found {src.count(old)} times (need {n}); nothing changed')
        src = src.replace(old, new)
    src += AE_TAIL
    compile(src, ae, 'exec'); out[ae] = src
    sn = os.path.join(root, 'kadima', 'sniper.py')
    s = open(sn).read()
    for old, new in SN:
        if s.count(old) != 1:
            sys.exit(f'sniper.py: anchor found {s.count(old)} times: {old.strip()[:60]!r}; nothing changed')
        s = s.replace(old, new)
    compile(s, sn, 'exec'); out[sn] = s
    dl = os.path.join(root, 'kadima', 'daily_limit.py')
    out[dl] = open(os.path.join(HERE, 'kadima', 'daily_limit.py')).read()
    compile(out[dl], dl, 'exec')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    root = os.path.expanduser(a.root)
    if a.rollback:
        for name in ('auto_executor.py', 'sniper.py'):
            p = os.path.join(root, 'kadima', name)
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', name)
        dl = os.path.join(root, 'kadima', 'daily_limit.py')
        if os.path.exists(dl):
            os.remove(dl); print('removed daily_limit.py')
        return
    out = plan(root)
    if a.check:
        print('check ok:', ', '.join(os.path.basename(p) for p in out)); return
    for p, text in out.items():
        if os.path.exists(p):
            shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', os.path.basename(p))


if __name__ == '__main__':
    main()
