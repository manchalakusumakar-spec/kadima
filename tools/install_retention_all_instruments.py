"""Install the all-instrument retention update into the LIVE checkout (~/kadima-py).

Files (taken from the fetched branch, git ref FETCH_HEAD by default):
  tools/market_data_retention.py      (replaces the live NIFTY/BANKNIFTY-only version)
  tools/kadima_db_compact.py          (new, one-time VACUUM helper)
  tools/test_market_data_retention.py (new, offline tests on a scratch database)

Safety: refuses unless the live market_data_retention.py is byte-identical to the
version captured in the 11 Oct snapshot (or already updated). Check-only by
default; --apply backs up, writes, compiles, and restores everything on failure.
No service is restarted: the nightly 02:30 timer runs the script fresh each time.

    cd ~/kadima-py && git fetch -q origin ops/retention-all-instruments
    git show FETCH_HEAD:tools/install_retention_all_instruments.py > /tmp/install_ret.py
    python3 /tmp/install_ret.py            # check only
    python3 /tmp/install_ret.py --apply    # install
"""
import argparse
import hashlib
import os
import py_compile
import shutil
import subprocess
import sys
import time

LIVE_RETENTION_SHA256 = '4b78ebe1f27a2af6fcf46442aeca76a59b06aa40e9017d9f89391d7bf0ca34ba'
FILES = ['tools/market_data_retention.py', 'tools/kadima_db_compact.py',
         'tools/test_market_data_retention.py']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.path.expanduser('~/kadima-py'))
    ap.add_argument('--ref', default='FETCH_HEAD')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args(argv)
    new = {}
    for f in FILES:
        r = subprocess.run(['git', '-C', a.root, 'show', f'{a.ref}:{f}'], capture_output=True)
        if r.returncode:
            print(f'BLOCKED: cannot read {f} from {a.ref}. Run: git -C {a.root} fetch -q origin '
                  'ops/retention-all-instruments')
            return 2
        new[f] = r.stdout
    target = os.path.join(a.root, FILES[0])
    current = open(target, 'rb').read() if os.path.exists(target) else b''
    if sha(current) == sha(new[FILES[0]]):
        print('market_data_retention.py: already updated')
    elif sha(current) != LIVE_RETENTION_SHA256:
        print('BLOCKED: live tools/market_data_retention.py differs from the 11 Oct snapshot '
              f'(sha256 {sha(current)[:12]}). Nothing written; send this line for review.')
        return 2
    changes = [f for f in FILES if not os.path.exists(os.path.join(a.root, f))
               or open(os.path.join(a.root, f), 'rb').read() != new[f]]
    for f in FILES:
        print(f"  {'+' if f in changes else '='} {f}")
    if not changes:
        print('Nothing to change.')
        return 0
    if not a.apply:
        print('Check only. Re-run with --apply to install.')
        return 0
    backup = os.path.expanduser(time.strftime('~/backup-retention-%Y%m%d-%H%M%S'))
    os.makedirs(backup)
    written = []
    try:
        for f in changes:
            path = os.path.join(a.root, f)
            if os.path.exists(path):
                shutil.copy2(path, os.path.join(backup, os.path.basename(f)))
            tmp = path + '.new'
            with open(tmp, 'wb') as out:
                out.write(new[f])
            py_compile.compile(tmp, doraise=True)
            os.replace(tmp, path)
            written.append(path)
    except Exception as exc:
        for path in written:
            saved = os.path.join(backup, os.path.basename(path))
            if os.path.exists(saved):
                shutil.copy2(saved, path)
            else:
                os.remove(path)
        print(f'FAILED ({exc}); all files restored from {backup}')
        return 1
    print(f'Installed {len(written)} file(s). Backup: {backup}')
    print(f'Rollback: cp {backup}/market_data_retention.py {target}')
    print('Next: python3 tools/test_market_data_retention.py   (scratch DB only)')
    print('      python3 tools/market_data_retention.py        (dry run on the real DB: counts only)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
