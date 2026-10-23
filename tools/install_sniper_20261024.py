#!/usr/bin/env python3
"""Install Sniper mode (trader decisions 2026-10-24 01:33 IST) into the live KADIMA tree.

  - copies kadima/sniper.py (new file)
  - auto_executor.py: Sniper gate after trend_rules.entry_gate (gamma auto entries only),
    1-lot cap + 15%-of-cash risk check before sizing the order, entry recorded after a
    submitted BUY, no-progress exit in check_exits
  - celestial.py: OI/direction flags become notes while Sniper is live

Anchor-based and fail-closed: if any anchor is missing or not unique in a file, that file
is left untouched and the installer exits non-zero without changing anything. Idempotent
(a file that already has the SNIPER v1 marker is skipped). Backups: <file>.bak-sniper-20261024.

Usage (on EC2):
  python3 tools/install_sniper_20261024.py --root ~/kadima-py            # install
  python3 tools/install_sniper_20261024.py --root ~/kadima-py --check    # dry run
  python3 tools/install_sniper_20261024.py --root ~/kadima-py --rollback # restore backups
"""
import argparse, os, py_compile, shutil, sys, tempfile

SUFFIX = '.bak-sniper-20261024'
MARK = '# SNIPER v1'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AE = [
    # 1. gate, right after the existing trend gate call (before its block check)
    ("""            gate = trend_rules.entry_gate(index_name, chain.index.token, direction, spot,
                                          snap.vwap if snap is not None else None, now_ist())
""",
     """            gate = trend_rules.entry_gate(index_name, chain.index.token, direction, spot,
                                          snap.vwap if snap is not None else None, now_ist())
            # SNIPER v1 (2026-10-24): few, early, with-trend, confirmed gamma entries only.
            from . import sniper
            _sn = sniper.gate(index_name, chain.index.token, direction, spot,
                              snap.vwap if snap is not None else None, now_ist(),
                              gate=gate, selected=selected, manual=manual_request)
            if _sn.get('block'):
                return self._reject(_sn['block'], index_name, direction)
            if _sn.get('override'):
                gate = {'trend': gate.get('trend'), 'stop_level': _sn.get('stop_level')}
"""),
    # 2. 1-lot cap and cash-risk check, final sizing before the order
    ("""        quantity = allowed_lots * lot_size
""",
     """        # SNIPER v1: 1 lot, skip when stop risk > 15% of available cash.
        from . import sniper
        allowed_lots, _sn_block = sniper.size(kind, manual_request, limit_price, stop, lot_size,
                                              allowed_lots, capital_guard.broker_available_funds())
        if _sn_block or allowed_lots < 1:
            return self._reject(_sn_block or 'sniper_no_lots', index_name, direction)
        quantity = allowed_lots * lot_size
"""),
    # 3. remember Sniper entries (daily cap, after-loss rule, no-progress exit)
    ("""        result = execution_ledger.submit(pos, 'BUY', quantity, limit_price)
""",
     """        result = execution_ledger.submit(pos, 'BUY', quantity, limit_price)
        # SNIPER v1
        from . import sniper
        sniper.after_submit(kind, manual_request, pos)
"""),
    # 4. no-progress exit for Sniper trades
    ("""            elif getattr(pos,'kind',None)=='fast_momentum' and fast_momentum.timeout_reason(pos,now):
                reason = fast_momentum.timeout_reason(pos,now)
""",
     """            elif getattr(pos,'kind',None)=='fast_momentum' and fast_momentum.timeout_reason(pos,now):
                reason = fast_momentum.timeout_reason(pos,now)
            elif getattr(pos, 'kind', None) == 'gamma_blast' and _sniper_no_progress(pos, now):  # SNIPER v1
                reason = 'sniper_no_progress'
"""),
]
AE_TAIL = '''

def _sniper_no_progress(pos, now):  # SNIPER v1
    from . import sniper
    return sniper.no_progress(pos, now)
'''
CEL = [
    ("""        row['eligible'] = not reasons
""",
     """        # SNIPER v1: OI/direction flags are notes while Sniper is live.
        from . import sniper
        reasons = sniper.relax_reasons(reasons, notes)
        row['eligible'] = not reasons
"""),
]


def patch_text(text, patches, tail=''):
    if MARK in text:
        return None, 'already installed'
    for old, _new in patches:
        n = text.count(old)
        if n != 1:
            return False, f'anchor found {n} times: {old.strip().splitlines()[0][:70]}'
    for old, new in patches:
        text = text.replace(old, new)
    return text + tail, 'patched'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.path.expanduser('~/kadima-py'))
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(a.root, 'kadima')
    files = {'auto_executor.py': (AE, AE_TAIL), 'celestial.py': (CEL, '')}
    if a.rollback:
        for name in files:
            b = os.path.join(k, name + SUFFIX)
            if os.path.exists(b):
                shutil.copy2(b, os.path.join(k, name)); print('restored', name)
        sn = os.path.join(k, 'sniper.py')
        if os.path.exists(sn):
            os.remove(sn); print('removed sniper.py')
        return 0
    staged = {}
    for name, (patches, tail) in files.items():
        path = os.path.join(k, name)
        text = open(path).read()
        new, msg = patch_text(text, patches, tail)
        print(f'{name}: {msg}')
        if new is False:
            print('ABORT: nothing changed'); return 1
        if new:
            tmp = tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, dir=k)
            tmp.write(new); tmp.close()
            try:
                py_compile.compile(tmp.name, doraise=True)
            except py_compile.PyCompileError as exc:
                os.unlink(tmp.name); print('ABORT: compile', name, exc); return 1
            staged[name] = tmp.name
    src = os.path.join(HERE, 'kadima', 'sniper.py')
    py_compile.compile(src, doraise=True)
    if a.check:
        for t in staged.values():
            os.unlink(t)
        print('check ok: all anchors unique, patched files compile'); return 0
    dst = os.path.join(k, 'sniper.py')
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst); print('sniper.py copied')
    for name, tmp in staged.items():
        path = os.path.join(k, name)
        st = os.stat(path)
        if not os.path.exists(path + SUFFIX):
            shutil.copy2(path, path + SUFFIX)
        os.replace(tmp, path)
        try:
            os.chown(path, st.st_uid, st.st_gid); os.chmod(path, st.st_mode)
        except PermissionError:
            pass
        print(f'{name} installed (backup {name}{SUFFIX})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
