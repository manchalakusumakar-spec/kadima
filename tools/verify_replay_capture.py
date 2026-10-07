"""Run replay checks against this checkout with external sockets prohibited."""
from contextlib import ExitStack
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT/'tools'))
    with tempfile.TemporaryDirectory(prefix='kadima-replay-ci-') as data, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            'KADIMA_DATA_DIR': data, 'KADIMA_REPLAY_CAPTURE': '0',
            'KADIMA_CELESTIAL_ENABLED': '0', 'KADIMA_FAST_MOMENTUM_LIVE': '0'}))
        for target, attribute in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                                  (socket, 'create_connection')):
            stack.enter_context(patch.object(target, attribute,
                side_effect=AssertionError('External sockets forbidden in replay verification')))
        for pattern in ('test_fast_entry_quality.py', 'test_replay_capture.py'):
            suite = unittest.defaultTestLoader.discover(str(ROOT/'tools'), pattern=pattern)
            if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
                return 1
        for name in ('test_offline.py', 'test_audit_fixes.py'):
            try:
                runpy.run_path(str(ROOT/'tools'/name), run_name='__main__')
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
