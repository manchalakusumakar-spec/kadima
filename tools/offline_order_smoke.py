"""Isolated order/ledger rehearsal; no broker, live config, services or live DB.

Uses the reviewed release's existing synthetic execution tests and adds a
complete synthetic BUY -> partial/full fills -> SELL -> flat demonstration.
Run from a separate extracted release, never from the installed engine tree.
--compare-installed reads only six explicit Python source files and verifies
that they match the reviewed multi-position release. It does not import them.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import nullcontext
from dataclasses import dataclass
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

BASE_RELEASE = '6dba0c82c3c097a58fc376303470b99f4404ab46'
PRODUCTION = Path('/home/ubuntu/kadima-py')
FILES_TO_COMPARE = (
    'kadima/angel_orders.py', 'kadima/execution_ledger.py', 'kadima/storage.py',
    'kadima/portfolio_capacity.py', 'kadima/auto_executor.py',
    'kadima/market_session_gate.py',
)


class IsolationError(RuntimeError):
    pass


class Guard:
    """Defense in depth for these reviewed tests, not a hostile-code sandbox."""
    def __init__(self, source, scratch, installed=None):
        self.source = Path(source).resolve()
        self.scratch = Path(scratch).resolve()
        self.stdlib = Path(sysconfig.get_path('stdlib')).resolve()
        self.read_only_installed = ({(Path(installed)/name).absolute()
                                     for name in FILES_TO_COMPARE}
                                    if installed is not None else set())
        self.blocked = 0

    def reject(self):
        self.blocked += 1
        raise IsolationError('offline_isolation_boundary')

    def path(self, value, dir_fd=None):
        if isinstance(value, int):
            # tempfile/sqlite can legitimately reopen an already-created FD.
            return Path(os.readlink('/proc/self/fd/'+str(value))).resolve()
        p = Path(os.fsdecode(value))
        if not p.is_absolute():
            if dir_fd is not None and dir_fd != -1:
                p = Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/p
            else:
                p = Path.cwd()/p
        return p.resolve()

    def readable(self, value):
        p = self.path(value)
        if p in self.read_only_installed:
            return True
        if p.is_relative_to(self.scratch):
            return True
        if p.is_relative_to(self.source):
            # Real configuration/session/credentials are never needed here.
            if p.name in ('config.py', 'session.py', 'credentials.json', '.env'):
                return False
            return True
        return (p.is_relative_to(self.stdlib)
                and not any(n in p.parts for n in ('site-packages', 'dist-packages')))

    def writable(self, value, dir_fd=None):
        return self.path(value, dir_fd).is_relative_to(self.scratch)

    def __call__(self, event, args):
        if (event.startswith('socket.') or event.startswith('subprocess.')
                or event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.fork',
                             'os.forkpty', 'os.kill', 'os.killpg', 'ctypes.dlopen')):
            self.reject()
        if event == 'import':
            name = args[0].split('.')[0]
            if name in ('SmartApi', 'smartapi', 'pyotp', 'requests', 'httpx', 'httpx2'):
                self.reject()
        if event == 'open':
            path, mode, flags = args
            writes = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writes:
                if not self.writable(path): self.reject()
            elif not self.readable(path):
                self.reject()
        elif event == 'sqlite3.connect':
            if not self.writable(args[0]): self.reject()
        elif event in ('os.mkdir', 'os.chmod', 'os.chown'):
            if not self.writable(args[0], args[-1] if len(args) >= 3 else None):
                self.reject()
        elif event in ('os.remove', 'os.rmdir'):
            if not self.writable(args[0], args[1] if len(args) > 1 else None):
                self.reject()
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            # No symlink or hardlink is needed to run the selected tests.
            if event != 'os.rename': self.reject()
            if not (self.writable(args[0], args[2] if len(args) > 2 else None)
                    and self.writable(args[1], args[3] if len(args) > 3 else None)):
                self.reject()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def compare_installed(root, release, multi):
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root or root == release:
        raise ValueError('separate_canonical_installed_directory_required')
    session = load_module('_offline_compare_session', release/'session_auto/install.py')
    expected = {
        name: (release/name).read_bytes()
        for name in FILES_TO_COMPARE
        if name not in ('kadima/auto_executor.py', 'kadima/market_session_gate.py')
    }
    expected['kadima/auto_executor.py'] = multi.patcher.patch_executor(
        session.patch_executor((release/'kadima/auto_executor.py').read_bytes()))
    expected['kadima/market_session_gate.py'] = (release/'session_auto/gate.py').read_bytes()
    for name in FILES_TO_COMPARE:
        path = root/name
        if path.resolve() != path or path.is_symlink() or not path.is_file():
            raise ValueError('installed_source_unavailable')
        with path.open('rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('installed_regular_source_required')
            actual = stream.read(2_000_001)
        if actual != expected[name]:
            raise ValueError('installed_source_differs_from_reviewed_test')
    return len(expected)


def extra_tests(life):
    class OrderJourney(unittest.TestCase):
        def setUp(self):
            self.context = life.Lifecycle()
            self.context.setUp()

        def tearDown(self):
            self.context.tearDown()

        def test_actual_transport_dry_run_never_logs_in(self):
            # The test suite normally mocks place_limit_order. Remove that one
            # patch, then exercise the real transport's synthetic dry-run branch.
            patch.stopall()
            life.audit.log.reset_mock()
            with patch.object(life.orders, 'ORDER_PLACEMENT_ENABLED', False), \
                    patch.object(life.session.SESSION, 'ensure',
                                 side_effect=IsolationError('unexpected_session_use')) as login:
                result = life.orders.place_limit_order(
                    tradingsymbol='OFFLINE_TEST_CE', symboltoken='SYNTHETIC',
                    exchange='NFO', transactiontype='BUY', quantity=10, price=100.)
            self.assertTrue(result.ok and result.dry_run)
            self.assertEqual(result.reason, 'dry_run_not_sent')
            login.assert_not_called()
            payloads = [call.kwargs for call in life.audit.log.call_args_list
                        if call.args and call.args[0] == 'order_dry_run']
            self.assertEqual(len(payloads), 1)
            p = payloads[0]
            self.assertEqual((p['ordertype'], p['transactiontype'], p['quantity']),
                             ('LIMIT', 'BUY', '10'))

        def test_synthetic_buy_partial_full_exit_and_no_residual(self):
            c = self.context
            c.pos.entry_price = 0.
            c.pos.remaining_qty = 0
            c.pos.status = 'entry_pending'
            acknowledgement = life.ledger.submit(c.pos, 'BUY', 10, 105.)
            self.assertTrue(acknowledgement['pending'])
            self.assertEqual(c.pos.remaining_qty, 0)  # acknowledgement is not fill
            c.fill(3, 101.)
            self.assertEqual(c.pos.remaining_qty, 3)
            c.fill(3, 101.)  # repeated broker snapshot must not add another fill
            self.assertEqual(c.pos.remaining_qty, 3)
            c.fill(10, 102., 'complete')
            self.assertEqual((c.pos.status, c.pos.remaining_qty), ('open', 10))
            c.place.return_value = life.orders.OrderResult(True, 'O2', 'ok')
            c.ex._close_position(c.pos, 110., 'offline_test_exit')
            self.assertEqual(c.pos.remaining_qty, 10)  # exit acknowledgement only
            c.fill(4, 108., oid='O2')
            self.assertEqual(c.pos.remaining_qty, 6)
            c.fill(4, 108., oid='O2')
            self.assertEqual(c.pos.remaining_qty, 6)
            c.fill(10, 109., 'complete', 'O2')
            self.assertEqual(c.pos.remaining_qty, 0)
            self.assertEqual(c.pos.status, 'closed')
            self.assertEqual(life.ledger.pending(), [])
            self.assertEqual(life.storage.load_open_positions(), [])
            self.assertEqual(c.pnl(), 70.)  # artificial fixture arithmetic, not profit
            self.assertEqual(c.place.call_count, 2)
    return OrderJourney


def run(release, installed=None):
    release = Path(release).resolve()
    if release == PRODUCTION or release.is_relative_to(PRODUCTION):
        raise ValueError('extract_release_separately_do_not_run_in_live_tree')
    required = ('tests/test_execution_lifecycle.py', 'tests/test_multi_position_capacity.py')
    if not all((release/name).is_file() for name in required):
        raise ValueError('complete_reviewed_release_required')
    # Sensitive process environment is not forwarded to the test code.
    os.environ.clear()
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='kadima-offline-order-') as temporary:
        scratch = Path(temporary)
        tempfile.tempdir = str(scratch)
        os.environ.update(HOME=str(scratch), TMPDIR=str(scratch),
                          KADIMA_DATA_DIR=str(scratch), ORDER_PLACEMENT_ENABLED='0',
                          KADIMA_CELESTIAL_ENABLED='0', KADIMA_SESSION_AUTOTRADE='0')
        guard = Guard(release, scratch, installed)
        sys.addaudithook(guard)
        # No real package __init__ or configuration is imported.
        package = types.ModuleType('kadima')
        package.__path__ = [str(release/'kadima')]
        sys.modules['kadima'] = package
        life = load_module('_offline_lifecycle', release/required[0])
        multi = load_module('_offline_multi', release/required[1])
        matched = compare_installed(installed, release, multi) if installed else None
        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for klass in (life.Lifecycle, life.BrokerParsing, multi.CapacityTests,
                      multi.RealExecutorTests, extra_tests(life)):
            suite.addTests(loader.loadTestsFromTestCase(klass))
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=0).run(suite)
        passed = result.wasSuccessful() and not result.skipped and guard.blocked == 0
        if not passed:
            # Only test names are surfaced, never exception bodies or object reprs.
            failed = [case.id().rsplit('.', 1)[-1] for case, _ in result.failures + result.errors]
            print(json.dumps(dict(result='OFFLINE_TEST_FAILED', tests_run=result.testsRun,
                                  failed_tests=failed, skipped=len(result.skipped),
                                  blocked_operations=guard.blocked)))
            return 1
        report = dict(result='OFFLINE_ORDER_TEST_PASSED', tests_run=result.testsRun,
                      failures=0, errors=0, skipped=0,
                      installed_source_files_matched=matched,
                      real_broker_requests=0, production_database_accessed=False,
                      live_settings_changed=False,
                      scenario='SYNTHETIC BUY / PARTIAL-FULL FILLS / EXIT / FLAT',
                      scope='Reviewed transport, ledger, exit and multi-entry fixtures; simulated market/broker inputs',
                      not_verified='Live authentication, exchange acceptance, real fills, market-data freshness or profitability')
        print(json.dumps(report, indent=2))
        # Close any test-local connections before TemporaryDirectory cleanup.
        conn = getattr(life.storage._local, 'conn', None)
        if conn is not None:
            conn.close()
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-installed', type=Path)
    args = parser.parse_args()
    return run(Path(__file__).resolve().parents[1], args.compare_installed)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception,) as exc:
        # A guard failure must not dump credentials, live data or arbitrary paths.
        print('OFFLINE TEST STOPPED:', type(exc).__name__, '(no live-order fallback)')
        raise SystemExit(1)
