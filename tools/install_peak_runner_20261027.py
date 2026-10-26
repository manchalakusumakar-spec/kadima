#!/usr/bin/env python3
"""PEAK RUNNER v1 installer (trader 2026-10-27 00:15 IST). See kadima/scalp_runner.py.

scalp_v2.py       the fixed +20% target exit is replaced (mode live) by the runner: floor +20%,
                  wide backstop, and a 15-second peak-forecast exit; thesis exits are skipped
                  while a runner is active.
auto_executor.py  monitor loop polls every 2 s while a runner is open.
Anchor-based; nothing written unless every anchor matches once and compiles.
Backups *.bak-peak-runner-20261027. Usage: --root ~/kadima-py --data /var/lib/kadima
[--mode shadow|live] [--check | --rollback]
"""
import argparse, json, os, shutil, sys

MARK = 'PEAK RUNNER v1'
SUFFIX = '.bak-peak-runner-20261027'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SV = [(
"""        reason = 'scalp_v2_target_20pct' if ltp >= entry * (1 + TARGET_PCT) - 1e-9 else None
        reason = reason or thesis_exit(pos, ltp)""",
"""        from . import scalp_runner   # PEAK RUNNER v1: forecast the peak instead of a fixed +20%
        try:
            stop, reason, _handled = scalp_runner.step(int(sid), entry, peak, ltp, stop)
        except Exception as exc:  # noqa: BLE001 - old target on any runner error
            audit.log('scalp_runner_error', error=str(exc)[:200])
            reason, _handled = None, False
        if not _handled:
            reason = 'scalp_v2_target_20pct' if ltp >= entry * (1 + TARGET_PCT) - 1e-9 else None
        if not scalp_runner.active(int(sid)):
            reason = reason or thesis_exit(pos, ltp)""")]

AE = [(
"""                interval = min(interval, protection.monitor_interval(self))
""",
"""                interval = min(interval, protection.monitor_interval(self))
                from . import scalp_runner   # PEAK RUNNER v1: 2-s checks while a runner is open
                interval = min(interval, scalp_runner.interval(self))
""")]


def patch(path, items):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in items:
        n = src.count(old)
        if n != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {n} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    compile(src, path, 'exec')
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--data', default='/var/lib/kadima')
    ap.add_argument('--mode', choices=('shadow', 'live'), default='shadow')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    sv, ae, sr = (os.path.join(k, n) for n in ('scalp_v2.py', 'auto_executor.py', 'scalp_runner.py'))
    cfg = os.path.join(a.data, 'scalp_runner.json')
    if a.rollback:
        for p in (sv, ae):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        if os.path.exists(sr):
            os.remove(sr); print('removed', sr)
        json.dump({'mode': 'off'}, open(cfg, 'w')); print('mode off', cfg)
        return
    new_sr = open(os.path.join(HERE, 'kadima', 'scalp_runner.py')).read()
    compile(new_sr, sr, 'exec')
    t_sv, t_ae = patch(sv, SV), patch(ae, AE)
    if a.check:
        print('check ok: scalp_v2.py 1/1, auto_executor.py 1/1, scalp_runner.py compiles'); return
    for p in (sv, ae):
        shutil.copy2(p, p + SUFFIX)
    open(sr, 'w').write(new_sr); print('written', sr)
    open(sv, 'w').write(t_sv); print('written', sv)
    open(ae, 'w').write(t_ae); print('written', ae)
    json.dump({'mode': a.mode}, open(cfg, 'w')); print('config', cfg, a.mode)


if __name__ == '__main__':
    main()
