#!/usr/bin/env python3
"""TREND + GRACE v1 (trader confirmed 2026-10-27 09:14 IST), layered on CONFIRM v3.

Replay 21/22/23/26 Oct on minute prices with live exits (v3 rules):
  gamma CONFIRMED  4 trades, 4 wins, +122%  ->  5 trades, 5 wins, +143% (adds 26-Oct +21%)
  scalp CONFIRMED  15 trades, 60% wins, +127%  ->  80% wins, +256% (26-Oct: -42% -> +101%)

1. gamma_confirm.py: the direction rule passes when the Bull Shield agrees (as now) OR the
   day trend agrees: index >= trend_pct (0.3%) from today's open AND on the same side of
   VWAP (CE: up and above, PE: down and below). HHI, score and window rules unchanged.
   Switch: gamma_confirm.json "trend_confirm": false; "trend_pct".
2. Scalp loss handling stays with BEST ENTRY v1 (live since 26-Oct night); its scalp grace
   goes 3 -> 5 minutes via best_entry.json "scalp_grace_min": 5 (deploy script, no code).
3. fast_momentum.py: the "no progress" exit for v2 scalps checks at 10 minutes instead of 5
   (with 5 it undoes the grace: replay with BEST ENTRY rules +134% / +144%, with 10 +249%).
   Constant NO_PROGRESS_V2_SEC (600); other fast_momentum positions keep 300.

Unchanged: sizing, daily loss limit, Sniper rules, gamma stops/trail, stock blast lanes.
Anchor-based; nothing written unless every anchor matches once. Backups *.bak-trendgrace-20261027.
Replay (BEST ENTRY loss rules, v3 scalp window, 21/22/23/26 Oct): grace 3 + no-progress 5
(now) +134%, 26-Oct -32%; grace 5 + no-progress 10: 80% wins, +249%, 26-Oct +101%.
Usage: --root ~/kadima-py [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'TREND GRACE v1'
SUFFIX = '.bak-trendgrace-20261027'

GC = [
("""            if isinstance(raw.get('require_direction'), bool):
                value['require_direction'] = raw['require_direction']""",
"""            if isinstance(raw.get('require_direction'), bool):
                value['require_direction'] = raw['require_direction']
            if isinstance(raw.get('trend_confirm'), bool):   # TREND GRACE v1
                value['trend_confirm'] = raw['trend_confirm']
            v = raw.get('trend_pct')
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 3:
                value['trend_pct'] = float(v)"""),
("""    if cfg['require_direction'] and not direction_agrees(row):""",
"""    if cfg['require_direction'] and not (direction_agrees(row) or (   # TREND GRACE v1
            cfg.get('trend_confirm', True) and trend_agrees(row, now, cfg.get('trend_pct', 0.3)))):"""),
]
GC_TAIL = '''

_trend_logged: dict = {}


def trend_agrees(row, now, pct=0.3):
    """TREND GRACE v1 (2026-10-27): the day trend agrees with the option side.
    CE: index >= pct% above today's open and above VWAP; PE: <= -pct% and below VWAP.
    Any missing or stale input -> False (the Shield rule alone then decides)."""
    try:
        side, name = row.get('direction'), row.get('underlying')
        if side not in ('CE', 'PE') or not name:
            return False
        from .config import INDICES
        from . import celestial, trend_rules
        if name not in INDICES:
            return False
        h = celestial.health('structure:' + name)
        if not h or not celestial.fresh(h.get('ts'), now) or not celestial.fresh(h.get('spot_at'), now):
            return False
        spot = float(h.get('spot'))
        vwap = trend_rules.vwap_for(name, now)
        day_open = trend_rules.inputs(INDICES[name].token, now).get('day_open')
        if not (math.isfinite(spot) and vwap and day_open):
            return False
        move = (spot / float(day_open) - 1) * 100
        ok = (move >= pct and spot > vwap) if side == 'CE' else (move <= -pct and spot < vwap)
        key = (name, side, now.strftime('%H:%M'))
        if ok and _trend_logged.get((name, side)) != key[2]:
            _trend_logged[(name, side)] = key[2]
            try:
                from . import audit
                audit.log('gamma_confirm_trend', index=name, direction=side, move_pct=round(move, 3),
                          spot=spot, vwap=round(float(vwap), 2), symbol=row.get('symbol'))
            except Exception:  # noqa: BLE001
                pass
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False
'''

FM_TAIL = '''

NO_PROGRESS_V2_SEC = 600   # TREND GRACE v1 (2026-10-27): v2 scalps; others keep 300


def _no_progress_limit(pos):
    try:
        from . import scalp_v2
        if scalp_v2._plan(int(pos.signal_id)):
            return NO_PROGRESS_V2_SEC
    except Exception:  # noqa: BLE001
        pass
    return 300
'''

FM = [
("""    if (now-pos.entry_ts).total_seconds()>=300 and (pos.peak_price or pos.entry_price)<pos.entry_price*1.05:""",
"""    limit = _no_progress_limit(pos)   # TREND GRACE v1: v2 scalps get 10 minutes
    if (now-pos.entry_ts).total_seconds()>=limit and (pos.peak_price or pos.entry_price)<pos.entry_price*1.05:"""),
]


def patch(path, items, tail=''):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in items:
        if src.count(old) != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {src.count(old)} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    src += tail
    compile(src, path, 'exec')
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    f = {n: os.path.join(k, n) for n in ('gamma_confirm.py', 'fast_momentum.py')}
    if a.rollback:
        for p in f.values():
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    out = {f['gamma_confirm.py']: patch(f['gamma_confirm.py'], GC, GC_TAIL),
           f['fast_momentum.py']: patch(f['fast_momentum.py'], FM, FM_TAIL)}
    if a.check:
        print('check ok:', ', '.join(os.path.basename(p) for p in out)); return
    for p, text in out.items():
        shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', p)


if __name__ == '__main__':
    main()
