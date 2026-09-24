"""Focused, source-matched recovery install; never replaces the live executor.

Reuse the MCX installer's reviewed exposure, process, backup and restart checks.
No live activation unless the separate go_live.py command is explicitly run.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


for name, expected in {
    'stock_fast_update/install.py': '00ac2c1ba6b5a68a21cd2bf532dd53e1440e6e35',
    'mcx_live_update/install.py': '3d7042653f63c4857658f85cc6cd2e1551845324',
}.items():
    if digest((ROOT/name).read_bytes()) != expected:
        raise RuntimeError('recovery_installer_dependency_changed')

spec = importlib.util.spec_from_file_location('_momentum_mcx_installer', ROOT/'mcx_live_update/install.py')
mcx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcx)
base = mcx.base
base.FILES = {
    'run.py': 'cb701bb145a996635d689376a6b24ab2dac030e4',
    'kadima/stock_fast.py': 'e84932b645d584787df21e1bb94ce927d80b6cc3',
    'kadima/stock_execution.py': '880c29cf2e9a7ca78c094c84d76320b1b8fa3625',
    'kadima/stock_fast_api.py': '0d9b50418d6cc12752ebf96114e98504eaca455d',
    'kadima/preparation_diagnostics.py': None,
    'kadima/move_capture.py': None,
    'kadima/move_capture_service.py': None,
    'kadima/move_capture_handoff.py': None,
    'kadima/move_capture_execution.py': None,
    'kadima/momentum_quote_watch.py': None,
}


if __name__ == '__main__':
    try:
        import fcntl
        with open('/run/lock/kadima-activation.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            base.main()
    except base.Blocked as exc:
        print('BLOCKED:', str(exc))
        raise SystemExit(1)
    except Exception as exc:
        print('BLOCKED:', type(exc).__name__, '(details suppressed)')
        raise SystemExit(1)
