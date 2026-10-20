#!/usr/bin/env python3
"""Install the NIFTY 50 breadth SHADOW recorder into the LIVE ~/kadima-py checkout.

Forecast-only, shadow-only: replaces kadima/hourly_forecast.py (adds a 3-line hook that records
breadth beside the forecast) and adds kadima/nifty_breadth.py. Whole-file swap guarded by SHA-256,
fail-closed: the live hourly_forecast.py must be exactly the PR #48 release, otherwise NOTHING is
written. Backups: <file>.bak-breadth-20261020. --rollback restores them and removes the new module.
Nothing records until $KADIMA_DATA_DIR/nifty50_weights.json (dated, 45-55 names) is provided.
No service is restarted here.

  ~/kadima-py/venv/bin/python tools/install_nifty_breadth_shadow_20261020.py ~/kadima-py --check
  ~/kadima-py/venv/bin/python tools/install_nifty_breadth_shadow_20261020.py ~/kadima-py
  ~/kadima-py/venv/bin/python tools/install_nifty_breadth_shadow_20261020.py ~/kadima-py --rollback
"""
import hashlib, os, py_compile, shutil, sys, tempfile
from pathlib import Path

HF, NEW_MOD = 'kadima/hourly_forecast.py', 'kadima/nifty_breadth.py'
BASE_SHA = '16b92751a7a1dfc024ac09fdeaa4fb7759943b587452e98912968e64022598e8'   # PR #48 release
SUFFIX = '.bak-breadth-20261020'
SRC = Path(__file__).resolve().parent.parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = Path(args[0] if args else '.').expanduser()
    live, bak, mod = root / HF, root / (HF + SUFFIX), root / NEW_MOD
    new_sha, mod_sha = sha(SRC / HF), sha(SRC / NEW_MOD)
    if not live.exists():
        sys.exit(f'ABORT: {live} not found; nothing written')
    cur = sha(live)
    if '--rollback' in sys.argv:
        if cur == BASE_SHA and not mod.exists():
            print('already at base'); return
        if not bak.exists() or sha(bak) != BASE_SHA:
            sys.exit('ABORT: backup missing or not the PR #48 release; nothing restored')
        if cur not in (new_sha, BASE_SHA):
            sys.exit(f'ABORT: live file {cur[:12]} is neither base nor this release; nothing restored')
        shutil.copy2(bak, live)
        if mod.exists():
            mod.unlink()
        print('restored', HF, sha(live)[:12], 'and removed', NEW_MOD); return
    installed = cur == new_sha and mod.exists() and sha(mod) == mod_sha
    print(f'live {cur[:12]}  base {BASE_SHA[:12]}  release {new_sha[:12]}  module {mod_sha[:12]}')
    if '--check' in sys.argv:
        state = 'installed' if installed else 'ready' if cur == BASE_SHA else 'MISMATCH'
        print('state', state); sys.exit(0 if state != 'MISMATCH' else 2)
    if installed:
        print('already installed; nothing changed'); return
    if cur != BASE_SHA:
        sys.exit('ABORT: live file is not the PR #48 release; nothing written')
    if mod.exists() and sha(mod) != mod_sha:
        sys.exit('ABORT: an unknown kadima/nifty_breadth.py exists; nothing written')
    with tempfile.TemporaryDirectory() as tmp:
        for f in (HF, NEW_MOD):
            py_compile.compile(str(SRC / f), cfile=os.path.join(tmp, 'x.pyc'), doraise=True)
    if not bak.exists():
        shutil.copy2(live, bak)
    if sha(bak) != BASE_SHA:
        sys.exit('ABORT: existing backup is not the PR #48 release; nothing written')
    shutil.copy2(SRC / NEW_MOD, mod.with_name(mod.name + '.new'))
    os.replace(mod.with_name(mod.name + '.new'), mod)
    shutil.copy2(SRC / HF, live.with_name(live.name + '.new-breadth'))
    os.replace(live.with_name(live.name + '.new-breadth'), live)
    if sha(live) != new_sha or sha(mod) != mod_sha:
        shutil.copy2(bak, live); mod.unlink()
        sys.exit('ABORT: post-write hash mismatch; base restored')
    print('installed', HF, new_sha[:12], 'and', NEW_MOD, mod_sha[:12], '(shadow only)')


if __name__ == '__main__':
    main()
