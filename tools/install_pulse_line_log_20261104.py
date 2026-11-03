#!/usr/bin/env python3
"""PULSE LINE LOG v1 installer (trader 2026-11-04). See kadima/pulse_line_log.py. Display only.
live_forecast.py: one call after each snapshot is built. Anchor-based; backup *.bak-pulse-line-log-20261104.
Usage: --root ~/kadima-py [--check | --rollback]
"""
import argparse, os, shutil, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK, SUFFIX = 'PULSE LINE LOG v1', '.bak-pulse-line-log-20261104'
OLD = """            LIVE[name] = snap
            _DB[name] = self.db
"""
NEW = """            LIVE[name] = snap
            _DB[name] = self.db
            try:   # PULSE LINE LOG v1: one row a minute for the after-market audit (display only)
                from . import pulse_line_log
                pulse_line_log.record(name, snap, self.db, now)
            except Exception:                           # noqa: BLE001
                pass
"""


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--root', required=True)
    ap.add_argument('--check', action='store_true'); ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args(); k = os.path.join(os.path.expanduser(a.root), 'kadima')
    lf, mod = os.path.join(k, 'live_forecast.py'), os.path.join(k, 'pulse_line_log.py')
    if a.rollback:
        if os.path.exists(lf + SUFFIX):
            shutil.copy2(lf + SUFFIX, lf); print('restored', lf)
        if os.path.exists(mod):
            os.remove(mod); print('removed', mod)
        return
    src = open(lf).read()
    if MARK in src:
        sys.exit('live_forecast.py already contains ' + MARK + '; nothing changed')
    if src.count(OLD) != 1:
        sys.exit('live_forecast.py anchor found %d times; nothing changed' % src.count(OLD))
    out = src.replace(OLD, NEW); compile(out, lf, 'exec')
    new_mod = open(os.path.join(HERE, 'kadima', 'pulse_line_log.py')).read(); compile(new_mod, mod, 'exec')
    if a.check:
        print('check ok: live_forecast.py 1/1, pulse_line_log.py compiles'); return
    shutil.copy2(lf, lf + SUFFIX)
    open(mod, 'w').write(new_mod); open(lf, 'w').write(out); print('written', mod, lf)


if __name__ == '__main__':
    main()
