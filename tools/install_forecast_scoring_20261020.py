#!/usr/bin/env python3
"""Install the 2026-10-20 forecast scoring-integrity fix into the LIVE ~/kadima-py checkout.

Forecast-only: replaces kadima/hourly_forecast.py and nothing else. Whole-file swap guarded by
SHA-256 on both sides, fail-closed: if the live file is not exactly the reviewed base (or the
already-installed release), NOTHING is written. Backup goes to
kadima/hourly_forecast.py.bak-scoring-20261020; the new file is py_compiled before it replaces
the live one; a second run changes nothing; --rollback restores the backup and checks its hash.
No service is restarted here: the change takes effect on the next `systemctl restart kadima`.

Usage (on EC2, after market hours, from the release checkout):
  ~/kadima-py/venv/bin/python tools/install_forecast_scoring_20261020.py ~/kadima-py --check
  ~/kadima-py/venv/bin/python tools/install_forecast_scoring_20261020.py ~/kadima-py
  ~/kadima-py/venv/bin/python tools/install_forecast_scoring_20261020.py ~/kadima-py --rollback
"""
import hashlib, os, py_compile, shutil, sys, tempfile
from pathlib import Path

TARGET = 'kadima/hourly_forecast.py'
BASE_SHA = 'c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870'   # live on 2026-10-18
SUFFIX = '.bak-scoring-20261020'
SOURCE = Path(__file__).resolve().parent.parent / TARGET                     # this release's file


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = Path(args[0] if args else '.').expanduser()
    live, bak = root / TARGET, root / (TARGET + SUFFIX)
    new_sha = sha(SOURCE)
    if not live.exists():
        sys.exit(f'ABORT: {live} not found; nothing written')
    cur = sha(live)

    if '--rollback' in sys.argv:
        if cur == BASE_SHA:
            print('already at base', BASE_SHA[:12]); return
        if not bak.exists() or sha(bak) != BASE_SHA:
            sys.exit('ABORT: backup missing or not the reviewed base; nothing restored')
        if cur != new_sha:
            sys.exit(f'ABORT: live file {cur[:12]} is neither base nor this release; nothing restored')
        shutil.copy2(bak, live)
        print('restored', TARGET, sha(live)[:12]); return

    print(f'live {cur[:12]}  base {BASE_SHA[:12]}  release {new_sha[:12]}')
    if '--check' in sys.argv:
        state = 'installed' if cur == new_sha else 'ready' if cur == BASE_SHA else 'MISMATCH'
        print('state', state); sys.exit(0 if state != 'MISMATCH' else 2)
    if cur == new_sha:
        print('already installed; nothing changed'); return
    if cur != BASE_SHA:
        sys.exit('ABORT: live file is not the reviewed base; nothing written')
    with tempfile.TemporaryDirectory() as tmp:
        py_compile.compile(str(SOURCE), cfile=os.path.join(tmp, 'x.pyc'), doraise=True)
    if not bak.exists():
        shutil.copy2(live, bak)
    if sha(bak) != BASE_SHA:
        sys.exit('ABORT: existing backup is not the reviewed base; nothing written')
    tmp_live = live.with_name(live.name + '.new-20261020')
    shutil.copy2(SOURCE, tmp_live)
    os.replace(tmp_live, live)                      # atomic on the same filesystem
    if sha(live) != new_sha:
        shutil.copy2(bak, live)
        sys.exit('ABORT: post-write hash mismatch; base restored')
    print('installed', TARGET, new_sha[:12], 'backup', bak.name)


if __name__ == '__main__':
    main()
