"""Offline, read-only stopped-host diagnostics; no live services or broker."""
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MaintenanceRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.m = load('stopped_readiness_status_test', 'momentum_recovery/check_stopped_readiness.py')
        self.source = load('stopped_readiness_source_test', 'momentum_recovery/install_stopped_audit.py')
        self.base = load('stopped_readiness_status_base', 'stock_fast_update/install.py')
        self.status = dict(state='audit_learning_source_installed_services_stopped',
            source_commit=self.source.RELEASE['commit'], source_installed=True,
            dashboard_changed=True, manual_holding_external=True, activation=False, restart_requested=False,
            ledger_repaired=False, runtime_health_verified=False,
            stopped_units=list(self.source.UNITS), observed_at='2026-09-28T00:30:00+05:30')
        self.attempt = dict(started_at='2026-09-28T00:29:00+05:30')
        def private(path, *, directory=False):
            info = path.lstat()
            if (path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    or (not directory and info.st_nlink != 1)):
                raise self.m.Blocked('private_record_required')
        self.helper = SimpleNamespace(root_parents=lambda path: None, private_path=private,
            read_regular=lambda path, limit=4_000_000: self.base.read_file(path), utc_now=lambda: NOW)
        self.write_records()

    def write_records(self):
        for name, value in (('status.json', self.status), ('attempted.json', self.attempt)):
            path = self.root/name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def test_success_record_is_observed_without_new_attempt_or_mutation(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        status, fingerprints = self.m.verify_maintenance(self.helper, self.root)
        self.assertEqual(status, self.status)
        self.assertEqual(set(fingerprints), {self.root/'status.json', self.root/'attempted.json'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        # A repeat diagnostic is allowed; this is not a second deployment attempt.
        self.assertEqual(self.m.verify_maintenance(self.helper, self.root)[0], status)

    def test_incomplete_or_incompatible_status_is_refused(self):
        original = self.status.copy()
        for key, value in [('state', 'installing'), ('source_commit', '0'*40),
                           ('source_installed', False), ('source_installed', 1),
                           ('dashboard_changed', False), ('manual_holding_external', False),
                           ('activation', True), ('restart_requested', True),
                           ('ledger_repaired', True), ('runtime_health_verified', True)]:
            with self.subTest(key=key, value=value):
                self.status = dict(original, **{key: value})
                self.write_records()
                with self.assertRaises(self.m.Blocked):
                    self.m.verify_maintenance(self.helper, self.root)

    def test_missing_duplicate_or_unknown_stopped_units_are_refused(self):
        good = list(self.source.UNITS)
        for units in (good[:-1], [good[0]]*len(good), good[:-1]+['other.service'], None):
            with self.subTest(units=units):
                self.status['stopped_units'] = units
                self.write_records()
                with self.assertRaises(self.m.Blocked):
                    self.m.verify_maintenance(self.helper, self.root)

    def test_attempt_chronology_and_future_records_are_refused(self):
        original_status, original_attempt = self.status.copy(), self.attempt.copy()
        cases = [({}, original_status),
                 ({'started_at': '2026-09-28T00:29:00'}, original_status),
                 ({'started_at': '2026-09-28T00:31:00+05:30'}, original_status),
                 (original_attempt, dict(original_status, observed_at='2026-10-02T00:00:00+00:00'))]
        for attempt, status in cases:
            with self.subTest(attempt=attempt, status=status):
                self.attempt, self.status = attempt, status
                self.write_records()
                with self.assertRaises(self.m.Blocked):
                    self.m.verify_maintenance(self.helper, self.root)

    def test_private_files_required_and_later_changes_detected(self):
        _, fingerprints = self.m.verify_maintenance(self.helper, self.root)
        self.status['state'] = 'blocked'
        self.write_records()
        with self.assertRaises(self.base.Blocked):
            self.base.check_fingerprints(fingerprints)
        (self.root/'attempted.json').chmod(0o644)
        with self.assertRaises(self.m.Blocked):
            self.m.verify_maintenance(self.helper, self.root)


class InstalledSourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.target = self.root/'installed'
        self.target.mkdir()
        self.m = load('stopped_readiness_installed_test', 'momentum_recovery/check_stopped_readiness.py')
        self.source = load('stopped_readiness_installed_source', 'momentum_recovery/install_stopped_audit.py')
        self.installer = load('stopped_readiness_audit_installer', 'trade_audit_update/install.py')
        self.base = self.installer.base
        shutil.copytree(ROOT/'kadima', self.target/'kadima', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(ROOT/'run.py', self.target/'run.py')
        shutil.copyfile(ROOT/'mcx_live_update/auto_executor.py', self.target/'kadima/auto_executor.py')
        self.config = self.target/'kadima/config.py'
        self.config.write_bytes(self.config.read_bytes()+b'\nLOCAL_OPERATOR_SETTING = 123\n')
        self.ui_root = ROOT/'momentum_recovery/reviewed_dashboard'
        installed_ui = self.root/'ui'
        shutil.copytree(self.ui_root, installed_ui)
        self.base.GATEWAY, self.base.PAGE = installed_ui/'gateway.py', installed_ui/'dashboard.html'

    def inspect(self):
        return self.m.verify_sources(self.installer, ROOT, self.target, self.ui_root,
                                     (os.getuid(), os.getgid()), self.source)

    def test_installed_pr24_with_composed_executor_and_ui_is_accepted_without_writes(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with (patch('subprocess.run', side_effect=AssertionError('service/process action prohibited')),
              patch('sqlite3.connect', side_effect=AssertionError('database access prohibited')),
              patch('socket.create_connection', side_effect=AssertionError('network prohibited'))):
            fingerprints = self.inspect()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        for path in (self.target/'run.py', self.target/'kadima/auto_executor.py', self.config,
                     self.base.GATEWAY, self.base.PAGE):
            self.assertEqual(fingerprints[path], path.read_bytes())

    def test_old_or_unknown_backend_dependency_cannot_pass_as_installed(self):
        for path in (self.target/'run.py', self.target/'kadima/auto_executor.py',
                     self.target/'kadima/trade_learning.py'):
            original = path.read_bytes()
            path.write_bytes(original+b'\n# unreviewed source\n')
            with self.subTest(path=path.name), self.assertRaises((self.m.Blocked, self.base.Blocked)):
                self.inspect()
            self.assertEqual(path.read_bytes(), original+b'\n# unreviewed source\n')
            path.write_bytes(original)

    def test_missing_learning_file_requires_install_not_readiness_approval(self):
        path = self.target/'kadima/trade_learning.py'
        path.unlink()
        with self.assertRaises(self.m.Blocked):
            self.inspect()
        self.assertFalse(path.exists())

    def test_unknown_installed_ui_is_preserved_and_refused(self):
        for path in (self.base.GATEWAY, self.base.PAGE):
            original = path.read_bytes()
            path.write_bytes(original+b'\nUNREVIEWED\n')
            with self.subTest(path=path.name), self.assertRaises(self.m.Blocked):
                self.inspect()
            self.assertEqual(path.read_bytes(), original+b'\nUNREVIEWED\n')
            path.write_bytes(original)


class LocalReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root/'kadima.db'
        self.m = load('stopped_readiness_local_test', 'momentum_recovery/check_stopped_readiness.py')
        self.note = load('stopped_readiness_note', 'momentum_recovery/reconcile_contract_note.py')
        self.evidence = dict(document_sha256='a'*64, signal_id=1, quantity=60,
            symbol='TESTPE', token='123', exchange='BFO', trade_date='2026-09-24', expiry='2026-09-24',
            entry_at='2026-09-24T09:32:00+05:30', intent_at='2026-09-24T09:33:00+05:30',
            exit_at='2026-09-24T09:39:00+05:30', entry_price=100, exit_price=120,
            expected_entry_price=105, account_sha256=hashlib.sha256(b'TESTACCOUNT').hexdigest(),
            exchange_order_id='987', trade_id='654', intent='Ktest', day_buy_qty=90, day_sell_qty=90)
        self.note.evidence(self.evidence, NOW)
        with sqlite3.connect(self.db) as c:
            c.executescript('''
                CREATE TABLE positions(signal_id INTEGER, symbol TEXT, token TEXT, exchange TEXT,
                    kind TEXT, status TEXT, remaining_qty INTEGER, lots INTEGER, lot_size INTEGER,
                    entry_price REAL, entry_ts TEXT, exit_price REAL, exit_ts TEXT, exit_order_id TEXT, exit_reason TEXT);
                CREATE TABLE execution_orders(intent TEXT, signal_id INTEGER, side TEXT, quantity INTEGER,
                    order_id TEXT, state TEXT, filled INTEGER, notional REAL, created_at TEXT, dry_run INTEGER);
                CREATE TABLE execution_fills(intent TEXT);
                CREATE TABLE celestial_external_fills(signal_id INTEGER, order_id TEXT, cumulative_qty INTEGER,
                    quantity INTEGER, price REAL, pnl REAL, ts TEXT);
                CREATE TABLE celestial_config(id INTEGER,payload TEXT);
                CREATE TABLE stock_fast_controls(id INTEGER,payload TEXT);
            ''')
            c.execute('INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (1,'TESTPE','123','BFO','MANUAL_ADOPTED','exit_pending',60,2,30,105,
                 self.evidence['entry_at'],None,None,None,'trail'))
            c.execute('INSERT INTO execution_orders VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('Ktest',1,'SELL',60,None,'unknown',0,0,self.evidence['intent_at'],0))
            c.execute('INSERT INTO celestial_config VALUES (1,?)',
                (json.dumps(dict(auto_trade_enabled=False, kill_latched=False)),))
            c.execute('INSERT INTO stock_fast_controls VALUES (1,?)',
                (json.dumps(dict(live_enabled=False, holding_minutes=15, revision=14)),))

    def inspect(self, note=None):
        return self.m.local_readiness(self.db, note or self.note, self.evidence)

    def test_matching_evidence_and_paused_controls_remain_read_only(self):
        before = self.db.read_bytes()
        out = self.inspect()
        self.assertEqual(out['unresolved_positions'], 1)
        self.assertEqual(out['unresolved_orders'], 1)
        self.assertIs(out['reviewed_historical_case_matches'], True)
        self.assertIs(out['persisted_pause_verified'], True)
        self.assertEqual(out['controls'], dict(auto_trade_enabled=False, stock_live_enabled=False,
                                             kill_latched=False, stock_revision=14))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertNotIn('account_sha256', json.dumps(out))
        self.assertNotIn('exchange_order_id', json.dumps(out))

    def test_sqlite_connection_is_read_only_and_query_only(self):
        original_connect = sqlite3.connect
        calls, statements = [], []
        def connect(path, **kwargs):
            calls.append((path, kwargs.copy()))
            c = original_connect(path, **kwargs)
            c.set_trace_callback(statements.append)
            return c
        before = self.db.read_bytes()
        with patch.object(self.m.sqlite3, 'connect', connect):
            self.inspect()
        self.assertEqual(len(calls), 1)
        self.assertIn('mode=ro', calls[0][0])
        self.assertIs(calls[0][1]['uri'], True)
        self.assertTrue(any(s.upper().replace(' ', '') == 'PRAGMAQUERY_ONLY=ON' for s in statements))
        self.assertFalse(any(s.lstrip().upper().startswith(('CREATE ', 'INSERT ', 'UPDATE ', 'DELETE ', 'BEGIN IMMEDIATE'))
                             for s in statements))
        self.assertEqual(self.db.read_bytes(), before)

    def test_even_an_inspection_helper_cannot_write_the_ledger(self):
        def try_write(c, evidence):
            c.execute('DELETE FROM positions')
        before = self.db.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            self.inspect(SimpleNamespace(inspect=try_write))
        self.assertEqual(self.db.read_bytes(), before)

    def test_extra_unresolved_rows_report_counts_and_refuse_evidence_match(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO positions(signal_id,status) VALUES (2,NULL)")
            c.execute("INSERT INTO execution_orders(intent,state) VALUES ('another',NULL)")
        out = self.inspect()
        self.assertEqual((out['unresolved_positions'], out['unresolved_orders']), (2, 2))
        self.assertIs(out['reviewed_historical_case_matches'], False)

    def test_contract_or_existing_execution_change_is_not_repaired(self):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE positions SET symbol='OTHERPE'")
        before = self.db.read_bytes()
        self.assertIs(self.inspect()['reviewed_historical_case_matches'], False)
        self.assertEqual(self.db.read_bytes(), before)

    def test_allowed_historical_row_drift_changes_digest_even_when_case_still_matches(self):
        before = self.inspect()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE positions SET exit_reason='operator-reviewed-new-reason'")
        after = self.inspect()
        self.assertTrue(before['reviewed_historical_case_matches'])
        self.assertTrue(after['reviewed_historical_case_matches'])
        self.assertEqual(before['database_identity'], after['database_identity'])
        self.assertEqual(before['unresolved_positions'], after['unresolved_positions'])
        self.assertEqual(before['unresolved_orders'], after['unresolved_orders'])
        self.assertNotEqual(before['historical_rows_sha256'], after['historical_rows_sha256'])
        self.assertNotIn('operator-reviewed-new-reason', json.dumps(after))

    def test_identical_database_replacement_is_detected_between_and_during_inspection(self):
        before = self.inspect()
        replacement = self.root/'replacement.db'
        replacement.write_bytes(self.db.read_bytes())
        os.replace(replacement, self.db)
        after = self.inspect()
        self.assertEqual(before['historical_rows_sha256'], after['historical_rows_sha256'])
        self.assertNotEqual(before['database_identity'], after['database_identity'])

        def replace_after_read(connection, evidence):
            result = self.note.inspect(connection, evidence)
            replacement.write_bytes(self.db.read_bytes())
            os.replace(replacement, self.db)
            return result
        with self.assertRaisesRegex(self.m.Blocked, 'database_identity_changed_during_inspection'):
            self.inspect(SimpleNamespace(inspect=replace_after_read))

    def test_hardlinked_database_is_refused_without_opening_or_writing_it(self):
        before = self.db.read_bytes()
        os.link(self.db, self.root/'alias.db')
        with patch.object(self.m.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(self.m.Blocked, 'regular_unlinked_database_required'):
                self.inspect()
            connect.assert_not_called()
        self.assertEqual(self.db.read_bytes(), before)

    def test_unpaused_stock_is_reported_without_changing_operator_controls(self):
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE stock_fast_controls SET payload=?',
                (json.dumps(dict(live_enabled=True, holding_minutes=15, revision=15)),))
        before = self.db.read_bytes()
        out = self.inspect()
        self.assertIs(out['persisted_pause_verified'], False)
        self.assertIs(out['controls']['stock_live_enabled'], True)
        self.assertEqual(self.db.read_bytes(), before)

    def test_unknown_or_wrongly_typed_controls_cannot_be_reported_paused(self):
        valid = dict(live_enabled=False, holding_minutes=15, revision=14)
        for patch_value in ({'live_enabled': 'false'}, {'live_enabled': None},
                            {'revision': True}, {'revision': '14'}, {'revision': None}):
            with self.subTest(patch=patch_value):
                with sqlite3.connect(self.db) as c:
                    c.execute('UPDATE stock_fast_controls SET payload=?',
                              (json.dumps(dict(valid, **patch_value)),))
                before = self.db.read_bytes()
                with self.assertRaises(self.m.Blocked):
                    self.inspect()
                self.assertEqual(self.db.read_bytes(), before)

    def test_missing_database_is_never_created_and_symlink_is_refused(self):
        missing = self.root/'missing.db'
        with self.assertRaises((self.m.Blocked, FileNotFoundError)):
            self.m.local_readiness(missing, self.note, self.evidence)
        self.assertFalse(missing.exists())
        link = self.root/'linked.db'
        link.symlink_to(self.db)
        with self.assertRaises(self.m.Blocked):
            self.m.local_readiness(link, self.note, self.evidence)

    def test_unknown_database_schema_is_not_silently_reported_empty(self):
        with sqlite3.connect(self.db) as c:
            c.execute('DROP TABLE positions')
        with self.assertRaises((self.m.Blocked, sqlite3.DatabaseError)):
            self.inspect()


class WatchdogInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cron = self.root/'etc'
        self.proc = self.root/'proc'
        self.cron.mkdir()
        self.proc.mkdir()
        self.m = load('stopped_readiness_watchdog_test', 'momentum_recovery/check_stopped_readiness.py')
        self.helper = SimpleNamespace(read_regular=lambda path, limit: path.read_bytes(),
            observe_unit=lambda unit: dict(LoadState='not-found', ActiveState='inactive', MainPID='0'))
        self.calls = []

    def invoke(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(command[:2], ['/usr/bin/crontab', '-u'])
        self.assertEqual(command[-1], '-l')
        return SimpleNamespace(returncode=1, stdout='', stderr='no crontab for '+command[2])

    def inspect(self, invoke=None):
        return self.m.watchdog_triggers(self.helper, invoke=invoke or self.invoke,
                                       cron_root=self.cron, proc=self.proc)

    def test_only_direct_references_and_pids_reported_without_raw_commands(self):
        (self.cron/'crontab').write_text(
            '# ignored eod_watchdog\n25 15 * * * root SECRET=do-not-emit python -m kadima.eod_watchdog\n')
        process = self.proc/'123'
        process.mkdir()
        (process/'cmdline').write_bytes(b'python\x00-m\x00kadima.eod_watchdog\x00PRIVATE_TOKEN\x00')
        out = self.inspect()
        self.assertEqual(len(out['references']), 1)
        self.assertEqual(out['references'][0]['line'], 2)
        self.assertEqual(out['running_pids'], [123])
        self.assertIs(out['indirect_triggers_verified'], False)
        self.assertNotIn('do-not-emit', json.dumps(out))
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(out))
        self.assertEqual(len(self.calls), 2)

    def test_unknown_crontab_result_cannot_be_reported_as_absent(self):
        out = self.inspect(lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout='', stderr='permission unavailable'))
        self.assertEqual(set(out['inspection_errors']),
                         {'crontab_unavailable:root', 'crontab_unavailable:ubuntu'})
        self.assertIs(out['indirect_triggers_verified'], False)

    def test_linked_cron_source_is_not_followed(self):
        folder = self.cron/'cron.d'
        folder.mkdir()
        (self.root/'private').write_text('secret outside scope')
        (folder/'job').symlink_to(self.root/'private')
        out = self.inspect()
        self.assertIn('cron_entry_unreviewed', out['inspection_errors'])
        self.assertEqual(out['references'], [])

    def test_cli_does_not_offer_apply_repair_restart_or_activation(self):
        for flag in ('--apply', '--repair', '--restart', '--activate'):
            with self.subTest(flag=flag), patch.object(self.m, 'load_reviewed') as load_helper:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.m.main(['--history', '/unused', '--evidence', '/unused', flag])
                load_helper.assert_not_called()


class WatchdogHoldIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.m = load('stopped_readiness_hold_integration', 'momentum_recovery/check_stopped_readiness.py')
        self.helper = SimpleNamespace(observe_unit=lambda name: dict(ActiveState='inactive', MainPID='0'))
        self.prep = SimpleNamespace(verify_timer_hold=Mock())
        self.source = SimpleNamespace(LOCKS=(), verify_prior_state=Mock(return_value=({}, {})),
                                      ensure_stopped=Mock())
        class ContextBlocked(RuntimeError):
            def __init__(self):
                super().__init__('fixture_context_blocked')
                self.summary = {'credentials_printed': False}
        class HoldBlocked(RuntimeError):
            def __init__(self):
                super().__init__('fixture_install_uncertain')
                self.hold_receipt = {'held': False, 'mutation_attempted': True, 'state': 'uncertain'}
        self.context = SimpleNamespace(Blocked=ContextBlocked,
            inspect_context=Mock(side_effect=ContextBlocked()))
        self.hold = SimpleNamespace(Blocked=HoldBlocked,
            hold_reviewed_cron=Mock(return_value={'held': True, 'state': 'held'}),
            verify_held_cron=Mock())
        self.modules = dict(zip(self.m.PINS, (self.helper, self.prep, self.source, SimpleNamespace(),
            self.context, SimpleNamespace(Blocked=RuntimeError), self.hold)))

    def run_main(self, hold=False):
        output = io.StringIO()
        argv = ['--history', '/unused', '--evidence', '/unused']
        if hold:
            argv.append('--hold-reviewed-watchdog-cron')
        with (patch.object(self.m.os, 'geteuid', return_value=0),
              patch.object(self.m.os, 'umask'),
              patch.object(self.m.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000)),
              patch.object(self.m, 'load_reviewed', side_effect=lambda name: self.modules[name]),
              patch.object(self.m, 'verify_maintenance', return_value=({}, {})),
              patch.object(self.m, 'watchdog_triggers', return_value={'references': []}),
              contextlib.redirect_stdout(output), self.assertRaises(SystemExit)):
            self.m.main(argv)
        raw = output.getvalue()
        report, _ = json.JSONDecoder().raw_decode(raw)
        return report, raw

    def test_default_read_only_failure_never_holds_cron(self):
        report, raw = self.run_main()
        self.hold.hold_reviewed_cron.assert_not_called()
        self.assertEqual(report['action'], 'read_only_stopped_readiness')
        self.assertNotIn('watchdog_hold', report)
        self.assertIn('READ ONLY:', raw)

    def test_verified_hold_survives_later_context_failure_in_report(self):
        report, raw = self.run_main(hold=True)
        self.source.ensure_stopped.assert_called_once()
        self.hold.hold_reviewed_cron.assert_called_once_with(self.helper)
        self.assertEqual(report['watchdog_hold'], {'held': True, 'state': 'held'})
        self.assertEqual(report['blocked'], 'fixture_context_blocked')
        self.assertFalse(report['ledger_repaired'])
        self.assertFalse(report['restart_requested'])
        self.assertNotIn('READ ONLY:', raw)

    def test_uncertain_hold_is_reported_before_context_or_broker_access(self):
        self.hold.hold_reviewed_cron.side_effect = self.hold.Blocked()
        report, _ = self.run_main(hold=True)
        self.context.inspect_context.assert_not_called()
        self.assertEqual(report['watchdog_hold'],
                         {'held': False, 'mutation_attempted': True, 'state': 'uncertain'})
        self.assertEqual(report['blocked'], 'fixture_install_uncertain')


if __name__ == '__main__':
    unittest.main()
