"""Install reviewed recovery sources and request guarded live activation.

Must be run on the AWS host from a separate release checkout, with global auto
paused and no unresolved intraday exposure. Does not place a test order.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo -n python3.')
    for stage, args in (
        ('source_install', ['momentum_recovery/install.py', '--broker-check', '--apply']),
        ('activation', ['tools/activate_execution.py', '--apply', '--enable-move-capture']),
    ):
        result = subprocess.run([sys.executable, *args], cwd=ROOT)
        if result.returncode:
            raise SystemExit('Momentum recovery blocked at '+stage+'; inspect the reported reason. No bypass attempted.')


if __name__ == '__main__':
    main()
