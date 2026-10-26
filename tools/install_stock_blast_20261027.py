#!/usr/bin/env python3
"""STOCK BLAST v1 installer (trader 2026-10-27 01:00 IST). See kadima/stock_blast.py.

Whole files (installed only if the live copy still has the expected pre-change md5):
  stock_blast.py              new
  move_capture_execution.py   stock strike rules, 4 strikes each side, entries to 15:10,
                              per-strike rejection log
  stock_fast.py               a running stock blast (peak >= +5%) is not cut at the fixed
                              holding deadline
  risk_budget.py              optional per-call gates (None = index gates unchanged)
Anchor patches:
  auto_executor.py            stock fast entries use stock strike rules, confidence sizing and
                              stock risk gates; stock fast positions get the peak runner exit.
Nothing is written unless every check passes and everything compiles.
Backups *.bak-stock-blast-20261027.
Usage: --root ~/kadima-py --data /var/lib/kadima [--check | --rollback]
"""
import argparse, hashlib, json, os, shutil, sys

MARK = 'STOCK BLAST v1'
SUFFIX = '.bak-stock-blast-20261027'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WHOLE = {   # file -> md5 of the live copy this change was built against
    'move_capture_execution.py': 'cce0f1bdcf4e1f7b31788be7ba9d5073',
    'stock_fast.py': '8f4abd34b23e6273f6bc12b11fb5d390',
    'risk_budget.py': '09c0817b158f0fde7e4b5e93746c3636',
}

AE = [(
"""        ranked = [r for r in strike_selector.rank(chain, direction, spot, days_to_expiry)
                  if not r.rejected and str(r.quote.token) in valid_tokens]
""",
"""        from . import stock_blast   # STOCK BLAST v1: stock rules for stock fast-momentum entries
        _sb = stock_blast.applies(chain, fast)
        ranked = [r for r in (stock_blast.rank(chain, direction, spot, days_to_expiry, fast.get('score', 0))
                              if _sb else strike_selector.rank(chain, direction, spot, days_to_expiry))
                  if not r.rejected and str(r.quote.token) in valid_tokens]
"""), (
"""        if _nocap:
            allocation = _scalp_nocap_allocation(allocation)
""",
"""        if _nocap:
            allocation = _scalp_nocap_allocation(allocation)
        if _sb:   # STOCK BLAST v1: size by trigger confidence
            allocation = stock_blast.allocation(allocation, fast.get('score', 0))
"""), (
"""            max_lots=min(1, MAX_LOTS_PER_SIGNAL) if gap else (NOCAP_MAX_LOTS if _nocap else MAX_LOTS_PER_SIGNAL))
""",
"""            max_lots=min(1, MAX_LOTS_PER_SIGNAL) if gap else (NOCAP_MAX_LOTS if _nocap else MAX_LOTS_PER_SIGNAL),
            gates=stock_blast.risk_gates(fast.get('score', 0)) if _sb else None)   # STOCK BLAST v1
"""), (
"""                if not pos.exit_reason:
                    scalp_v2.maybe_partial(self, pos, ltp)   # spec Scalp T1: book half at +12%
""",
"""                if not pos.exit_reason:
                    scalp_v2.maybe_partial(self, pos, ltp)   # spec Scalp T1: book half at +12%
                if v2_stop is None:   # STOCK BLAST v1: -35% hard stop, +12% locks +3%, peak runner
                    from . import stock_blast
                    sb_stop = stock_blast.managed_stop(pos, ltp)
                    if sb_stop is not None:
                        sb_base, sb_eff, sb_active, sb_reason = sb_stop
                        if sb_reason and not pos.exit_reason:
                            pos.exit_reason = sb_reason
                        else:
                            structural = pos.structural_stop = max(structural or 0, sb_base)
                            pos.effective_stop, pos.trail_active = sb_eff, bool(sb_active)
""")]


def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


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
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    ae, sb = os.path.join(k, 'auto_executor.py'), os.path.join(k, 'stock_blast.py')
    cfg = os.path.join(a.data, 'stock_blast.json')
    if a.rollback:
        for name in list(WHOLE) + ['auto_executor.py']:
            p = os.path.join(k, name)
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        json.dump({'mode': 'off'}, open(cfg, 'w')); print('mode off', cfg)
        if os.path.exists(sb):
            os.remove(sb); print('removed', sb)
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
    new_sb = open(os.path.join(HERE, 'kadima', 'stock_blast.py')).read()
    compile(new_sb, sb, 'exec')
    t_ae = patch(ae, AE)
    if a.check:
        print('check ok: 3 whole files match expected md5, auto_executor.py 4/4 anchors, all compile')
        return
    for name in list(WHOLE) + ['auto_executor.py']:
        p = os.path.join(k, name)
        shutil.copy2(p, p + SUFFIX)
    open(sb, 'w').write(new_sb); print('written', sb)
    for name, src in new.items():
        open(os.path.join(k, name), 'w').write(src); print('written', name)
    open(ae, 'w').write(t_ae); print('written', ae)
    json.dump({'mode': 'live'}, open(cfg, 'w')); print('config', cfg, 'live')


if __name__ == '__main__':
    main()
