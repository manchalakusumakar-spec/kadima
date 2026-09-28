"""Offline maintenance install checks; real file transactions, no AWS/broker."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name).resolve()
        self.release = self.target / 'pr19-release'
        shutil.copytree(ROOT / 'kadima', self.release / 'kadima')
        shutil.copyfile(ROOT / 'run.py', self.release / 'run.py')
        previous = json.loads((ROOT / 'tests/fixtures/pr19_sources.json').read_text())
        self.assertEqual(previous['commit'], self.momentum_commit())
        for name, source in previous['files'].items():
            (self.release / name).write_text(source)
        for name in ('momentum_recovery/install.py', 'mcx_live_update/install.py',
                     'mcx_live_update/auto_executor.py', 'stock_fast_update/install.py'):
            (self.release / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, self.release / name)
        self.installer = load('stopped_source_plan_review', self.release / 'momentum_recovery/install.py')
        self.base = self.installer.base
        shutil.copytree(self.release / 'kadima', self.target / 'kadima')
        shutil.copyfile(ROOT / 'run.py', self.target / 'run.py')
        self.executor = self.target / 'kadima/auto_executor.py'
        shutil.copyfile(ROOT / 'mcx_live_update/auto_executor.py', self.executor)
        self.config = self.target / 'kadima/config.py'
        self.config.write_bytes(self.config.read_bytes() + b'\nLOCAL_OPERATOR_SETTING = 987\n')
        self.news = self.target / 'kadima/news_collector.py'
        self.news.write_bytes((ROOT / 'tests/fixtures/news_collector_98c0e36.py').read_bytes())
        for name, previous in self.base.FILES.items():
            if previous is None:
                (self.target / name).unlink()

    def plan(self):
        return self.base.backend_plan(self.release, self.target, (os.getuid(), os.getgid()))

    @staticmethod
    def momentum_commit():
        return 'bd294a7048749258fbaa2cc786f20a441a2a8073'

    def test_reviewed_source_fixture_plan_preserves_composed_executor_and_local_config(self):
        originals = {p: p.read_bytes() for p in (self.executor, self.config)}
        changes, fingerprints = self.plan()
        expected = {'kadima/news_collector.py'} | {
            name for name, previous in self.base.FILES.items() if previous is None}
        self.assertEqual({str(c.path.relative_to(self.target)) for c in changes}, expected)
        self.assertEqual(self.base.digest(originals[self.executor]),
                         '7cfe6cf21a520ba967d6a672b2f54a99bbe81321')
        for path, raw in originals.items():
            self.assertNotIn(path, [c.path for c in changes])
            self.assertEqual(fingerprints[path], raw)
            self.assertEqual(path.read_bytes(), raw)

    def test_unknown_predecessor_or_executor_overlay_never_creates_files(self):
        for path in (self.news, self.executor):
            original = path.read_bytes()
            path.write_bytes(original + b'\n# unreviewed operator source\n')
            before = {p: p.read_bytes() for p in self.target.rglob('*.py')}
            with self.subTest(path=path.name), self.assertRaises(self.base.Blocked):
                self.plan()
            self.assertEqual(before, {p: p.read_bytes() for p in self.target.rglob('*.py')})
            path.write_bytes(original)


class StoppedTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.m = load('stopped_source_transaction_review', 'momentum_recovery/install_stopped_source.py')
        self.base = load('stopped_source_base_review', 'momentum_recovery/install.py').base
        self.old = self.root / 'runtime.py'
        self.old.write_bytes(b'VALUE = "previous"\n')
        self.old.chmod(0o640)
        self.new = self.root / 'new_worker.py'
        self.protected = self.root / 'config.py'
        self.protected.write_bytes(b'LOCAL_CONFIG = 123\n')
        owner = (os.getuid(), os.getgid())
        self.changes = [self.base.change_for(self.old, b'VALUE = "updated"\n', owner),
                        self.base.change_for(self.new, b'WORKER = "new"\n', owner)]
        self.fingerprints = {c.path: c.original for c in self.changes}
        self.fingerprints[self.protected] = self.protected.read_bytes()
        self.units = ('kadima.service', 'kadima-universe-scanner.service')
        self.observations = {u: dict(LoadState='loaded', ActiveState='active', MainPID='99')
                             for u in self.units}
        self.backup = self.root / 'backup'
        self.backup.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.calls = []
        self.records = []
        self.admissions = []
        def claim(state, now):
            with (state / 'attempted.json').open('x') as stream:
                json.dump({'started_at': now.isoformat()}, stream)
        def record(state, report):
            self.records.append(report.copy())
            (state / 'status.json').write_text(json.dumps(report))
        self.helper = SimpleNamespace(root_parents=lambda path: None,
            private_path=lambda path, **kwargs: None, claim_attempt=claim,
            utc_now=lambda: datetime.now(timezone.utc), record_status=record)

    def observe(self, unit):
        return self.observations[unit].copy()

    def invoke(self, command):
        self.calls.append(command)
        self.assertEqual(Path(command[0]).name, 'systemctl')
        self.assertEqual(command[1], 'stop', 'No restart, resume, broker, repair or arbitrary command allowed')
        self.assertEqual(tuple(command[2:]), self.units)
        for unit in command[2:]:
            self.observations[unit].update(ActiveState='inactive', MainPID='0')
        return SimpleNamespace(returncode=0)

    def admission(self):
        self.admissions.append(True)

    def apply(self, **overrides):
        kwargs = dict(observe=self.observe, invoke=self.invoke,
                      record=lambda report: self.records.append(report.copy()), guard=self.admission)
        kwargs.update(overrides)
        return self.m.apply_source_changes(self.base, self.changes, self.fingerprints,
                                           self.units, self.backup, **kwargs)

    def execute(self, **overrides):
        kwargs = dict(apply=True, manual_holding_external=True,
                      admission=self.admission, observe=self.observe, invoke=self.invoke)
        kwargs.update(overrides)
        return self.m.execute(self.base, self.changes, self.fingerprints, self.units,
                              self.state, self.helper, **kwargs)

    def assert_original_source(self):
        self.assertEqual(self.old.read_bytes(), self.changes[0].original)
        self.assertFalse(self.new.exists())

    def assert_stopped(self):
        for state in self.observations.values():
            self.assertEqual((state['ActiveState'], state['MainPID']), ('inactive', '0'))

    def test_success_changes_only_planned_files_after_processes_stop(self):
        original_write = self.base.write_atomic
        writes = []
        def checked_write(item, raw):
            self.assert_stopped()
            self.assertTrue(self.calls)
            writes.append(item.path)
            return original_write(item, raw)
        with (patch.object(self.base, 'write_atomic', checked_write),
              patch('socket.create_connection', side_effect=AssertionError('network call prohibited')),
              patch('sqlite3.connect', side_effect=AssertionError('database call prohibited'))):
            self.apply()
        self.assertEqual(writes, [c.path for c in self.changes])
        for item in self.changes:
            self.assertEqual(item.path.read_bytes(), item.replacement)
        self.assertEqual(self.protected.read_bytes(), self.fingerprints[self.protected])
        self.assertEqual(self.old.stat().st_mode & 0o777, 0o640)
        self.assert_stopped()
        self.assertTrue(self.admissions)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_preserved_dependency_blocks_before_stop(self):
        self.protected.write_bytes(b'UNREVIEWED = 1\n')
        with self.assertRaises((self.m.Blocked, self.base.Blocked)):
            self.apply()
        self.assertEqual(self.calls, [])
        self.assert_original_source()

    def test_source_changed_after_planning_blocks_before_stop(self):
        self.old.write_bytes(b'UNREVIEWED = 2\n')
        with self.assertRaises((self.m.Blocked, self.base.Blocked)):
            self.apply()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.old.read_bytes(), b'UNREVIEWED = 2\n')
        self.assertFalse(self.new.exists())

    def test_pause_or_timer_guard_failure_blocks_before_stop_and_writes(self):
        for reason in ('entries_not_paused', 'competing_worker_active_or_unknown', 'entry_timer_still_active'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                self.backup = Path(temporary)
                def refuse():
                    raise self.m.Blocked(reason)
                with self.assertRaisesRegex(self.m.Blocked, reason):
                    self.apply(guard=refuse)
                self.assertEqual(self.calls, [])
                self.assert_original_source()

    def test_stop_command_failure_leaves_original_source_without_restart(self):
        def fail(command):
            self.calls.append(command)
            self.observations[self.units[0]].update(ActiveState='inactive', MainPID='0')
            return SimpleNamespace(returncode=1)
        with patch.object(self.base, 'write_atomic') as writer:
            with self.assertRaisesRegex(self.m.Blocked, 'service_stop_failed'):
                self.apply(invoke=fail)
            writer.assert_not_called()
        self.assert_original_source()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 'stop')
        self.assertEqual(self.observations[self.units[0]]['ActiveState'], 'inactive')

    def test_stop_success_with_process_still_running_never_writes(self):
        def incomplete(command):
            self.calls.append(command)
            return SimpleNamespace(returncode=0)
        with patch.object(self.base, 'write_atomic') as writer:
            with self.assertRaisesRegex(self.m.Blocked, 'maintenance_service_not_stopped'):
                self.apply(invoke=incomplete)
            writer.assert_not_called()
        self.assert_original_source()
        self.assertEqual(len(self.calls), 1)

    def test_partial_write_failure_restores_sources_and_stays_stopped(self):
        original_write = self.base.write_atomic
        def fail_after_write(item, raw):
            self.assert_stopped()
            original_write(item, raw)
            if item.path == self.new and raw == item.replacement:
                raise OSError('simulated disk failure after file replacement')
        with patch.object(self.base, 'write_atomic', fail_after_write):
            with self.assertRaises(OSError):
                self.apply()
        self.assert_original_source()
        self.assert_stopped()
        self.assertEqual(len(self.calls), 1)

    def test_dependency_change_during_install_rolls_back_without_restart(self):
        original_write = self.base.write_atomic
        def external_change(item, raw):
            original_write(item, raw)
            if item.path == self.new and raw == item.replacement:
                self.protected.write_bytes(b'CONCURRENT_CHANGE = 1\n')
        with patch.object(self.base, 'write_atomic', external_change):
            with self.assertRaisesRegex(self.m.Blocked, 'installed_source_or_preserved_dependency_changed'):
                self.apply()
        self.assert_original_source()
        self.assertEqual(self.protected.read_bytes(), b'CONCURRENT_CHANGE = 1\n')
        self.assert_stopped()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.records[-1]['rollback'], 'unverified')

    def test_service_reactivation_mid_rollback_prevents_further_source_writes(self):
        original_write = self.base.write_atomic
        original_unlink = Path.unlink
        active_writes = []
        def write(item, raw):
            if any(s['ActiveState'] != 'inactive' for s in self.observations.values()):
                active_writes.append(item.path)
            original_write(item, raw)
            if item.path == self.new and raw == item.replacement:
                raise OSError('second write failed after replace')
        def unlink(path, *args, **kwargs):
            result = original_unlink(path, *args, **kwargs)
            if path == self.new:
                self.observations[self.units[0]].update(ActiveState='active', MainPID='200')
            return result
        with patch.object(self.base, 'write_atomic', write), patch.object(Path, 'unlink', unlink):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(active_writes, [])
        self.assertFalse(self.new.exists())
        self.assertEqual(self.old.read_bytes(), self.changes[0].replacement)
        self.assertEqual(self.records[-1]['rollback'], 'unverified')
        self.assertEqual(len(self.calls), 1)

    def test_execute_plan_neither_claims_attempt_nor_stops_services(self):
        report = self.execute(apply=False, manual_holding_external=False)
        self.assertEqual(report['state'], 'plan_verified')
        self.assertIs(report['broker_flat_verified'], False)
        self.assertIs(report['activation'], False)
        self.assertFalse((self.state / 'attempted.json').exists())
        self.assertEqual(self.calls, [])
        self.assert_original_source()

    def test_execute_requires_explicit_manual_holding_acknowledgement(self):
        with self.assertRaisesRegex(self.m.Blocked, 'manual_holding_external_acknowledgement_required'):
            self.execute(manual_holding_external=False)
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / 'attempted.json').exists())

    def test_replay_is_refused_before_any_admission_stop_or_write(self):
        (self.state / 'attempted.json').write_text('prior attempt')
        with self.assertRaisesRegex(self.m.Blocked, 'attempt_already_recorded'):
            self.execute()
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.calls, [])
        self.assert_original_source()

    def test_execute_success_records_sources_only_and_never_flat_or_repaired(self):
        with (patch('socket.create_connection', side_effect=AssertionError('network prohibited')),
              patch('sqlite3.connect', side_effect=AssertionError('database prohibited'))):
            report = self.execute()
        self.assertEqual(report['state'], 'source_installed_services_stopped')
        self.assertTrue((self.state / 'attempted.json').exists())
        self.assert_stopped()
        for key in ('broker_flat_verified', 'ledger_repaired', 'dashboard_changed',
                    'activation', 'restart_requested'):
            self.assertIs(report[key], False)
        self.assertIs(report['historical_repair_pending'], True)
        self.assertIs(report['startup_review_pending'], True)
        self.assertEqual(len(self.calls), 1)
        for entry in self.records:
            for key in ('broker_flat_verified', 'ledger_repaired', 'activation', 'restart_requested'):
                self.assertIs(entry[key], False)

    def test_final_verification_failure_keeps_attempt_and_records_block(self):
        original_verify = self.m.verify_installed
        verifications = []
        def verify(*args):
            verifications.append(True)
            if len(verifications) == 2:
                raise self.m.Blocked('final_source_observation_failed')
            return original_verify(*args)
        with patch.object(self.m, 'verify_installed', verify):
            with self.assertRaisesRegex(self.m.Blocked, 'final_source_observation_failed'):
                self.execute()
        self.assertTrue((self.state / 'attempted.json').exists())
        self.assertEqual(self.records[-1]['state'], 'blocked')
        self.assertIs(self.records[-1]['restart_requested'], False)
        self.assertEqual(len(self.calls), 1)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.m = load('stopped_source_admission_review', 'momentum_recovery/install_stopped_source.py')
        self.prep = load('stopped_source_prep_review', 'momentum_recovery/prepare_maintenance.py')
        self.states = {u: dict(LoadState='loaded', ActiveState='inactive', MainPID='0',
                              UnitFileState='disabled')
                       for u in self.prep.TIMERS + self.prep.WORKERS}
        patcher = patch.object(self.prep, 'unit', lambda unit: self.states[unit].copy())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controls = dict(auto_trade_enabled=False, stock_live_enabled=False,
                             kill_latched=False, api_position_count=0, broker_flat_verified=False)

    def test_empty_api_does_not_certify_flatness_or_call_broker(self):
        with patch.object(self.prep, 'snapshot', return_value=self.controls):
            result = self.m.paused_controls(self.prep, 'TEST')
        self.assertIs(result['broker_flat_verified'], False)

    def test_each_unpaused_or_unknown_control_is_refused(self):
        for key in ('auto_trade_enabled', 'stock_live_enabled', 'kill_latched'):
            for value in (True, None, 0):
                with self.subTest(key=key, value=value), patch.object(
                        self.prep, 'snapshot', return_value=dict(self.controls, **{key: value})):
                    with self.assertRaisesRegex(self.m.Blocked, 'paused_clear_controls_required'):
                        self.m.paused_controls(self.prep, 'TEST')

    def test_stale_controls_cannot_admit_source_maintenance(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        data = [dict(ts=stale, config=dict(auto_trade_enabled=False, kill_latched=False), positions=[]),
                dict(ts=stale, controls=dict(live_enabled=False, revision=14))]
        with patch.object(self.prep, 'api', side_effect=data):
            with self.assertRaisesRegex(self.prep.Blocked, 'fresh_control_snapshot_required'):
                self.m.paused_controls(self.prep, 'TEST')

    def test_active_timer_or_worker_blocks_without_control_or_broker_call(self):
        for unit in self.prep.TIMERS + self.prep.WORKERS:
            old = self.states[unit].copy()
            self.states[unit].update(ActiveState='active', MainPID='42')
            with self.subTest(unit=unit), patch.object(self.prep, 'snapshot') as snapshot:
                with self.assertRaises(self.prep.Blocked):
                    self.m.paused_controls(self.prep, 'TEST')
                snapshot.assert_not_called()
            self.states[unit] = old

    def test_optional_units_are_included_only_when_present(self):
        observations = {u: dict(LoadState='loaded', ActiveState='active', MainPID='9')
                        for u in self.m.CORE_UNITS}
        observations.update({u: dict(LoadState='not-found', ActiveState='inactive', MainPID='0')
                             for u in self.m.OPTIONAL_UNITS})
        observe = lambda unit: observations[unit].copy()
        self.assertEqual(self.m.selected_units(observe), self.m.CORE_UNITS)
        observations[self.m.OPTIONAL_UNITS[0]] = dict(LoadState='loaded', ActiveState='active', MainPID='8')
        self.assertEqual(self.m.selected_units(observe), self.m.CORE_UNITS + (self.m.OPTIONAL_UNITS[0],))


if __name__ == '__main__':
    unittest.main()
