"""Stopped audit/learning source maintenance, with real offline file writes."""
from datetime import datetime, timezone
import builtins
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_UNITS = ('kadima-universe-scanner.service', 'kadima.service',
                     'kadima-macro-watch.service', 'kadima-eod-watchdog.service')
DASHBOARD = 'kadima-dashboard.service'


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PriorMaintenanceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.m = load('audit_stop_prior_review', 'momentum_recovery/install_stopped_audit.py')
        self.base = load('audit_stop_prior_base', 'stock_fast_update/install.py')
        self.status = dict(state='source_installed_services_stopped',
            source_commit='bd294a7048749258fbaa2cc786f20a441a2a8073',
            source_installed=True, manual_holding_external=True, activation=False,
            restart_requested=False, ledger_repaired=False, stopped_units=list(APPLICATION_UNITS),
            observed_at='2026-09-28T00:10:00+05:30')
        self.attempt = dict(started_at='2026-09-28T00:09:00+05:30')
        def private(path, *, directory=False):
            info = path.lstat()
            if (path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    or (not directory and info.st_nlink != 1)):
                raise self.m.Blocked('private_prior_record_required')
        self.helper = SimpleNamespace(root_parents=lambda path: None,
            private_path=private, read_regular=lambda path, limit: self.base.read_file(path),
            utc_now=lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.write_records()

    def write_records(self):
        for name, value in (('status.json', self.status), ('attempted.json', self.attempt)):
            path = self.root / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def test_valid_old_record_is_provenance_not_current_readiness(self):
        status, fingerprints = self.m.verify_prior_state(self.helper, self.root)
        self.assertEqual(status, self.status)
        self.assertEqual(set(fingerprints), {self.root/'status.json', self.root/'attempted.json'})
        self.assertNotIn('broker_flat_verified', status)
        self.base.check_fingerprints(fingerprints)

    def test_each_incomplete_or_incompatible_record_is_refused(self):
        variants = [('state', 'installing'), ('source_commit', '0'*40),
                    ('source_installed', False), ('source_installed', 1),
                    ('manual_holding_external', False), ('activation', True),
                    ('restart_requested', True), ('ledger_repaired', True)]
        original = self.status.copy()
        for key, value in variants:
            with self.subTest(key=key, value=value):
                self.status = dict(original, **{key: value})
                self.write_records()
                with self.assertRaises(self.m.Blocked):
                    self.m.verify_prior_state(self.helper, self.root)

    def test_missing_duplicate_or_unreviewed_stopped_units_are_refused(self):
        for units in (list(APPLICATION_UNITS[:-1]), [APPLICATION_UNITS[0]]*4,
                      list(APPLICATION_UNITS[:-1])+['unknown.service'], tuple(APPLICATION_UNITS)):
            # JSON represents tuples as lists; test non-list using a dict instead.
            if isinstance(units, tuple):
                units = {name: True for name in units}
            with self.subTest(units=units):
                self.status['stopped_units'] = units
                self.write_records()
                with self.assertRaises(self.m.Blocked):
                    self.m.verify_prior_state(self.helper, self.root)

    def test_missing_naive_or_reversed_attempt_times_are_refused(self):
        for attempt in ({}, {'started_at': '2026-09-28T00:09:00'},
                        {'started_at': '2026-09-28T00:11:00+05:30'}):
            with self.subTest(attempt=attempt):
                self.attempt = attempt
                self.write_records()
                with self.assertRaises(self.m.Blocked):
                    self.m.verify_prior_state(self.helper, self.root)

    def test_status_mutation_after_admission_is_detected_without_source_writes(self):
        _, fingerprints = self.m.verify_prior_state(self.helper, self.root)
        self.status['state'] = 'blocked'
        self.write_records()
        with self.assertRaises(self.base.Blocked):
            self.base.check_fingerprints(fingerprints)

    def test_future_status_cannot_admit_source_maintenance(self):
        self.status['observed_at'] = '2026-10-02T00:00:00+00:00'
        self.write_records()
        with self.assertRaises(self.m.Blocked):
            self.m.verify_prior_state(self.helper, self.root)

    def test_missing_or_public_attempt_file_is_refused(self):
        path = self.root / 'attempted.json'
        path.chmod(0o644)
        with self.assertRaises(self.m.Blocked):
            self.m.verify_prior_state(self.helper, self.root)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.m.verify_prior_state(self.helper, self.root)

    def test_all_five_units_need_current_unambiguous_stopped_state(self):
        units = APPLICATION_UNITS + (DASHBOARD,)
        expected = dict(LoadState='loaded', ActiveState='inactive', MainPID='0')
        for name in units:
            for replacement in (dict(expected, ActiveState='active'),
                                dict(expected, ActiveState='deactivating'),
                                dict(expected, LoadState='not-found'),
                                dict(expected, MainPID='99'),
                                dict(expected, MainPID=None)):
                with self.subTest(unit=name, state=replacement):
                    states = {unit: expected for unit in units}
                    states[name] = replacement
                    with self.assertRaises(self.m.Blocked):
                        self.m.ensure_stopped(units, lambda unit: states[unit])


class StoppedAuditSourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / 'target'
        self.target.mkdir()
        self.m = load('audit_stop_plan_review', 'momentum_recovery/install_stopped_audit.py')
        self.installer = load('audit_stop_final_installer_review', 'trade_audit_update/install.py')
        self.base = self.installer.base
        shutil.copytree(ROOT/'kadima', self.target/'kadima', ignore=shutil.ignore_patterns('__pycache__'))
        fixture = json.loads((ROOT/'tests/fixtures/pr19_sources.json').read_text())
        self.assertEqual(fixture['commit'], 'bd294a7048749258fbaa2cc786f20a441a2a8073')
        for name, source in fixture['files'].items():
            (self.target/name).write_text(source)
        for name in ('trade_audit.py', 'price_history.py', 'trade_learning.py'):
            (self.target/'kadima'/name).unlink()
        shutil.copyfile(ROOT/'run.py', self.target/'run.py')
        shutil.copyfile(ROOT/'mcx_live_update/auto_executor.py', self.target/'kadima/auto_executor.py')
        self.config = self.target/'kadima/config.py'
        self.config.write_bytes(self.config.read_bytes()+b'\nLOCAL_OPERATOR_SETTING = 123\n')
        self.ui = self.root/'installed-ui'
        self.ui.mkdir()
        for name in ('gateway.py', 'dashboard.html'):
            shutil.copyfile(ROOT/'tests/fixtures/aws_dashboard'/name, self.ui/name)
        self.base.GATEWAY, self.base.PAGE = self.ui/'gateway.py', self.ui/'dashboard.html'
        self.incoming_ui = ROOT/'momentum_recovery/reviewed_dashboard'

    def plan(self):
        return self.m.build_plan(self.installer, ROOT, self.target, self.incoming_ui,
                                 (os.getuid(), os.getgid()))

    def test_exact_pr19_and_custom_ui_plan_admits_final_audit_learning_sources(self):
        before = {path: path.read_bytes() for path in self.target.rglob('*.py')}
        with (patch('sqlite3.connect', side_effect=AssertionError('database prohibited')),
              patch('socket.create_connection', side_effect=AssertionError('network prohibited'))):
            changes, fingerprints = self.plan()
        destinations = {item.path for item in changes}
        for name in ('trade_audit.py', 'price_history.py', 'trade_learning.py',
                     'engine.py', 'control_api.py', 'execution_ledger.py',
                     'forecast_observer.py', 'strike_selector.py'):
            self.assertIn(self.target/'kadima'/name, destinations)
        for path in (self.target/'run.py', self.target/'kadima/auto_executor.py', self.config):
            self.assertNotIn(path, destinations)
            self.assertEqual(fingerprints[path], before[path])
        self.assertEqual(self.base.digest(fingerprints[self.target/'kadima/auto_executor.py']),
                         '7cfe6cf21a520ba967d6a672b2f54a99bbe81321')
        for name in ('gateway.py', 'dashboard.html'):
            item = next(item for item in changes if item.path == self.ui/name)
            self.assertEqual(self.base.digest(item.original), self.m.UI_PREDECESSORS[name])
            self.assertEqual(item.replacement, (self.incoming_ui/name).read_bytes())
        self.assertEqual(before, {path: path.read_bytes() for path in self.target.rglob('*.py')})

    def test_unreviewed_entrypoint_executor_or_ui_cannot_be_overwritten(self):
        paths = (self.target/'run.py', self.target/'kadima/auto_executor.py',
                 self.ui/'gateway.py', self.ui/'dashboard.html')
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original+b'\n# unreviewed source\n')
                with self.assertRaises((self.m.Blocked, self.base.Blocked)):
                    self.plan()
                self.assertEqual(path.read_bytes(), original+b'\n# unreviewed source\n')
                self.assertFalse((self.target/'kadima/trade_learning.py').exists())
                path.write_bytes(original)

    def test_unpinned_composed_ui_is_refused(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            other = Path(directory)
            for name in ('gateway.py', 'dashboard.html'):
                shutil.copyfile(self.incoming_ui/name, other/name)
            (other/'gateway.py').write_bytes((other/'gateway.py').read_bytes()+b'\n# local edit\n')
            with self.assertRaisesRegex(self.m.Blocked, 'reviewed_dashboard_composition_required'):
                self.m.build_plan(self.installer, ROOT, self.target, other,
                                  (os.getuid(), os.getgid()))


class StoppedAuditTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.m = load('audit_stop_transaction_review', 'momentum_recovery/install_stopped_audit.py')
        self.base = load('audit_stop_atomic_base', 'stock_fast_update/install.py')
        self.old = self.root / 'control_api.py'
        self.old.write_bytes(b'API_VERSION = "installed"\n')
        self.old.chmod(0o640)
        self.new = self.root / 'trade_learning.py'
        self.page = self.root / 'dashboard.html'
        self.page.write_bytes(b'<!doctype html><p>Operator panels</p>\n')
        self.gateway = self.root / 'gateway.py'
        self.gateway.write_bytes(b'ROUTES = ["/existing"]\n')
        self.run = self.root / 'run.py'
        self.run.write_bytes(b'RUN = "reviewed PR19"\n')
        self.executor = self.root / 'auto_executor.py'
        self.executor.write_bytes(b'EXECUTOR = "preserved composition"\n')
        self.config = self.root / 'config.py'
        self.config.write_bytes(b'LOCAL_SETTING = 1\n')
        owner = os.getuid(), os.getgid()
        self.changes = [
            self.base.change_for(self.old, b'API_VERSION = "audit_learning"\n', owner),
            self.base.change_for(self.new, b'raise AssertionError("runtime module executed")\n', owner),
            self.base.change_for(self.page, b'<!doctype html><p>Operator panels and audit learning</p>\n', owner),
            self.base.change_for(self.gateway, b'ROUTES = ["/existing", "/learning"]\n', owner),
        ]
        self.fingerprints = {item.path: item.original for item in self.changes}
        self.fingerprints.update({p: p.read_bytes() for p in (self.run, self.executor, self.config)})
        self.units = APPLICATION_UNITS + (DASHBOARD,)
        self.observations = {u: dict(LoadState='loaded', ActiveState='inactive', MainPID='0')
                             for u in APPLICATION_UNITS}
        self.observations[DASHBOARD] = dict(LoadState='loaded', ActiveState='active', MainPID='50')
        self.backup = self.root / 'backup'
        self.backup.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.calls, self.records, self.admissions = [], [], []

        def claim(state, now):
            with (state / 'attempted.json').open('x') as output:
                json.dump({'started_at': now.isoformat()}, output)

        def record(state, value):
            self.records.append(value.copy())
            (state / 'status.json').write_text(json.dumps(value))

        self.helper = SimpleNamespace(root_parents=lambda path: None,
            private_path=lambda path, **kwargs: None, claim_attempt=claim,
            utc_now=lambda: datetime.now(timezone.utc), record_status=record)

    def observe(self, name):
        return self.observations[name].copy()

    def invoke(self, command):
        self.calls.append(command)
        self.assertEqual(Path(command[0]).name, 'systemctl')
        self.assertEqual(command[1], 'stop', 'Only stopping reviewed services is allowed')
        self.assertTrue(set(command[2:]) <= set(self.units))
        self.assertIn(DASHBOARD, command[2:])
        for name in command[2:]:
            self.observations[name].update(ActiveState='inactive', MainPID='0')
        return SimpleNamespace(returncode=0)

    def admission(self):
        self.admissions.append(True)
        for name in APPLICATION_UNITS:
            state = self.observations[name]
            if (state['ActiveState'], state['MainPID']) != ('inactive', '0'):
                raise self.m.Blocked('application_not_stopped')

    def apply(self, **overrides):
        kwargs = dict(observe=self.observe, invoke=self.invoke,
                      record=lambda value: self.records.append(value.copy()), guard=self.admission)
        kwargs.update(overrides)
        return self.m.apply_source_changes(self.base, self.changes, self.fingerprints,
                                          self.units, self.backup, **kwargs)

    def execute(self, **overrides):
        kwargs = dict(apply=True, manual_holding_external=True,
                      admission=self.admission, observe=self.observe, invoke=self.invoke)
        kwargs.update(overrides)
        return self.m.execute(self.base, self.changes, self.fingerprints, self.units,
                              self.state, self.helper, **kwargs)

    def assert_stopped(self):
        for name, state in self.observations.items():
            self.assertEqual((state['ActiveState'], state['MainPID']), ('inactive', '0'), name)

    def assert_originals(self):
        for item in self.changes:
            self.assertEqual(self.base.read_file(item.path, missing=True), item.original)

    def test_success_preserves_run_executor_config_and_stops_all_five_services(self):
        original_write = self.base.write_atomic
        written = []
        original_import = builtins.__import__

        def checked_import(name, *args, **kwargs):
            if name == 'kadima' or name.startswith('kadima.'):
                raise AssertionError('runtime import prohibited: '+name)
            return original_import(name, *args, **kwargs)

        def write(item, raw):
            self.assert_stopped()
            written.append(item.path)
            return original_write(item, raw)

        with (patch.object(self.base, 'write_atomic', write),
              patch('socket.create_connection', side_effect=AssertionError('network prohibited')),
              patch('urllib.request.urlopen', side_effect=AssertionError('network prohibited')),
              patch('sqlite3.connect', side_effect=AssertionError('database prohibited')),
              patch('builtins.__import__', checked_import)):
            self.apply()
        self.assertEqual(written, [item.path for item in self.changes])
        for item in self.changes:
            self.assertEqual(item.path.read_bytes(), item.replacement)
        for path in (self.run, self.executor, self.config):
            self.assertEqual(path.read_bytes(), self.fingerprints[path])
        self.assertEqual(self.old.stat().st_mode & 0o777, 0o640)
        self.assert_stopped()
        self.assertEqual(len(self.calls), 1)

    def test_changed_protected_source_or_original_ui_blocks_before_service_action(self):
        for path in (self.run, self.executor, self.config, self.page, self.gateway):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b'\n# concurrent change\n')
                with self.assertRaises((self.m.Blocked, self.base.Blocked)):
                    self.apply()
                self.assertEqual(self.calls, [])
                self.assertFalse(self.new.exists())
                self.assertEqual(path.read_bytes(), original + b'\n# concurrent change\n')
                path.write_bytes(original)

    def test_current_application_process_blocks_stale_prior_success(self):
        for name in APPLICATION_UNITS:
            with self.subTest(unit=name):
                self.observations[name].update(ActiveState='active', MainPID='300')
                with self.assertRaises(self.m.Blocked):
                    self.apply()
                self.assertEqual(self.calls, [])
                self.assert_originals()
                self.observations[name].update(ActiveState='inactive', MainPID='0')

    def test_guard_failure_does_not_touch_source_or_dashboard(self):
        def refuse():
            raise self.m.Blocked('timer_or_prior_status_changed')
        with self.assertRaisesRegex(self.m.Blocked, 'timer_or_prior_status_changed'):
            self.apply(guard=refuse)
        self.assertEqual(self.calls, [])
        self.assert_originals()

    def test_dashboard_stop_failure_never_writes_or_restarts(self):
        def refuse(command):
            self.calls.append(command)
            return SimpleNamespace(returncode=1)
        with patch.object(self.base, 'write_atomic') as writer:
            with self.assertRaises(self.m.Blocked):
                self.apply(invoke=refuse)
            writer.assert_not_called()
        self.assert_originals()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 'stop')

    def test_successful_stop_exit_with_running_pid_never_writes(self):
        def incomplete(command):
            self.calls.append(command)
            self.observations[DASHBOARD].update(ActiveState='inactive', MainPID='50')
            return SimpleNamespace(returncode=0)
        with patch.object(self.base, 'write_atomic') as writer:
            with self.assertRaises(self.m.Blocked):
                self.apply(invoke=incomplete)
            writer.assert_not_called()
        self.assert_originals()

    def test_partial_ui_write_failure_rolls_back_backend_and_ui_without_restart(self):
        original_write = self.base.write_atomic
        def fail(item, raw):
            self.assert_stopped()
            original_write(item, raw)
            if item.path == self.page and raw == item.replacement:
                raise OSError('write failed after UI replacement')
        with patch.object(self.base, 'write_atomic', fail):
            with self.assertRaises(OSError):
                self.apply()
        self.assert_originals()
        self.assert_stopped()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.records[-1]['rollback'], 'complete')

    def test_service_reactivation_during_rollback_stops_further_writes(self):
        original_write = self.base.write_atomic
        original_unlink = Path.unlink
        active_writes = []
        def fail(item, raw):
            if any(state['ActiveState'] != 'inactive' for state in self.observations.values()):
                active_writes.append(item.path)
            original_write(item, raw)
            if item.path == self.new and raw == item.replacement:
                raise OSError('failure after new file')
        def unlink(path, *args, **kwargs):
            result = original_unlink(path, *args, **kwargs)
            if path == self.new:
                self.observations[DASHBOARD].update(ActiveState='active', MainPID='301')
            return result
        with patch.object(self.base, 'write_atomic', fail), patch.object(Path, 'unlink', unlink):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(active_writes, [])
        self.assertEqual(self.records[-1]['rollback'], 'unverified')
        self.assertEqual(len(self.calls), 1)

    def test_plan_is_read_only_and_does_not_claim_an_attempt(self):
        report = self.execute(apply=False, manual_holding_external=False)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / 'attempted.json').exists())
        self.assertIs(report['source_installed'], False)
        self.assert_originals()

    def test_apply_requires_manual_holding_acknowledgement(self):
        with self.assertRaises(self.m.Blocked):
            self.execute(manual_holding_external=False)
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / 'attempted.json').exists())

    def test_replay_refused_before_admission_or_writes(self):
        (self.state / 'attempted.json').write_text('{}')
        with self.assertRaises(self.m.Blocked):
            self.execute()
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.calls, [])
        self.assert_originals()

    def test_success_status_never_claims_repair_flatness_startup_or_activation(self):
        with (patch('socket.create_connection', side_effect=AssertionError('network prohibited')),
              patch('sqlite3.connect', side_effect=AssertionError('database prohibited'))):
            report = self.execute()
        self.assertTrue((self.state / 'attempted.json').exists())
        self.assertIs(report['source_installed'], True)
        self.assertIs(report['historical_repair_pending'], True)
        self.assertIs(report['startup_review_pending'], True)
        for name in ('broker_flat_verified', 'ledger_repaired', 'activation', 'restart_requested'):
            self.assertIs(report[name], False)
        self.assert_stopped()
        self.assertEqual(len(self.calls), 1)

    def test_failed_post_transaction_verification_never_restarts_or_loses_attempt(self):
        original = self.m.verify_installed
        checks = []
        def verify(*args):
            checks.append(True)
            if len(checks) == 2:
                raise self.m.Blocked('final_source_changed')
            return original(*args)
        with patch.object(self.m, 'verify_installed', verify):
            with self.assertRaisesRegex(self.m.Blocked, 'final_source_changed'):
                self.execute()
        self.assertTrue((self.state / 'attempted.json').exists())
        self.assertEqual(self.records[-1]['state'], 'blocked')
        self.assertIs(self.records[-1]['restart_requested'], False)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
