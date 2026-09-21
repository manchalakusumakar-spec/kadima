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

spec=importlib.util.spec_from_file_location('_gap_base_installer',BASE_PATH)
base=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=base
spec.loader.exec_module(base)

base.FILES={
    'run.py':'9036548a589752d26efe2bf952b25cdc590a7cdf',
    'kadima/full_universe_scanner.py':(
        '8c01cc55e7348244212c6012ed5468bdb9853895',
        'a0f874a87ce63ca920e99b4724e36bae86e68ce1'),
    'kadima/stock_fast.py':'d4c537156dbb080a50b249d5164e9506f1e1b8dd',
    'kadima/auto_executor.py':'f830afebc49b764e48b91bd4c61f2260e769d25f',
    'kadima/gap_and_go.py':'b26599c469ec9e0af4dcd9503aa6965365bb4647',
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
