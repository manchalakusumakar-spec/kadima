"""Guarded Gap live source installer.

Uses the reviewed stock-fast transaction installer but expands the allowed source
set only to this Gap release. It never changes live flags; activation is separate.
"""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
BASE_PATH=ROOT/'stock_fast_update/install.py'
COMPOSED_EXECUTOR='6161c04be1266e5e9b010f67a9466c22c0fc8ef0'
DEPLOYED_SESSION_MULTI_EXECUTOR='346f780545139367a4152725743c7275a14e8239'

spec=importlib.util.spec_from_file_location('_gap_base_installer',BASE_PATH)
base=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=base
spec.loader.exec_module(base)

# The AWS executor is already the reviewed stock-fast + session-auto +
# multi-position composition. Preserve those layers while adding Gap.
_original_read_file=base.read_file
_composed_path=ROOT/'gap_live_update/auto_executor.py'
if base.digest(_original_read_file(_composed_path)) != COMPOSED_EXECUTOR:
    raise base.Blocked('composed_gap_executor_hash_mismatch')

def _gap_read_file(path, missing=False):
    path=Path(path)
    if path == ROOT/'kadima/auto_executor.py':
        return _original_read_file(_composed_path, missing=missing)
    return _original_read_file(path, missing=missing)

base.read_file=_gap_read_file

base.FILES={
    'run.py':'9036548a589752d26efe2bf952b25cdc590a7cdf',
    'kadima/full_universe_scanner.py':(
        '8c01cc55e7348244212c6012ed5468bdb9853895',
        'a0f874a87ce63ca920e99b4724e36bae86e68ce1'),
    'kadima/stock_fast.py':'d4c537156dbb080a50b249d5164e9506f1e1b8dd',
    'kadima/auto_executor.py':DEPLOYED_SESSION_MULTI_EXECUTOR,
    # Gap files may be absent on the first live installation. An earlier
    # paper-only gap_and_go.py is also an accepted reviewed starting point.
    'kadima/gap_and_go.py':(None,'b26599c469ec9e0af4dcd9503aa6965365bb4647'),
    'kadima/gap_baseline.py':None,
    'kadima/gap_live.py':None,
}


def main():
    base.main()


if __name__=='__main__':
    try:
        fd=os.open('/run/lock/kadima-gap-live-install.lock',
                   os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            main()
    except base.Blocked as exc:
        print('BLOCKED:',str(exc))
        raise SystemExit(1)
    except Exception as exc:
        print('BLOCKED:',type(exc).__name__,'(details suppressed; no credential output)')
        raise SystemExit(1)
