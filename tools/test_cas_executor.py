#!/usr/bin/env python3
"""Compatibility entrypoint for the network-denied CAS regression suite."""
import runpy
from pathlib import Path
if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).with_name('test_cas_safety.py')), run_name='__main__')
