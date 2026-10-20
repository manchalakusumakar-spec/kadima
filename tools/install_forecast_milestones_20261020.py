#!/usr/bin/env python3
"""Install the 2026-10-20 milestone chart + 5-point reforecast into the LIVE ~/kadima-py checkout.

Forecast-only: replaces kadima/hourly_forecast.py and nothing else (the panel files are copied
separately by tools/install_celestial_embed.py). Whole-file swap guarded by SHA-256, fail-closed:
the live file must be exactly the reviewed live base or the PR #47 scoring release, otherwise
NOTHING is written. Backup goes to kadima/hourly_forecast.py.bak-milestones-20261020; the new file
is py_compiled before it replaces the live one; a second run changes nothing; --rollback restores
the backup and checks its hash. No service is restarted here.

Usage (on EC2, after market hours, from the release checkout):
  ~/kadima-py/venv/bin/python tools/install_forecast_milestones_20261020.py ~/kadima-py --check
  ~/kadima-py/venv/bin/python tools/install_forecast_milestones_20261020.py ~/kadima-py
  ~/kadima-py/venv/bin/python tools/install_forecast_milestones_20261020.py ~/kadima-py --rollback
"""
import hashlib, os, py_compile, shutil, sys, tempfile
from pathlib import Path

TARGET = 'kadima/hourly_forecast.py'
PREDECESSORS = {
    'c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870': 'live base (2026-10-18)',
    'f4fb1b07ce4026557ca5a121f6ab3d745e1a4c76c7e6f7cc5806450bae936cd9': 'PR #47 scoring release',
}
SUFFIX = '.bak-milestones-20261020'
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
        if cur in PREDECESSORS:
            print('already at', PREDECESSORS[cur], cur[:12]); return
        if not bak.exists() or sha(bak) not in PREDECESSORS:
            sys.exit('ABORT: backup missing or not a reviewed predecessor; nothing restored')
        if cur != new_sha:
            sys.exit(f'ABORT: live file {cur[:12]} is neither a predecessor nor this release; nothing restored')
        shutil.copy2(bak, live)
        print('restored', TARGET, sha(live)[:12], PREDECESSORS[sha(live)]); return

    print(f'live {cur[:12]}  release {new_sha[:12]}')
    if '--check' in sys.argv:
        state = 'installed' if cur == new_sha else 'ready' if cur in PREDECESSORS else 'MISMATCH'
        print('state', state, PREDECESSORS.get(cur, ''))
        sys.exit(0 if state != 'MISMATCH' else 2)
    if cur == new_sha:
        print('already installed; nothing changed'); return
    if cur not in PREDECESSORS:
        sys.exit('ABORT: live file is not a reviewed predecessor; nothing written')
    with tempfile.TemporaryDirectory() as tmp:
        py_compile.compile(str(SOURCE), cfile=os.path.join(tmp, 'x.pyc'), doraise=True)
    if not bak.exists():
        shutil.copy2(live, bak)
    if sha(bak) != cur:
        sys.exit('ABORT: existing backup does not match the live file; nothing written')
    tmp_live = live.with_name(live.name + '.new-milestones')
    shutil.copy2(SOURCE, tmp_live)
    os.replace(tmp_live, live)                      # atomic on the same filesystem
    if sha(live) != new_sha:
        shutil.copy2(bak, live)
        sys.exit('ABORT: post-write hash mismatch; predecessor restored')
    print('installed', TARGET, new_sha[:12], 'over', PREDECESSORS[cur], 'backup', bak.name)


if __name__ == '__main__':
    main()
