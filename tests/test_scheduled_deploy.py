"""Offline scheduled-maintenance checks; never invoke systemd, GitHub or a broker."""
from datetime import timedelta, timezone
import configparser
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import shlex
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('tested_scheduled_deploy',
    ROOT/'momentum_recovery/scheduled_deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class ScheduleWindowTests(unittest.TestCase):
    def test_utc_and_ist_representations_admit_the_same_one_minute_window(self):
        start = deploy.SCHEDULED_AT
        self.assertIsNotNone(start.tzinfo)
        self.assertEqual(start.utcoffset(), timedelta(0))
        for elapsed in (0, 1, 30, 59.999999):
            at = start + timedelta(seconds=elapsed)
            for zone in (timezone.utc, ZoneInfo('Asia/Kolkata')):
                with self.subTest(elapsed=elapsed, zone=zone):
                    deploy.admit_run(at.astimezone(zone))

    def test_early_late_other_day_and_naive_runs_are_rejected(self):
        start = deploy.SCHEDULED_AT
        values = (start-timedelta(microseconds=1), start+timedelta(seconds=60),
                  start+timedelta(days=1), start-timedelta(days=1),
                  start.replace(tzinfo=None))
        for at in values:
            with self.subTest(at=at), self.assertRaises(deploy.Blocked):
                deploy.admit_run(at)

    def test_setup_cannot_create_a_catchup_job_at_or_after_scheduled_time(self):
        deploy.admit_setup(deploy.SCHEDULED_AT-timedelta(seconds=1))
        for at in (deploy.SCHEDULED_AT, deploy.SCHEDULED_AT+timedelta(days=1),
                   deploy.SCHEDULED_AT.replace(tzinfo=None)):
            with self.subTest(at=at), self.assertRaises(deploy.Blocked):
                deploy.admit_setup(at)


class SystemdUnitTests(unittest.TestCase):
    @staticmethod
    def unit(text):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        return parser

    def test_timer_is_single_calendar_event_without_catchup_or_repeat_triggers(self):
        timer = self.unit(deploy.render_timer())['Timer']
        self.assertEqual(timer.get('Persistent', '').lower(), 'false')
        self.assertEqual(timer.get('RandomizedDelaySec'), '0')
        self.assertIn(deploy.SCHEDULED_AT.strftime('%Y-%m-%d'), timer['OnCalendar'])
        self.assertIn(deploy.SCHEDULED_AT.strftime('%H:%M:%S'), timer['OnCalendar'])
        self.assertIn('UTC', timer['OnCalendar'])
        for key in ('OnBootSec', 'OnStartupSec', 'OnActiveSec',
                    'OnUnitActiveSec', 'OnUnitInactiveSec'):
            self.assertNotIn(key, timer)

    def test_oneshot_never_retries_or_outer_times_out_a_guarded_transaction(self):
        service = self.unit(deploy.render_service())['Service']
        self.assertEqual(service['Type'], 'oneshot')
        self.assertEqual(service['User'], 'root')
        self.assertEqual(service['Restart'], 'no')
        self.assertIn(service.get('KillMode', 'control-group'), ('mixed', 'control-group'))
        for key in ('TimeoutStartSec', 'TimeoutStopSec'):
            value = service[key].strip().lower()
            self.assertEqual(value, 'infinity')
        self.assertNotIn('ExecStartPost', service)
        self.assertNotIn('ExecStopPost', service)
        self.assertEqual(shlex.split(service['ExecStart'])[-1], 'run')


class SessionAutomationTests(unittest.TestCase):
    @staticmethod
    def observed(timer_state='inactive', timer_enabled='disabled', service_state='inactive'):
        return {
            'kadima-session-auto.timer': dict(LoadState='loaded', ActiveState=timer_state,
                                             UnitFileState=timer_enabled, MainPID='0'),
            'kadima-session-auto.service': dict(LoadState='loaded', ActiveState=service_state,
                                               UnitFileState='static', MainPID='0'),
        }

    def test_active_or_enabled_session_automation_blocks_deployment(self):
        values = ({}, self.observed(timer_state='active'),
                  self.observed(timer_enabled='enabled'),
                  self.observed(service_state='active'),
                  self.observed(service_state='activating'))
        for value in values:
            with self.subTest(value=value), self.assertRaises(deploy.Blocked):
                deploy.session_auto_admission(value)

    def test_changed_session_gate_blocks_before_invoking_a_stage(self):
        invoke, record = Mock(), Mock()
        guard = Mock(side_effect=deploy.Blocked('session_auto_changed'))
        with self.assertRaises(deploy.Blocked):
            deploy.run_stages([('repair', ['repair-command'])], invoke, record, pre_stage=guard)
        invoke.assert_not_called()

    def test_only_stopped_disabled_or_absent_session_units_are_accepted(self):
        deploy.session_auto_admission(self.observed())
        deploy.session_auto_admission({name: dict(LoadState='not-found',
            ActiveState='inactive', MainPID='0') for name in (deploy.TIMER, deploy.SERVICE)})
        value = self.observed()
        value[deploy.TIMER].pop('MainPID')  # Timers have no process identity.
        deploy.session_auto_admission(value)
        for pid in ('42', None):
            value = self.observed()
            value[deploy.SERVICE]['MainPID'] = pid
            with self.subTest(pid=pid), self.assertRaises(deploy.Blocked):
                deploy.session_auto_admission(value)
        value = self.observed(timer_enabled='static')
        with self.assertRaises(deploy.Blocked):
            deploy.session_auto_admission(value)


@contextmanager
def fixture_ownership():
    """Temporary fixtures model root-owned deployment files on non-root CI too.

    Preserve actual file types, symlinks, modes and bytes. Only the fixture owner
    is normalized; ancestor admission is exercised separately below.
    """
    original = Path.lstat
    def root_owner(path):
        values = list(original(path))
        values[4] = 0
        return os.stat_result(values)
    with patch.object(Path, 'lstat', root_owner), patch.object(deploy, 'root_parents'):
        yield


class StageTests(unittest.TestCase):
    def test_exact_stage_order_and_paths_keep_repair_and_installers_separate(self):
        bundle = Path('/opt/test-pinned-bundle')
        stages = deploy.stage_commands(bundle)
        self.assertEqual([name for name, _ in stages], ['repair', 'momentum', 'audit'])
        commands = dict(stages)
        scripts = {
            'repair': 'releases/repair/momentum_recovery/reconcile_contract_note.py',
            'momentum': 'releases/momentum/momentum_recovery/install.py',
            'audit': 'releases/audit/trade_audit_update/install.py',
        }
        for name, argv in stages:
            self.assertEqual(argv[:3], ['/usr/bin/python3', '-I', '-B'])
            self.assertEqual(argv[3], str(bundle/scripts[name]))
            self.assertEqual(argv.count('--apply'), 1)
            self.assertNotIn('--off-session-unrestored', argv)
            self.assertNotIn('--broker-check-only', argv)
            self.assertNotIn('--resume', argv)
            self.assertNotIn('go_live.py', ' '.join(argv))
            self.assertNotIn('activate_execution.py', ' '.join(argv))
        self.assertEqual(commands['repair'][4:], ['--evidence', str(bundle/'evidence.json'), '--apply'])
        self.assertEqual(commands['momentum'][4:], ['--broker-check', '--apply'])
        self.assertEqual(commands['audit'][4:], ['--with-dashboard', '--broker-check', '--apply'])

    def test_carryforward_or_any_repair_refusal_prevents_both_installers(self):
        stages = deploy.stage_commands(Path('/opt/test-pinned-bundle'))
        invoke = Mock(return_value=types.SimpleNamespace(returncode=1))
        records = []
        with self.assertRaises(deploy.Blocked):
            deploy.run_stages(stages, invoke, records.append)
        invoke.assert_called_once_with(stages[0][1])
        self.assertEqual(records[-1], dict(state='blocked', stage='repair', child_exit=1))

    def test_every_failure_boundary_stops_at_that_stage_and_does_not_retry(self):
        stages = deploy.stage_commands(Path('/opt/test-pinned-bundle'))
        for failed in range(len(stages)):
            with self.subTest(failed=failed):
                invoke = Mock(side_effect=[types.SimpleNamespace(returncode=0)
                    for _ in range(failed)] + [types.SimpleNamespace(returncode=7)])
                records, admissions = [], Mock()
                with self.assertRaises(deploy.Blocked):
                    deploy.run_stages(stages, invoke, records.append, pre_stage=admissions)
                self.assertEqual([call.args[0] for call in invoke.call_args_list],
                                 [argv for _, argv in stages[:failed+1]])
                self.assertEqual(admissions.call_count, failed+1)
                self.assertEqual(records[-1], dict(state='blocked', stage=stages[failed][0], child_exit=7))

    def test_success_records_each_stage_in_order_and_rechecks_admission(self):
        stages = deploy.stage_commands(Path('/opt/test-pinned-bundle'))
        records = []
        invoke = Mock(return_value=types.SimpleNamespace(returncode=0))
        admissions = Mock()
        self.assertEqual(deploy.run_stages(stages, invoke, records.append,
                                          pre_stage=admissions), 0)
        self.assertEqual([call.args[0] for call in invoke.call_args_list], [argv for _, argv in stages])
        self.assertEqual(admissions.call_count, 3)
        self.assertEqual([(item['stage'], item['child_exit']) for item in records
                          if item['state']=='stage_complete'],
                         [('repair', 0), ('momentum', 0), ('audit', 0)])

    def test_lost_child_result_never_invokes_the_next_stage(self):
        stages = deploy.stage_commands(Path('/opt/test-pinned-bundle'))
        invoke = Mock(side_effect=OSError('synthetic child launch failure'))
        with self.assertRaises(OSError):
            deploy.run_stages(stages, invoke, Mock())
        invoke.assert_called_once_with(stages[0][1])


class AttemptTests(unittest.TestCase):
    def test_durable_claim_cannot_be_reused_even_after_a_later_failure(self):
        with tempfile.TemporaryDirectory() as temporary, fixture_ownership():
            state = Path(temporary)
            deploy.claim_attempt(state, deploy.SCHEDULED_AT)
            path = state/'attempted.json'
            original = path.read_bytes()
            self.assertEqual(json.loads(original)['started_at'], deploy.SCHEDULED_AT.isoformat())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for at in (deploy.SCHEDULED_AT, deploy.SCHEDULED_AT+timedelta(seconds=1)):
                with self.assertRaises(deploy.Blocked):
                    deploy.claim_attempt(state, at)
                self.assertEqual(path.read_bytes(), original)

    def test_partial_run_keeps_marker_and_blocked_status_and_prevents_second_attempt(self):
        for failed in (0, 1):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temporary, fixture_ownership():
                state = Path(temporary)
                outcomes = [types.SimpleNamespace(returncode=0) for _ in range(failed)]
                outcomes.append(types.SimpleNamespace(returncode=9))
                with patch.object(deploy, 'STATE', state), \
                     patch.object(deploy, 'BUNDLE', state/'bundle'), \
                     patch.object(deploy.os, 'geteuid', return_value=0), \
                     patch.object(deploy, 'utc_now', return_value=deploy.SCHEDULED_AT), \
                     patch.object(deploy, 'verify_bundle') as verify, \
                     patch.object(deploy, 'check_own_units'), \
                     patch.object(deploy, 'check_session_auto'), \
                     patch.object(deploy, 'check_controls'), \
                     patch.object(deploy, 'final_database_check') as final_db, \
                     patch.object(deploy.subprocess, 'run', side_effect=outcomes) as invoke:
                    with self.assertRaises(deploy.Blocked):
                        deploy.run()
                    self.assertEqual(invoke.call_count, failed+1)
                    self.assertEqual(verify.call_count, failed+1)
                    final_db.assert_not_called()
                    status = json.loads((state/'status.json').read_text())
                    self.assertEqual((status['state'], status['stage'], status['child_exit']),
                                     ('blocked', ('repair', 'momentum')[failed], 9))
                    marker = (state/'attempted.json').read_bytes()
                    with self.assertRaises(deploy.Blocked):
                        deploy.run()
                    self.assertEqual(invoke.call_count, failed+1)
                    self.assertEqual((state/'attempted.json').read_bytes(), marker)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bundle = Path(self.temporary.name)
        self.ownership = fixture_ownership()
        self.ownership.__enter__()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.ownership.__exit__, None, None, None)
        self.manifest = dict(version=1, scheduled_at=deploy.SCHEDULED_AT.isoformat(), releases={})
        self.pins = copy.deepcopy(deploy.RELEASES)
        for name, pin in self.pins.items():
            path = self.bundle/'releases'/name/'entry.py'
            path.parent.mkdir(mode=0o700, parents=True)
            (self.bundle/'releases').chmod(0o700)
            raw = ('# synthetic '+name+' release\n').encode()
            self.write(path, raw)
            entry_blob = deploy.blob_hash(raw)
            # One-file fixture's canonical Git tree, independent of manifest_tree.
            tree_bytes = b'100644 entry.py\0'+bytes.fromhex(entry_blob)
            pin['tree'] = hashlib.sha1(b'tree '+str(len(tree_bytes)).encode()+b'\0'+tree_bytes).hexdigest()
            self.manifest['releases'][name] = dict(pin, files={
                'entry.py': dict(mode='100644', blob=entry_blob)})
        pinned = patch.object(deploy, 'RELEASES', self.pins)
        pinned.start()
        self.addCleanup(pinned.stop)
        for name, key, raw in (
                ('evidence.json', 'evidence_sha256', b'{"reviewed":"synthetic"}'),
                ('scheduled_deploy.py', 'runner_sha256', b'# synthetic runner\n')):
            self.write(self.bundle/name, raw)
            self.manifest[key] = hashlib.sha256(raw).hexdigest()
        self.save_manifest()

    @staticmethod
    def write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def save_manifest(self):
        self.write(self.bundle/'manifest.json', json.dumps(self.manifest).encode())

    def test_complete_private_fixture_verifies_without_execution(self):
        with patch.object(deploy.subprocess, 'run') as invoke:
            self.assertEqual(deploy.verify_bundle(self.bundle), self.manifest)
        invoke.assert_not_called()

    def test_changed_evidence_runner_or_release_bytes_are_rejected(self):
        for name in ('evidence.json', 'scheduled_deploy.py', 'releases/repair/entry.py'):
            path = self.bundle/name
            original = path.read_bytes()
            with self.subTest(name=name):
                self.write(path, original+b'changed')
                with self.assertRaises(deploy.Blocked):
                    deploy.verify_bundle(self.bundle)
                self.write(path, original)

    def test_modified_commit_tree_or_scheduled_time_is_refused(self):
        original = copy.deepcopy(self.manifest)
        for key in ('commit', 'tree'):
            self.manifest = copy.deepcopy(original)
            self.manifest['releases']['repair'][key] = 'f'*40
            self.save_manifest()
            with self.subTest(key=key), self.assertRaises(deploy.Blocked):
                deploy.verify_bundle(self.bundle)
        self.manifest = copy.deepcopy(original)
        self.manifest['scheduled_at'] = (deploy.SCHEDULED_AT+timedelta(days=1)).isoformat()
        self.save_manifest()
        with self.assertRaises(deploy.Blocked):
            deploy.verify_bundle(self.bundle)

    def test_changing_file_and_manifest_hash_cannot_change_the_pinned_tree(self):
        raw = b'# unauthorized replacement\n'
        self.write(self.bundle/'releases/repair/entry.py', raw)
        self.manifest['releases']['repair']['files']['entry.py']['blob'] = deploy.blob_hash(raw)
        self.save_manifest()
        with self.assertRaisesRegex(deploy.Blocked, 'manifest_tree'):
            deploy.verify_bundle(self.bundle)

    def test_unsafe_manifest_paths_and_git_link_modes_are_refused(self):
        original = copy.deepcopy(self.manifest)
        entry = original['releases']['repair']['files']['entry.py']
        for name in ('../outside.py', '/absolute.py', 'a/../entry.py', '.git/config'):
            self.manifest = copy.deepcopy(original)
            self.manifest['releases']['repair']['files'] = {name: entry}
            self.save_manifest()
            with self.subTest(name=name), self.assertRaises(deploy.Blocked):
                deploy.verify_bundle(self.bundle)
        for mode in ('120000', '160000'):
            self.manifest = copy.deepcopy(original)
            self.manifest['releases']['repair']['files']['entry.py']['mode'] = mode
            self.save_manifest()
            with self.subTest(mode=mode), self.assertRaises(deploy.Blocked):
                deploy.verify_bundle(self.bundle)

    def test_extra_files_loose_permissions_and_symlinks_are_refused(self):
        extra = self.bundle/'unexpected.py'
        self.write(extra, b'# unexpected')
        with self.assertRaises(deploy.Blocked):
            deploy.verify_bundle(self.bundle)
        extra.unlink()
        path = self.bundle/'evidence.json'
        path.chmod(0o644)
        with self.assertRaises(deploy.Blocked):
            deploy.verify_bundle(self.bundle)
        path.chmod(0o600)
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.bundle/'scheduled_deploy.py')
        with self.assertRaises(deploy.Blocked):
            deploy.verify_bundle(self.bundle)
        path.unlink()
        self.write(path, original)
        self.assertEqual(deploy.verify_bundle(self.bundle), self.manifest)


class PathSecurityTests(unittest.TestCase):
    def test_nonroot_owned_file_is_rejected_even_when_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'evidence.json'
            path.write_text('{}')
            path.chmod(0o600)
            values = list(path.lstat())
            values[4] = 1234
            with patch.object(Path, 'lstat', return_value=os.stat_result(values)), \
                 self.assertRaises(deploy.Blocked):
                deploy.private_path(path)

    def test_world_writable_or_nonroot_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'bundle'
            original = Path.stat
            for uid, bits in ((0, 0o022), (1234, 0)):
                def stat_parent(parent, **kwargs):
                    values = list(original(parent, **kwargs))
                    values[0] &= ~0o022
                    values[4] = 0
                    if parent == path.parent:
                        values[0] |= bits
                        values[4] = uid
                    return os.stat_result(values)
                with self.subTest(uid=uid, bits=bits), \
                     patch.object(Path, 'stat', stat_parent), \
                     self.assertRaises(deploy.Blocked):
                    deploy.root_parents(path)


if __name__ == '__main__':
    unittest.main()
