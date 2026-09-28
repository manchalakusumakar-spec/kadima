"""Focused peripheral startup refusals and bounded service rollback tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_test_paused_peripheral_start',
    ROOT/'momentum_recovery/start_paused_peripherals.py')
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)
NOW = datetime(2026, 9, 28, 3, 30, tzinfo=timezone.utc)
PYTHON = '/home/ubuntu/kadima-py/venv/bin/python3'


def launch(args, *, executable=PYTHON, ignore_errors='no'):
    return ('{ path='+executable+' ; argv[]='+args+' ; ignore_errors='+ignore_errors+
            ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }')


class EntrypointAdmissionTests(unittest.TestCase):
    def test_only_reviewed_python_modules_and_arguments_are_accepted(self):
        cases = ((m.SCANNER, '-m kadima.stock_universe_scanner', m.TARGET),
                 (m.MACRO, '-m kadima.macro_watch', m.TARGET),
                 (m.DASHBOARD, '-m uvicorn gateway:app --host 127.0.0.1 --port 8082 --no-access-log',
                  Path('/opt/kadima-dashboard')))
        for unit, operands, expected_cwd in cases:
            for flags in ('', '-u ', '-B ', '-u -B '):
                args = PYTHON+' '+flags+operands
                with self.subTest(unit=unit, flags=flags):
                    python, argv, cwd = m.entrypoint(unit, launch(args))
                    self.assertEqual(Path(python), Path(PYTHON))
                    self.assertEqual(tuple(argv), tuple(args.split()))
                    self.assertEqual(Path(cwd), expected_cwd)

    def test_scanner_without_a_reviewed_main_and_arbitrary_module_are_rejected(self):
        for module in ('kadima.full_universe_scanner', 'kadima.engine', 'kadima.eod_watchdog', 'unknown'):
            with self.subTest(module=module), self.assertRaises(m.Blocked):
                m.entrypoint(m.SCANNER, launch(PYTHON+' -m '+module))

    def test_module_switch_and_python_command_execution_are_rejected(self):
        for unit, args in ((m.MACRO, '-m kadima.stock_universe_scanner'),
                           (m.SCANNER, '-m kadima.macro_watch'),
                           (m.SCANNER, '-c arbitrary'),
                           (m.SCANNER, '-m kadima.stock_universe_scanner --live'),
                           (m.SCANNER, '-I -m kadima.stock_universe_scanner')):
            with self.subTest(unit=unit, args=args), self.assertRaises(m.Blocked):
                m.entrypoint(unit, launch(PYTHON+' '+args))

    def test_dashboard_bind_app_port_and_extra_argument_changes_are_rejected(self):
        expected = '-m uvicorn gateway:app --host 127.0.0.1 --port 8082 --no-access-log'
        variants = (expected.replace('127.0.0.1', '0.0.0.0'), expected.replace('8082', '8080'),
                    expected.replace('gateway:app', 'unknown:app'), expected+' --reload',
                    expected.replace(' --no-access-log', ''),
                    expected+' --workers 2')
        for args in variants:
            with self.subTest(args=args), self.assertRaises(m.Blocked):
                m.entrypoint(m.DASHBOARD, launch(PYTHON+' '+args))

    def test_shell_wrapper_multiple_launches_ignore_errors_and_argv_mismatch_are_refused(self):
        command = launch(PYTHON+' -m kadima.stock_universe_scanner')
        variants = (command+' '+command,
                    launch('/bin/sh -c arbitrary', executable='/bin/sh'),
                    launch(PYTHON+' -m kadima.stock_universe_scanner', ignore_errors='yes'),
                    launch('/usr/bin/python3 -m kadima.stock_universe_scanner'),
                    'unstructured-command')
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(m.Blocked):
                m.entrypoint(m.SCANNER, raw)

    def test_unknown_unit_has_no_entrypoint_admission(self):
        with self.assertRaises(m.Blocked):
            m.entrypoint('kadima-eod-watchdog.service',
                         launch(PYTHON+' -m kadima.macro_watch'))


class DependencyAdmissionTests(unittest.TestCase):
    def properties(self):
        return dict.fromkeys(m.DEPENDENCIES, '') | dict(Wants='network-online.target',
            Requires='sysinit.target system.slice', Conflicts='shutdown.target')

    def test_existing_engine_is_allowed_only_as_start_dependency(self):
        for key in ('Wants', 'Requires'):
            values = self.properties()
            values[key] += ' kadima.service'
            before = values.copy()
            m.unit_dependencies(values)
            self.assertEqual(values, before)

    def test_root_mount_for_working_directory_and_private_tmp_is_admitted(self):
        values = self.properties()
        values['Requires'] = 'sysinit.target system.slice -.mount'
        values['Wants'] = '-.mount'
        before = values.copy()
        m.unit_dependencies(values)
        self.assertEqual(values, before)

    def test_root_mount_other_relations_and_arbitrary_mounts_are_refused(self):
        for key in self.properties():
            invalid = ['home.mount', 'tmp.mount', 'unreviewed.mount']
            if key not in ('Requires', 'Wants'):
                invalid.append('-.mount')
            for dependency in invalid:
                values = self.properties()
                values[key] = dependency
                with self.subTest(key=key, dependency=dependency), self.assertRaises(m.Blocked):
                    m.unit_dependencies(values)

    def test_engine_stop_trigger_binding_and_unknown_dependencies_are_refused(self):
        for key in self.properties():
            invalid = ['kadima-eod-watchdog.service', 'kadima-session-auto.timer', 'unreviewed.service']
            if key not in ('Wants', 'Requires'):
                invalid.append('kadima.service')
            for dependency in invalid:
                values = self.properties()
                values[key] = dependency
                with self.subTest(key=key, dependency=dependency), self.assertRaises(m.Blocked):
                    m.unit_dependencies(values)

    def test_incomplete_duplicate_or_nonstring_properties_cannot_imply_empty(self):
        for key in self.properties():
            values = self.properties()
            values.pop(key)
            with self.subTest(missing=key), self.assertRaises((m.Blocked, KeyError)):
                m.unit_dependencies(values)
            values = self.properties()
            values[key] = []
            with self.subTest(nonstring=key), self.assertRaises(m.Blocked):
                m.unit_dependencies(values)


class ObserverSourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release, self.target, self.reviewed = (self.root/name for name in ('release', 'target', 'reviewed'))
        spec = importlib.util.spec_from_file_location('_peripheral_source_fixture',
            ROOT/'stock_fast_update/install.py')
        self.base = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.base
        spec.loader.exec_module(self.base)
        files = {
            'kadima/__init__.py': b'',
            'kadima/stock_universe_scanner.py': b'from . import shared\nraise RuntimeError("must_not_execute")\n',
            'kadima/macro_watch.py': b'from .news_context import headline_context\nfrom . import shared\n',
            'kadima/news_context.py': b'def headline_context(title): return title\n',
            'kadima/shared.py': b'from . import nested\n',
            'kadima/nested.py': b'VALUE = 1\n',
        }
        for name, raw in files.items():
            self.write(self.target/name, raw)
            if name != 'kadima/news_context.py':
                self.write(self.release/name, raw)
        self.write(self.release/'kadima/macro_watch.py', b'# older macro observer\n')
        self.overlay_pins = {}
        for name in ('kadima/macro_watch.py', 'kadima/news_context.py'):
            self.write(self.reviewed/Path(name).name, files[name])
            self.overlay_pins[name] = self.base.digest(files[name])
        self.names = set(files)

    def write(self, path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def inspect(self):
        with patch.object(m, 'OBSERVER_OVERLAYS', self.overlay_pins):
            return m.verify_observer_sources(self.base, self.release, self.target, self.reviewed)

    def test_exact_entrypoints_overlay_only_import_and_recursive_dependencies_are_fingerprinted(self):
        before = {path: path.read_bytes() for path in self.target.rglob('*.py')}
        actual = self.inspect()
        self.assertEqual({str(path.relative_to(self.target)) for path in actual}, self.names)
        self.assertEqual(actual, before)
        self.assertEqual(before, {path: path.read_bytes() for path in self.target.rglob('*.py')})
        self.assertFalse((self.release/'kadima/news_context.py').exists())

    def test_changed_entrypoint_overlay_or_transitive_dependency_is_refused(self):
        for name in self.names:
            path = self.target/name
            raw = path.read_bytes()
            path.write_bytes(raw+b'# unexpected installed change\n')
            with self.subTest(name=name), self.assertRaises(m.Blocked):
                self.inspect()
            path.write_bytes(raw)

    def test_missing_transitive_dependency_is_refused(self):
        (self.target/'kadima/nested.py').unlink()
        with self.assertRaises(self.base.Blocked):
            self.inspect()

    def test_modified_reviewed_overlay_reference_is_refused(self):
        for name in self.overlay_pins:
            path = self.reviewed/Path(name).name
            raw = path.read_bytes()
            path.write_bytes(raw+b'# changed reference\n')
            with self.subTest(name=name), self.assertRaises(m.Blocked):
                self.inspect()
            path.write_bytes(raw)

    def test_shared_dependency_uses_already_verified_composed_bytes_without_replacement(self):
        path = self.target/'kadima/shared.py'
        path.write_bytes(b'from . import nested\nLOCAL_CONFIG = True\n')
        admitted = {path: path.read_bytes()}
        with patch.object(m, 'OBSERVER_OVERLAYS', self.overlay_pins):
            result = m.verify_observer_sources(self.base, self.release, self.target, self.reviewed, admitted=admitted)
            self.assertEqual(result[path], admitted[path])
            path.write_bytes(b'CHANGED_AFTER_ADMISSION = True\n')
            with self.assertRaises(m.Blocked):
                m.verify_observer_sources(self.base, self.release, self.target, self.reviewed, admitted=admitted)

    def test_shipped_observer_overlay_blobs_match_reviewed_aws_sources(self):
        for name, expected in m.OBSERVER_OVERLAYS.items():
            raw = (ROOT/'momentum_recovery/reviewed_observers'/Path(name).name).read_bytes()
            with self.subTest(name=name):
                self.assertEqual(self.base.digest(raw), expected)


class SnapshotHealthTests(unittest.TestCase):
    def setUp(self):
        self.after = NOW-timedelta(seconds=30)
        self.payloads = {
            (8082, '/health'): dict(ok=True),
            (8081, '/universe_snapshot'): dict(ts=(NOW-timedelta(seconds=2)).isoformat(),
                tracked=[], prices={}, execution='central_executor_gated', coverage=0),
            (8081, '/macro_snapshot'): dict(ts=(NOW-timedelta(seconds=1)).isoformat(),
                news=[], economic_calendar=[])}
        self.get = Mock(side_effect=lambda port, route: deepcopy(self.payloads[port, route]))

    def check(self):
        return m.snapshot_health(self.get, self.after, NOW)

    def test_fresh_observer_snapshots_do_not_claim_live_market_data(self):
        report = self.check()
        self.assertIs(report['dashboard_gateway_health_verified'], True)
        self.assertIs(report['live_feed_verified'], False)
        self.assertIs(report['market_data_verification_pending'], True)
        self.assertEqual(set(call.args for call in self.get.call_args_list), set(self.payloads))

    def test_stale_prestart_future_naive_or_invalid_snapshot_is_refused(self):
        for route in ('/universe_snapshot', '/macro_snapshot'):
            previous = self.payloads[8081, route]['ts']
            for value in ((self.after-timedelta(seconds=1)).isoformat(),
                          (NOW+timedelta(seconds=1)).isoformat(),
                          NOW.replace(tzinfo=None).isoformat(), 'bad', None):
                self.payloads[8081, route]['ts'] = value
                with self.subTest(route=route, value=value), self.assertRaises(m.Blocked):
                    self.check()
            self.payloads[8081, route]['ts'] = previous

    def test_snapshot_older_than_three_minutes_is_refused_even_after_start(self):
        self.after = NOW-timedelta(minutes=4)
        self.payloads[8081, '/universe_snapshot']['ts'] = (NOW-timedelta(seconds=181)).isoformat()
        with self.assertRaises(m.Blocked):
            self.check()

    def test_unsafe_execution_and_wrong_schema_are_refused(self):
        for route, key, bad in (('/universe_snapshot', 'execution', 'direct'),
                               ('/universe_snapshot', 'prices', []),
                               ('/universe_snapshot', 'tracked', {}),
                               ('/macro_snapshot', 'news', {}),
                               ('/macro_snapshot', 'economic_calendar', {})):
            before = self.payloads[8081, route][key]
            self.payloads[8081, route][key] = bad
            with self.subTest(route=route, key=key), self.assertRaises(m.Blocked):
                self.check()
            self.payloads[8081, route][key] = before

    def test_gateway_health_requires_boolean_success(self):
        for value in (False, 'true', 1, None):
            self.payloads[8082, '/health']['ok'] = value
            with self.subTest(value=value), self.assertRaises(m.Blocked):
                self.check()


class UnitContextTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('_peripheral_context_fixture',
            ROOT/'momentum_recovery/stopped_context.py')
        self.context = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.context
        spec.loader.exec_module(self.context)
        self.properties = dict.fromkeys(self.context.PROPERTIES, '')
        self.properties.update(LoadState='loaded', ActiveState='inactive', MainPID='0',
            User='ubuntu', Group='', DynamicUser='no', NeedDaemonReload='no',
            WorkingDirectory=str(m.TARGET), FragmentPath='/etc/systemd/system/'+m.SCANNER,
            ExecStart=launch(PYTHON+' -m kadima.stock_universe_scanner'),
            Environment='KADIMA_CELESTIAL_ENABLED=1 ANGEL_API_KEY=private-inline',
            EnvironmentFiles='/etc/kadima.env (ignore_errors=no)')
        self.dependencies = dict.fromkeys(m.DEPENDENCIES, '')
        self.files = {Path(self.properties['FragmentPath']): self.evidence(
            self.properties['FragmentPath'], b'[Service]\n'),
            Path('/etc/kadima.env'): self.evidence('/etc/kadima.env',
                b'ANGEL_API_KEY=private-file\nANGEL_PASSWORD=private-password\n')}
        self.manager = ''
        self.raw_extra = ''
        self.omit_arrays = False
        self.proof_override = None
        self.calls = []
        self.user = SimpleNamespace(pw_name='ubuntu', pw_uid=1000, pw_gid=1000)
        self.cm = SimpleNamespace(**{name: getattr(self.context, name) for name in
            ('PROPERTIES', 'EMPTY_PROPERTIES', 'service_properties', 'parse_exec_start',
             'words', 'parse_environment', 'parse_environment_files',
             'parse_environment_file', 'NAME', 'assignment')},
            invoke=self.invoke, read_regular=self.read,
            verify_interpreter=Mock(return_value=dict(real='/usr/bin/python3.13')))

    def evidence(self, path, raw, *, uid=0, mode=0o600):
        return self.context.FileEvidence(Path(path), raw,
            (1, 10, uid, 0, mode, 1, len(raw), 1, 1))

    def invoke(self, command):
        self.calls.append(command)
        if command[0] == '/usr/bin/busctl':
            self.assertEqual(command[4], '/org/freedesktop/systemd1/unit/kadima_2duniverse_2dscanner_2eservice')
            return self.proof_override if self.proof_override is not None else \
                self.context.EMPTY_STRUCT_ARRAYS[command[-1]]+' 0\n'
        if command[1:] == ['show-environment']:
            return self.manager
        self.assertEqual(command[:3], ['/usr/bin/systemctl', 'show', m.SCANNER])
        if command[command.index('-p')+1] == 'Wants':
            return ''.join(key+'='+value+'\n' for key, value in self.dependencies.items())
        return ''.join(key+'='+value+'\n' for key, value in self.properties.items()
            if not (self.omit_arrays and not value and key in self.context.EMPTY_STRUCT_ARRAYS))+self.raw_extra

    def read(self, path, *, missing=False):
        if missing and path not in self.files:
            return None
        return self.files[path]

    def inspect(self):
        return m.inspect_unit(m.SCANNER, self.cm, self.user)

    def test_reviewed_context_reads_ordered_environment_without_exposing_values(self):
        value = self.inspect()
        self.assertEqual(value['env']['ANGEL_API_KEY'], 'private-file')
        self.assertEqual(value['summary']['unit'], m.SCANNER)
        self.assertIs(value['summary']['credentials_printed'], False)
        self.assertIs(value['summary']['runtime_imported'], False)
        for secret in ('private-inline', 'private-file', 'private-password'):
            self.assertNotIn(secret, json.dumps(value['summary']))

    def test_omitted_typed_arrays_are_proven_for_scanner_unit_not_engine(self):
        self.omit_arrays = True
        value = self.inspect()
        self.assertTrue(any(command[0] == '/usr/bin/busctl' for command in self.calls))
        self.assertIn('ExecStartPre', value['summary']['property_shape']['typed_empty_properties_verified'])

    def test_nonempty_or_wrong_type_omitted_array_proof_refuses_with_redacted_summary(self):
        self.omit_arrays = True
        for proof in ('as 0\n', 'a(sasbttttuii) 1 private-command\n', ''):
            self.proof_override = proof
            with self.subTest(proof=proof), self.assertRaises(RuntimeError) as failure:
                self.inspect()
            summary = failure.exception.summary
            self.assertEqual(summary['unit'], m.SCANNER)
            self.assertNotIn('private-command', json.dumps(summary))
            self.assertIs(summary['runtime_imported'], False)

    def test_extra_launch_hook_never_admits_or_exposes_command(self):
        self.properties['ExecStartPre'] = 'private-hook-command'
        with self.assertRaises(RuntimeError) as failure:
            self.inspect()
        self.assertNotIn('private-hook-command', json.dumps(failure.exception.summary))
        self.cm.verify_interpreter.assert_not_called()

    def test_fragment_owner_writability_and_data_directory_changes_are_rejected(self):
        path = Path(self.properties['FragmentPath'])
        for uid, mode in ((1000, 0o600), (0, 0o666)):
            self.files[path] = self.evidence(path, b'[Service]\n', uid=uid, mode=mode)
            with self.subTest(uid=uid, mode=mode), self.assertRaises(m.Blocked):
                self.inspect()
        self.files[path] = self.evidence(path, b'[Service]\n')
        self.properties['Environment'] += ' KADIMA_DATA_DIR=/tmp/other-database'
        with self.assertRaises(m.Blocked):
            self.inspect()

    def test_manager_only_proxy_or_python_injection_is_refused(self):
        for value in ('LD_PRELOAD=private-lib', 'PYTHONPATH=private-python',
                      'https_proxy=private-proxy', 'KADIMA_FORCE_START=1'):
            self.manager = value+'\n'
            with self.subTest(value=value), self.assertRaises(m.Blocked) as failure:
                self.inspect()
            self.assertNotIn(value, json.dumps(failure.exception.summary))

    def test_repeated_properties_and_dependency_shape_cannot_hide_changes(self):
        self.raw_extra = 'User=root\n'
        with self.assertRaises(RuntimeError):
            self.inspect()
        self.raw_extra = ''
        self.dependencies['OnFailure'] = 'kadima-eod-watchdog.service'
        with self.assertRaises(m.Blocked):
            self.inspect()

    def test_unreviewed_dependency_failure_identifies_units_without_credentials(self):
        self.dependencies['Wants'] = 'unreviewed.mount'
        with self.assertRaises(m.Blocked) as failure:
            self.inspect()
        summary = failure.exception.summary
        self.assertEqual(summary['dependencies']['Wants'], 'unreviewed.mount')
        self.assertEqual(str(failure.exception), 'unreviewed_peripheral_dependency:Wants')
        for secret in ('private-inline', 'private-file', 'private-password'):
            self.assertNotIn(secret, json.dumps(summary))

    def test_dependency_display_reordering_does_not_change_service_context(self):
        self.dependencies['Requires'] = 'system.slice sysinit.target -.mount'
        before = self.inspect()['stable']
        self.dependencies['Requires'] = '-.mount   sysinit.target system.slice'
        self.assertEqual(before, self.inspect()['stable'])
        self.dependencies['Wants'] = 'network-online.target'
        self.assertNotEqual(before, self.inspect()['stable'])


class StartupRecordTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('_peripheral_recovery_fixture',
            ROOT/'tests/test_repaired_paused_start.py')
        fixture = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = fixture
        spec.loader.exec_module(fixture)
        self.case = fixture.BeforeStartResumeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.start = SimpleNamespace(RESUME_STATE=self.case.state/'resume-before-start',
            STARTUP_MANUAL_HOLDING=fixture.m.STARTUP_MANUAL_HOLDING,
            verify_resume=lambda helper, deps: fixture.m.verify_resume(helper, deps, self.case.state, self.case.dropin))
        self.start.RESUME_STATE.mkdir(mode=0o700)
        self.status = dict(action='repaired_paused_engine_start', state='repaired_engine_running_paused',
            activation=False, start_requested=[m.ENGINE], start_command_invoked=True,
            resumed_before_start=True, override_write_requested=False, override_retained=True,
            runtime_source_changed=False, ledger_repaired=False, peripheral_services_started=False,
            peripheral_startup_pending=True, stopped_after_failure=[],
            override=dict(path=str(self.case.dropin), original_present=False, retained=True),
            startup_manual_holding_exception=dict(self.start.STARTUP_MANUAL_HOLDING),
            process=dict(pid='206362', started_ticks='104422145'), observed_at=NOW.isoformat())
        self.attempt = dict(started_at=(NOW-timedelta(seconds=30)).isoformat())
        self.write()

    def write(self):
        for name, value in (('status.json', self.status), ('attempted.json', self.attempt)):
            path = self.start.RESUME_STATE/name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def verify(self):
        return m.verify_startup_record(self.start, self.case.helper, self.case.dependencies, NOW)

    def test_successful_recovery_and_original_failure_are_preserved_and_fingerprinted(self):
        before = {path: path.read_bytes() for path in self.case.state.rglob('*.json')}
        status, started, fingerprints = self.verify()
        self.assertEqual(status, self.status)
        self.assertEqual(started.isoformat(), self.attempt['started_at'])
        self.assertEqual(fingerprints, before)
        self.assertEqual(before, {path: path.read_bytes() for path in self.case.state.rglob('*.json')})

    def test_original_failed_status_cannot_substitute_for_successful_recovery(self):
        self.status = self.case.status
        self.write()
        with self.assertRaises(m.Blocked):
            self.verify()

    def test_ambiguous_activated_or_wrong_manual_recovery_is_refused(self):
        before = deepcopy(self.status)
        variants = [dict(activation=True), dict(start_command_invoked=False), dict(resumed_before_start=False),
                    dict(blocked='error'), dict(status_recording_failed=True), dict(engine_stopped_after_failure=True),
                    dict(start_requested=[m.DASHBOARD]), dict(override_write_requested=True),
                    dict(startup_manual_holding_exception=dict(self.start.STARTUP_MANUAL_HOLDING, netqty=130)),
                    dict(startup_manual_holding_exception=dict(self.start.STARTUP_MANUAL_HOLDING, netqty=65.0))]
        for change in variants:
            self.status = dict(before, **change)
            self.write()
            with self.subTest(change=change), self.assertRaises(m.Blocked):
                self.verify()

    def test_missing_public_or_future_recovery_records_are_refused(self):
        status = self.start.RESUME_STATE/'status.json'
        status.chmod(0o644)
        with self.assertRaises(RuntimeError): self.verify()
        self.status['observed_at'] = (NOW+timedelta(seconds=1)).isoformat()
        self.write()
        with self.assertRaises(m.Blocked): self.verify()
        status.unlink()
        with self.assertRaises(FileNotFoundError): self.verify()

    def test_invalid_process_identity_or_missing_original_provenance_is_refused(self):
        before = deepcopy(self.status['process'])
        for process in (dict(pid='0', started_ticks='1'), dict(pid=206362, started_ticks='104422145'), {}, None):
            self.status['process'] = process
            self.write()
            with self.subTest(process=process), self.assertRaises(m.Blocked): self.verify()
        self.status['process'] = before
        self.write()
        (self.case.state/'attempted.json').unlink()
        with self.assertRaises(FileNotFoundError): self.verify()

    def test_pinned_current_startup_helper_loads_with_confirmed_pe_exception(self):
        start = m.load_start()
        self.assertEqual(start.STARTUP_MANUAL_HOLDING, self.start.STARTUP_MANUAL_HOLDING)


class PeripheralTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root/'state'
        self.units = (m.DASHBOARD, m.SCANNER, m.MACRO)
        self.active = dict.fromkeys(self.units, False)
        self.events = []
        self.statuses = []
        self.start_error = None
        self.stop_error = None
        self.identities = {unit: dict(pid=1000+index) for index, unit in enumerate(self.units)}

        def private_path(path, *, directory=False):
            path = Path(path)
            info = path.lstat()
            self.assertEqual(info.st_uid, os.getuid())
            self.assertFalse(info.st_mode & 0o077)
            self.assertFalse(path.is_symlink())
            self.assertTrue(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            return path

        def claim_attempt(path, now):
            self.events.append('claim')
            private_path(path, directory=True)
            with os.fdopen(os.open(Path(path)/'attempted.json',
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'wb') as stream:
                stream.write(json.dumps(dict(started_at=now.isoformat())).encode())

        def record_status(path, report):
            private_path(path, directory=True)
            self.statuses.append(deepcopy(report))
            self.events.append('record')

        self.helper = SimpleNamespace(root_parents=lambda path: None, private_path=private_path,
            claim_attempt=claim_attempt, record_status=record_status, utc_now=lambda: NOW)
        self.admission = Mock(side_effect=self.admit)
        self.health = Mock(side_effect=self.healthy)
        self.guard = Mock(side_effect=self.guarded)
        self.process = Mock(side_effect=self.identity)

    def admit(self):
        self.events.append('admission')
        return {}

    def healthy(self):
        self.events.append('health')
        return dict(ok=True)

    def guarded(self):
        self.events.append('guard')

    def identity(self, unit):
        self.events.append(('process', unit))
        self.assertTrue(self.active[unit])
        return deepcopy(self.identities[unit])

    def observe(self, unit):
        self.events.append(('observe', unit))
        return dict(ActiveState='active' if self.active[unit] else 'inactive',
                    MainPID=str(self.identities[unit]['pid']) if self.active[unit] else '0')

    def invoke(self, command):
        self.events.append(tuple(command))
        self.assertEqual(command[0], '/usr/bin/systemctl')
        self.assertEqual(len(command), 3)
        unit = command[2]
        self.assertIn(unit, self.units, 'Engine and watchdog must never be changed')
        if command[1] == 'start':
            self.active[unit] = True
            return SimpleNamespace(returncode=1 if self.start_error == unit else 0)
        self.assertEqual(command[1], 'stop')
        if self.stop_error != unit:
            self.active[unit] = False
        return SimpleNamespace(returncode=1 if self.stop_error == unit else 0)

    def run_transaction(self, *, apply=True):
        return m.transaction(apply=apply, helper=self.helper, state_dir=self.state,
            admission=self.admission, observe=self.observe, invoke=self.invoke,
            health=self.health, guard=self.guard, process=self.process)

    def actions(self):
        return [event for event in self.events if isinstance(event, tuple) and event[0] == '/usr/bin/systemctl']

    def starts(self):
        return [event[2] for event in self.actions() if event[1] == 'start']

    def stops(self):
        return [event[2] for event in self.actions() if event[1] == 'stop']

    def test_plan_has_no_attempt_file_health_probe_or_service_mutation(self):
        report = self.run_transaction(apply=False)
        self.admission.assert_called_once()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.actions(), [])
        self.health.assert_not_called()
        self.process.assert_not_called()
        self.assertIs(report['activation'], False)

    def test_admission_failure_never_claims_or_changes_services(self):
        self.admission.side_effect = RuntimeError('synthetic_admission_failure')
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.actions(), [])

    def test_any_active_peripheral_is_refused_without_stopping_existing_process(self):
        for unit in self.units:
            self.active = dict.fromkeys(self.units, False)
            self.active[unit] = True
            with self.subTest(unit=unit), self.assertRaises(m.Blocked):
                self.run_transaction()
            self.assertTrue(self.active[unit])
            self.assertEqual(self.actions(), [])

    def test_existing_attempt_refuses_replay_without_service_mutation(self):
        self.state.mkdir(mode=0o700)
        attempt = self.state/'attempted.json'
        attempt.write_bytes(b'{"started_at":"earlier"}')
        attempt.chmod(0o600)
        with self.assertRaises((FileExistsError, RuntimeError)):
            self.run_transaction()
        self.assertEqual(attempt.read_bytes(), b'{"started_at":"earlier"}')
        self.assertEqual(self.actions(), [])

    def test_success_starts_in_order_then_checks_health_and_final_process_identity(self):
        report = self.run_transaction()
        self.assertEqual(self.starts(), list(self.units))
        self.assertEqual(self.stops(), [])
        self.assertTrue(all(self.active.values()))
        self.assertTrue((self.state/'attempted.json').exists())
        self.assertEqual(report['state'], 'paused_peripherals_running')
        self.assertEqual(report['start_requested'], list(self.units))
        self.assertEqual(report['processes'], self.identities)
        self.assertIs(report['activation'], False)
        self.assertGreaterEqual(self.guard.call_count, len(self.units)+1)
        self.assertEqual(self.events.count(('process', m.SCANNER)), 2)
        self.assertLess(self.events.index('claim'), self.events.index(('/usr/bin/systemctl', 'start', m.DASHBOARD)))
        self.assertGreater(self.events.index('health'), self.events.index(('/usr/bin/systemctl', 'start', m.MACRO)))
        self.assertGreater(len(self.events)-1-self.events[::-1].index(('process', m.SCANNER)),
                           self.events.index('health'))

    def test_partial_start_failure_stops_attempted_units_in_reverse_order(self):
        self.start_error = m.SCANNER
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.assertEqual(self.starts(), [m.DASHBOARD, m.SCANNER])
        self.assertEqual(self.stops(), [m.SCANNER, m.DASHBOARD])
        self.assertFalse(any(self.active.values()))
        self.health.assert_not_called()

    def test_health_failure_rolls_back_only_all_three_started_peripherals(self):
        self.health.side_effect = RuntimeError('synthetic_health_failure')
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.assertEqual(self.stops(), list(reversed(self.units)))
        self.assertFalse(any(self.active.values()))

    def test_process_identity_failure_cleans_only_services_already_attempted(self):
        def identity(unit):
            if unit == m.SCANNER:
                raise RuntimeError('synthetic_process_mismatch')
            return self.identity(unit)
        self.process.side_effect = identity
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.assertEqual(self.stops(), list(reversed(self.starts())))
        self.assertFalse(any(self.active.values()))
        self.health.assert_not_called()

    def test_guard_failure_after_health_rolls_back_started_services(self):
        def guard():
            if self.health.call_count:
                raise RuntimeError('synthetic_guard_failure')
            self.guarded()
        self.guard.side_effect = guard
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.assertEqual(self.stops(), list(reversed(self.units)))

    def test_changed_final_process_identity_cannot_report_success(self):
        def health():
            self.identities[m.SCANNER]['pid'] += 1
            return self.healthy()
        self.health.side_effect = health
        with self.assertRaises(m.Blocked):
            self.run_transaction()
        self.assertEqual(self.stops(), list(reversed(self.units)))

    def test_external_start_during_guard_is_not_claimed_or_stopped(self):
        def guard():
            self.active[m.SCANNER] = True
            self.guarded()
        self.guard.side_effect = guard
        with self.assertRaises(m.Blocked):
            self.run_transaction()
        self.assertNotIn(m.SCANNER, self.starts())
        self.assertNotIn(m.SCANNER, self.stops())
        self.assertTrue(self.active[m.SCANNER])
        self.assertEqual(self.stops(), list(reversed(self.starts())))

    def test_cleanup_failure_keeps_truthful_report_and_continues_remaining_stops(self):
        self.stop_error = m.MACRO
        self.health.side_effect = RuntimeError('synthetic_health_failure')
        with self.assertRaises(RuntimeError) as failure:
            self.run_transaction()
        self.assertEqual(self.stops(), list(reversed(self.units)))
        self.assertTrue(self.active[m.MACRO])
        self.assertFalse(self.active[m.SCANNER])
        self.assertFalse(self.active[m.DASHBOARD])
        report = failure.exception.report
        self.assertEqual(report['state'], 'paused_peripherals_blocked')
        self.assertNotIn(m.MACRO, report['stopped_after_failure'])
        self.assertTrue(report['cleanup_failures'])
        self.assertEqual(report, self.statuses[-1])


if __name__ == '__main__':
    unittest.main()
