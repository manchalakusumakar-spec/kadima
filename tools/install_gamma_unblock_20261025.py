#!/usr/bin/env python3
"""GAMMA CONFIRM v1 + GAMMA UNBLOCK v1 (trader decisions 2026-10-25). Install AFTER Sniper.

1. kadima/gamma_confirm.py (new): Celestial CONFIRMED = score >= threshold AND strike
   concentration (HHI) >= 0.09 AND 09:45-10:00 IST. Auto gamma entries need CONFIRMED.
2. celestial_scanner.py: rows carry strike_concentration_hhi; a strike one step OTM
   (the ladder is ATM +/- 1) counts as tradeable. 12 of 14 rule winners on 21-23 Oct
   were refused as contract_not_tradeable only because they were one strike OTM.
3. celestial.py: tier comes from gamma_confirm (score-only BLAST/CONFIRMED become ACTIVE).
4. auto_executor.py: (a) CONFIRMED gate for non-manual gamma entries, right after the
   Sniper gate; (b) when no confirmed 5-min swing exists yet, a gamma entry uses the
   trend band stop level instead of being refused (35% hard cap still applies).

Anchor-based: every anchor must be found exactly once in the current (post-Sniper) file,
or nothing is written. Backups: <file>.bak-gammaunblock-20261025.
Usage: --root ~/kadima-py [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'GAMMA CONFIRM v1'
SUFFIX = '.bak-gammaunblock-20261025'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCANNER = [
    ("""            otm_allowed = getattr(result,'score',0)>=90 and metrics['premium_move_pct']>=15
""",
     """            otm_allowed = getattr(result,'score',0)>=90 and metrics['premium_move_pct']>=15
            # GAMMA CONFIRM v1 / UNBLOCK (2026-10-25): one strike OTM is tradeable too.
            otm_allowed = otm_allowed or abs(q.strike - chain.atm) <= chain.index.strike_step
"""),
    ("""                sector_score=0,event_risk_penalty=0,**metrics)
""",
     """                strike_concentration_hhi=getattr(components,'strike_concentration_hhi',None),  # GAMMA CONFIRM v1
                sector_score=0,event_risk_penalty=0,**metrics)
"""),
]
CEL = [
    ("""        row['tier'] = ('BLAST' if score >= 90 else 'CONFIRMED' if score >= 85 else
                       'ACTIVE' if score >= 70 else 'WATCH' if score >= 40 else 'RADAR')
""",
     """        row['tier'] = ('BLAST' if score >= 90 else 'CONFIRMED' if score >= 85 else
                       'ACTIVE' if score >= 70 else 'WATCH' if score >= 40 else 'RADAR')
        # GAMMA CONFIRM v1 (2026-10-25): CONFIRMED = score>=threshold + HHI>=0.09 + 09:45-10:00.
        try:
            from . import gamma_confirm
            row['tier'] = gamma_confirm.tier(row, now, cfg['score_threshold'], row['tier'])
        except Exception:  # noqa: BLE001 - display only
            pass
"""),
]
AE = [
    ("""            if _sn.get('override'):
                gate = {'trend': gate.get('trend'), 'stop_level': _sn.get('stop_level')}
""",
     """            if _sn.get('override'):
                gate = {'trend': gate.get('trend'), 'stop_level': _sn.get('stop_level')}
            # GAMMA CONFIRM v1 (2026-10-25): auto gamma entries only for Celestial CONFIRMED.
            if not manual_request:
                from . import gamma_confirm
                _gc = gamma_confirm.entry_block(
                    selected, now_ist(),
                    minimum if minimum is not None else celestial.read_config()['score_threshold'])
                if _gc:
                    return self._reject(_gc, index_name, direction)
"""),
    ("""        if structure is None:
            return self._reject('fresh_structure_or_valid_expiry_unavailable', index_name, direction)
""",
     """        if structure is None and kind == 'gamma_blast':
            # GAMMA CONFIRM v1 / UNBLOCK: no confirmed 5-min swing yet (a clean one-way move):
            # use the trend band stop level instead of refusing. 35% hard cap still applies.
            structure = _gamma_band_structure(chain, index_name, direction, spot, snap)
        if structure is None:
            return self._reject('fresh_structure_or_valid_expiry_unavailable', index_name, direction)
"""),
]
AE_TAIL = '''

def _gamma_band_structure(chain, index_name, direction, spot, snap):  # GAMMA CONFIRM v1
    """(spot stop level, days to expiry) from the trend band, or None."""
    try:
        from . import trend_rules
        now = now_ist()
        expiry = datetime.strptime(chain.scrips.nearest_expiry(chain.index), '%d%b%Y')
        expiry = expiry.replace(tzinfo=now.tzinfo, hour=config.MARKET_CLOSE[0],
                                minute=config.MARKET_CLOSE[1])
        days = (expiry - now).total_seconds() / 86400
        vwap = snap.vwap if snap is not None else None
        b = trend_rules.band(index_name, chain.index.token, spot, vwap, now)
        if days <= 0 or b is None:
            return None
        level = trend_rules.stop_level(direction, b)
        if not trade_policy.positive(level) or (direction == 'CE' and level >= spot) \\
                or (direction == 'PE' and level <= spot):
            return None
        audit.log('gamma_band_structure', index=index_name, direction=direction, spot=spot,
                  level=round(level, 2), source=b.get('source'))
        return level, days
    except Exception as exc:  # noqa: BLE001 - fall back to the old refusal
        audit.log('gamma_band_structure_error', index=index_name, error=str(exc)[:200])
        return None
'''
FILES = {'celestial_scanner.py': (SCANNER, ''), 'celestial.py': (CEL, ''),
         'auto_executor.py': (AE, AE_TAIL)}


def plan(root):
    out = {}
    for name, (patches, tail) in FILES.items():
        path = os.path.join(root, 'kadima', name)
        src = open(path).read()
        if MARK in src:
            sys.exit(f'{name}: already contains {MARK}; nothing changed')
        new = src
        for old, rep in patches:
            n = new.count(old)
            if n != 1:
                sys.exit(f'{name}: anchor found {n} times (need 1): {old.strip()[:70]!r}; nothing changed')
            new = new.replace(old, rep)
        new += tail
        compile(new, path, 'exec')
        out[path] = new
    mod = os.path.join(HERE, 'kadima', 'gamma_confirm.py')
    out[os.path.join(root, 'kadima', 'gamma_confirm.py')] = open(mod).read()
    compile(out[os.path.join(root, 'kadima', 'gamma_confirm.py')], 'gamma_confirm.py', 'exec')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    root = os.path.expanduser(a.root)
    if a.rollback:
        for name in FILES:
            p = os.path.join(root, 'kadima', name)
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', name)
        g = os.path.join(root, 'kadima', 'gamma_confirm.py')
        if os.path.exists(g):
            os.remove(g); print('removed gamma_confirm.py')
        return
    out = plan(root)
    if a.check:
        for p in out:
            print(os.path.basename(p) + ': ok')
        print('check ok: all anchors unique, patched files compile'); return
    for p, text in out.items():
        if os.path.exists(p):
            shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text)
        print('written', os.path.basename(p))


if __name__ == '__main__':
    main()
