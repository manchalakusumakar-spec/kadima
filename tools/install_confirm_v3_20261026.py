#!/usr/bin/env python3
"""CONFIRM v3 (trader confirmed 2026-10-26 evening).

1. Gamma CONFIRMED: strike concentration HHI >= 0.09 is required again, ON TOP of the v2 rule
   (score >= threshold, index direction agrees, 09:45-10:10). Two places, so a missing or
   unreadable config file cannot silently drop the check:
     - kadima/gamma_confirm.py DEFAULTS min_hhi 0.0 -> 0.09
     - DATA_DIR/gamma_confirm.json min_hhi -> 0.09, only if that file exists (other keys kept)
2. Scalp v2 CONFIRMED: kadima/scalp_v2.py CONFIRM_SCORE (80, 90) -> (70, 90).
   Window 09:45-10:30 unchanged.

Exits, sizing, loss controls, Sniper, Best Entry, no-cap: unchanged.
Anchor-based: nothing is written unless every check passes; an item already at v3 is skipped.
Backups *.bak-confirmv3-20261026.
Usage: --root ~/kadima-py --data /var/lib/kadima [--check | --rollback]
"""
import argparse, json, os, shutil, sys

SUFFIX = '.bak-confirmv3-20261026'
MIN_HHI = 0.09

GAMMA_OLD = "DEFAULTS = {'mode': 'live', 'min_hhi': 0.0, 'require_direction': True,   # CONFIRM v2 2026-10-25\n"
GAMMA_NEW = "DEFAULTS = {'mode': 'live', 'min_hhi': 0.09, 'require_direction': True,   # CONFIRM v3 2026-10-26\n"
SCALP_OLD = "CONFIRM_SCORE = (80.0, 90.0)   # CONFIRM v2 2026-10-25: 80-90 (70-80 was weaker)\n"
SCALP_NEW = "CONFIRM_SCORE = (70.0, 90.0)   # CONFIRM v3 2026-10-26: back to 70-90 (trader)\n"


def plan_text(path, old, new):
    """New text for path, or None when it is already at v3. Exits on any other state."""
    src = open(path).read()
    if src.count(new) == 1 and src.count(old) == 0:
        return None
    if src.count(old) != 1:
        sys.exit(f'{os.path.basename(path)}: v2 anchor found {src.count(old)} times; nothing changed')
    out = src.replace(old, new)
    compile(out, path, 'exec')
    return out


def plan_json(path):
    """New JSON text for the gamma config, or None when absent or already at v3."""
    if not os.path.exists(path):
        return None
    try:
        raw = json.load(open(path))
    except ValueError:
        sys.exit(f'{path}: not valid JSON; nothing changed')
    if not isinstance(raw, dict):
        sys.exit(f'{path}: not a JSON object; nothing changed')
    if raw.get('min_hhi') == MIN_HHI:
        return None
    raw['min_hhi'] = MIN_HHI
    return json.dumps(raw, indent=2) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--data', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    gc, sv = os.path.join(k, 'gamma_confirm.py'), os.path.join(k, 'scalp_v2.py')
    gj = os.path.join(a.data, 'gamma_confirm.json')
    if a.rollback:
        for p in (gc, sv, gj):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    for p in (gc, sv):
        if not os.path.exists(p):
            sys.exit(f'{p} missing; nothing changed')
    writes = [(p, t) for p, t in ((gc, plan_text(gc, GAMMA_OLD, GAMMA_NEW)),
                                  (sv, plan_text(sv, SCALP_OLD, SCALP_NEW)),
                                  (gj, plan_json(gj))) if t is not None]
    if a.check:
        print('check ok; to write:', [os.path.basename(p) for p, _ in writes] or 'nothing (already v3)')
        return
    for p, text in writes:
        shutil.copy2(p, p + SUFFIX)
        with open(p, 'w') as f:
            f.write(text)
        print('written', p)
    if not writes:
        print('already v3; nothing written')


if __name__ == '__main__':
    main()
