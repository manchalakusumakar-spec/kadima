"""Offline engine-only startup checks with real private attempt records.

The fake service manager cannot touch host services, and health probes use
synthetic payloads rather than a broker or network connection.
"""
from copy import deepcopy
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_test_repaired_paused_start',
    ROOT/'momentum_recovery/start_repaired_paused.py')
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)
NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)


def broker_observation(**kwargs):
    spec = importlib.util.spec_from_file_location('_paused_broker_fixture',
        ROOT/'tests/test_stopped_broker_read.py')
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    value, _, _ = fixtures.ChildReaderTests().child(**kwargs)
    return fixtures.m, value


def pending_broker_failure():
    broker, receipt = broker_observation(orders=[dict(exchange='NFO',
        tradingsymbol='NIFTY29SEP2623250CE', symboltoken='73921', orderid='pending1',
        orderstatus='trigger pending', quantity='130', transactiontype='SELL',
        clientcode='PRIVATE_ACCOUNT', apikey='SECRET_KEY')])
    try:
        m.require_broker_readiness(receipt, broker.validate_receipt, m.utc_now())
    except m.Blocked as exc:
        return exc
    raise AssertionError('Pending order was admitted')


class BrokerDiagnosticTests(unittest.TestCase):
    def test_pending_order_receipt_preserves_identity_and_suppresses_provider_fields(self):
        exc = pending_broker_failure()
        receipt = exc.broker_readiness
        self.assertEqual(str(exc), 'fresh_broker_readiness_required')
        self.assertEqual(receipt['pending_orders'], 1)
        self.assertEqual(receipt['pending_order_details'][0]['orderid'], 'pending1')
        self.assertEqual(receipt['pending_order_details'][0]['status'], 'trigger pending')
        for secret in ('PRIVATE_ACCOUNT', 'SECRET_KEY', 'clientcode', 'apikey'):
            self.assertNotIn(secret, json.dumps(receipt))

    def test_changed_manual_quantity_reports_unexpected_exposure(self):
        broker, _ = broker_observation()
        broker, receipt = broker_observation(positions=[dict(broker.ALLOWED_HOLDING, netqty=65)])
        with self.assertRaises(m.Blocked) as result:
            m.require_broker_readiness(receipt, broker.validate_receipt, m.utc_now())
        detail = result.exception.broker_readiness
        self.assertEqual(detail['nonzero_positions'][0]['netqty'], 65)
        self.assertEqual(detail['error']['reason'], 'unexpected_exposure')
        receipt['nonzero_positions'][0]['netqty'] = 999
        self.assertEqual(detail['nonzero_positions'][0]['netqty'], 65)

    def test_incomplete_login_receipt_reports_redacted_reason(self):
        broker, receipt = broker_observation(login_error=RuntimeError('SECRET_LOGIN_RESPONSE'))
        with self.assertRaises(m.Blocked) as result:
            m.require_broker_readiness(receipt, broker.validate_receipt, m.utc_now())
        detail = result.exception.broker_readiness
        self.assertFalse(detail['books_complete'])
        self.assertEqual(detail['error'], dict(stage='login', reason='login_failed', type='RuntimeError'))
        self.assertNotIn('SECRET_LOGIN_RESPONSE', json.dumps(detail))

    def test_flat_or_exact_manual_holding_still_passes(self):
        broker, _ = broker_observation()
        for positions in ([], [broker.ALLOWED_HOLDING]):
            with self.subTest(positions=positions):
                broker, receipt = broker_observation(positions=positions)
                before = deepcopy(receipt)
                self.assertIs(m.require_broker_readiness(receipt, broker.validate_receipt, m.utc_now()), receipt)
                self.assertEqual(receipt, before)

    def test_invalid_or_stale_receipt_is_never_attached(self):
        for kind in ('extra_field', 'stale', 'unbounded_details'):
            with self.subTest(kind=kind):
                broker, receipt = broker_observation()
                if kind == 'extra_field':
                    receipt['token'] = 'UNVALIDATED_SECRET'
                elif kind == 'stale':
                    receipt['started_at'] = (m.utc_now()-timedelta(minutes=2)).isoformat()
                else:
                    receipt['pending_order_details'] = ['UNVALIDATED_SECRET']*9
                with self.assertRaises(broker.Blocked) as result:
                    m.require_broker_readiness(receipt, broker.validate_receipt, m.utc_now())
                self.assertFalse(hasattr(result.exception, 'broker_readiness'))
                self.assertNotIn('UNVALIDATED_SECRET', str(result.exception))

    def test_main_prints_validated_initial_admission_receipt(self):
        failure = pending_broker_failure()
        output = io.StringIO()
        with patch.object(m, 'load_repair', side_effect=failure), \
             patch.object(m.os, 'geteuid', return_value=0), patch.object(m.os, 'umask'), \
             redirect_stdout(output), self.assertRaises(SystemExit) as result:
            m.main(['--history', '/unused', '--evidence', '/unused', '--resume-before-start', '--apply'])
        self.assertEqual(result.exception.code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report['broker_readiness'], failure.broker_readiness)
        self.assertEqual(report['start_requested'], [])
        self.assertFalse(report['activation'])
        self.assertEqual(report['state'], 'blocked')


class PausedEnvironmentTests(unittest.TestCase):
    def environment(self):
        return dict(KADIMA_CELESTIAL_ENABLED='1', KADIMA_SESSION_AUTOTRADE='1',
                    ANGEL_API_KEY='synthetic-private-key', UNRELATED='preserved')

    def test_celestial_and_session_gate_are_retained_without_mutation(self):
        environment = self.environment()
        before = environment.copy()
        m.paused_environment(environment)
        self.assertEqual(environment, before)

    def test_missing_or_disabled_celestial_or_session_gate_blocks(self):
        for name in ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_SESSION_AUTOTRADE'):
            for value in (None, '0', '', 'true', '1 '):
                environment = self.environment()
                if value is None:
                    environment.pop(name)
                else:
                    environment[name] = value
                with self.subTest(name=name, value=value), self.assertRaises(m.Blocked):
                    m.paused_environment(environment)

    def test_every_independent_live_or_force_flag_must_be_absent(self):
        expected = {'KADIMA_FORCE_START', 'KADIMA_MCX_LIVE',
                    'KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_CAS_LIVE',
                    'KADIMA_GAP_LIVE', 'KADIMA_MOVE_CAPTURE_LIVE'}
        self.assertEqual(set(m.UNSET_FLAGS), expected)
        for name in expected:
            for value in ('1', '0', ''):
                environment = self.environment()
                environment[name] = value
                with self.subTest(name=name, value=value), self.assertRaises(m.Blocked):
                    m.paused_environment(environment)


class DependencyAdmissionTests(unittest.TestCase):
    def properties(self):
        return dict.fromkeys(m.DEPENDENCIES, '') | dict(
            Wants='network-online.target', Requires='sysinit.target system.slice',
            Conflicts='shutdown.target')

    def test_infrastructure_only_dependencies_are_admitted(self):
        properties = self.properties()
        original = properties.copy()
        m.dependency_admission(properties)
        self.assertEqual(properties, original)

    def test_observed_aws_root_mount_requires_is_admitted(self):
        properties = dict.fromkeys(m.DEPENDENCIES, '') | dict(
            Requires='sysinit.target system.slice -.mount', Conflicts='shutdown.target')
        raw = ''.join(f'{key}={value}\n' for key, value in properties.items())
        self.assertEqual(m.dependency_snapshot(lambda command: raw),
                         properties | dict(Requires='-.mount sysinit.target system.slice'))

    def test_dependency_reordering_after_reload_is_not_a_change(self):
        properties = self.properties() | dict(Requires='sysinit.target system.slice -.mount')
        raw = lambda p: ''.join(f'{key}={value}\n' for key, value in p.items())
        expected = m.dependency_snapshot(lambda command: raw(properties))
        reordered = properties | dict(Requires='-.mount   system.slice sysinit.target')
        m.verify_dependencies(lambda command: raw(reordered), expected)

    def test_real_dependency_change_still_blocks_with_names(self):
        properties = self.properties()
        raw = lambda p: ''.join(f'{key}={value}\n' for key, value in p.items())
        expected = m.dependency_snapshot(lambda command: raw(properties))
        changed = properties | dict(Wants='network-online.target local-fs.target')
        with self.assertRaises(m.Blocked) as result:
            m.verify_dependencies(lambda command: raw(changed), expected)
        self.assertEqual(result.exception.dependency_changes, dict(Wants=dict(
            before=['network-online.target'], after=['local-fs.target', 'network-online.target'])))

    def test_root_mount_exception_is_only_requires_and_never_a_mount_wildcard(self):
        for key, name in [('Requires', 'home.mount'), ('Requires', 'unreviewed.mount')] + [
                (key, '-.mount') for key in m.DEPENDENCIES if key != 'Requires']:
            properties = self.properties()
            properties[key] = name
            with self.subTest(key=key, name=name), self.assertRaises(m.Blocked):
                m.dependency_admission(properties)

    def test_watchdog_session_timer_and_unknown_units_are_rejected(self):
        for name in self.properties():
            for dependency in ('kadima-eod-watchdog.service', 'kadima-session-auto.timer',
                               'unreviewed.service', 'arbitrary.timer'):
                properties = self.properties()
                properties[name] = dependency
                with self.subTest(name=name, dependency=dependency), self.assertRaises(m.Blocked):
                    m.dependency_admission(properties)

    def test_missing_dependency_property_cannot_imply_empty(self):
        for name in self.properties():
            properties = self.properties()
            properties.pop(name)
            with self.subTest(name=name), self.assertRaises((m.Blocked, KeyError)):
                m.dependency_admission(properties)

    def test_duplicate_property_output_never_hides_a_missing_dependency(self):
        properties = self.properties()
        raw = ''.join(f'{key}={value}\n' for key, value in properties.items())
        raw = raw.replace('Triggers=\n', 'Wants=network-online.target\n')
        with self.assertRaises(m.Blocked):
            m.dependency_snapshot(lambda command: raw)


class BeforeStartResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root/'state'
        self.state.mkdir(mode=0o700)
        self.dropin = self.root/'paused.conf'
        self.dropin.write_bytes(m.DROPIN_BYTES)
        self.dropin.chmod(0o600)
        self.status = dict(action='repaired_paused_engine_start', activation=False,
            state='blocked_paused_start', blocked='engine_dependencies_changed', ledger_repaired=False,
            runtime_source_changed=False, start_command_invoked=False, start_requested=[],
            stopped_after_failure=[], peripheral_services_started=False, peripheral_startup_pending=True,
            override_retained=True, override_write_requested=True,
            override=dict(path=str(self.dropin), original_present=False, retained=True),
            observed_at=NOW.isoformat())
        self.attempt = dict(started_at=(NOW-timedelta(seconds=5)).isoformat())
        self.manifest = dict(path=str(self.dropin), original_present=False,
                             replacement_sha256=hashlib.sha256(m.DROPIN_BYTES).hexdigest())
        self.dependencies = dict.fromkeys(m.DEPENDENCIES, '') | dict(
            Requires='-.mount sysinit.target system.slice', Conflicts='shutdown.target')
        def private(path, directory=False):
            path = Path(path)
            info = path.lstat()
            if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077 or (
                    not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                raise m.Blocked('root_private_path_required')
            return path
        self.helper = SimpleNamespace(root_parents=lambda p: None, private_path=private,
                                      read_regular=lambda p, limit=100000: p.read_bytes())
        self.write_records()

    def write_records(self):
        for name, value in (('status.json', self.status), ('attempted.json', self.attempt),
                            ('override.json', self.manifest)):
            path = self.state/name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def verify(self):
        with patch.object(m, 'utc_now', return_value=NOW+timedelta(seconds=10)):
            return m.verify_resume(self.helper, self.dependencies, self.state, self.dropin)

    def test_exact_before_start_failure_is_read_only_and_preserves_records(self):
        before = {p: p.read_bytes() for p in self.state.iterdir()}
        records, override = self.verify()
        self.assertEqual(records, before)
        self.assertEqual(override, self.status['override'])
        self.assertEqual({p: p.read_bytes() for p in self.state.iterdir()}, before)
        self.assertEqual(self.dropin.read_bytes(), m.DROPIN_BYTES)

    def test_started_unknown_or_partial_attempt_is_never_resumed(self):
        original = deepcopy(self.status)
        changes = [dict(start_command_invoked=True), dict(start_command_invoked=0),
                   dict(start_requested=['kadima.service']), dict(state='starting_paused_engine'),
                   dict(blocked='engine_api_start_timeout'), dict(status_recording_failed=True),
                   dict(process={'pid':'1234'}), dict(health={}), dict(engine_stopped_after_failure=True),
                   dict(override_retained=None), dict(resumed_before_start=True)]
        for change in changes:
            self.status = original | change
            self.write_records()
            with self.subTest(change=change), self.assertRaises(m.Blocked):
                self.verify()

    def test_override_manifest_bytes_or_private_permissions_must_match(self):
        self.manifest['original_present'] = True
        self.write_records()
        with self.assertRaises(m.Blocked): self.verify()
        self.manifest['original_present'] = False
        self.write_records()
        self.dropin.write_bytes(b'[Service]\n')
        with self.assertRaises(m.Blocked): self.verify()
        self.dropin.write_bytes(m.DROPIN_BYTES)
        (self.state/'status.json').chmod(0o644)
        with self.assertRaises(m.Blocked): self.verify()

    def test_resume_requires_observed_dependencies_not_just_allowlisted_units(self):
        self.dependencies['Wants'] = 'network-online.target'
        with self.assertRaisesRegex(m.Blocked, 'reviewed_resume_dependencies_required'):
            self.verify()

    def test_future_or_reversed_chronology_blocks(self):
        for at in (NOW+timedelta(minutes=1), NOW-timedelta(minutes=1)):
            self.status['observed_at'] = at.isoformat()
            self.write_records()
            with self.subTest(at=at), self.assertRaises(m.Blocked): self.verify()


class LocalGetTests(unittest.TestCase):
    def test_reviewed_loopback_get_has_no_body_proxy_or_redirect_following(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(m.urllib.request, 'build_opener', return_value=opener) as build:
            result = m.local_get(8081, '/api/kadima/config', 'synthetic-private-token')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8081/api/kadima/config')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-private-token')
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], m.NoRedirect)
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, None, None, None, None))
        self.assertEqual(result, dict(ok=True))

    def test_mutation_routes_and_remote_ports_are_refused_before_network(self):
        with patch.object(m.urllib.request, 'build_opener') as build:
            for port, path in ((8081, '/api/kadima/pause'), (8081, '/api/order/buy'),
                               (8082, '/health'), (443, '/api/kadima/config')):
                with self.subTest(port=port, path=path), self.assertRaises(m.Blocked):
                    m.local_get(port, path, 'synthetic-private-token')
        build.assert_not_called()


class DropinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root/'kadima.service.d'/'paused.conf'
        self.state = self.root/'state'
        self.state.mkdir(mode=0o700)
        self.created = []
        def create_private(path, raw):
            self.created.append(Path(path))
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY,
                                   0o600), 'wb') as stream:
                stream.write(raw)
        self.helper = SimpleNamespace(root_parents=Mock(), create_private=create_private,
                                      read_regular=lambda path: path.read_bytes())
        # Only the synthetic directory's observed owner is root. Real bytes,
        # permissions, links and filesystem operations remain under test.
        actual_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            observed = actual_lstat(path, *args, **kwargs)
            if path == self.path.parent:
                values = list(observed)
                values[4] = 0
                return os.stat_result(values)
            return observed
        owner = patch.object(Path, 'lstat', lstat)
        owner.start()
        self.addCleanup(owner.stop)

    def test_override_has_only_live_flag_unsets_and_private_reversible_manifest(self):
        report = m.prepare_dropin(self.helper, self.state, self.path)
        self.assertEqual(self.path.read_bytes(), b'[Service]\nUnsetEnvironment='+
                         ' '.join(m.UNSET_FLAGS).encode()+b'\n')
        self.assertNotIn(b'KADIMA_CELESTIAL_ENABLED', self.path.read_bytes())
        self.assertNotIn(b'KADIMA_SESSION_AUTOTRADE', self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        manifest_path = self.state/'override.json'
        manifest = json.loads(manifest_path.read_bytes())
        self.assertEqual(manifest['path'], str(self.path))
        self.assertIs(manifest['original_present'], False)
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.created, [manifest_path, self.path])
        self.assertIs(report['retained'], True)

    def test_existing_operator_dropin_is_never_replaced_even_if_bytes_match(self):
        self.path.parent.mkdir()
        for raw in (b'[Service]\nEnvironment=UNRELATED=operator\n', m.DROPIN_BYTES):
            self.path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(m.Blocked):
                m.prepare_dropin(self.helper, self.state, self.path)
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertFalse((self.state/'override.json').exists())
        self.assertEqual(self.created, [])

    def test_symlink_dropin_and_writable_parent_are_refused(self):
        self.path.parent.mkdir()
        victim = self.root/'operator.conf'
        victim.write_bytes(b'operator content')
        self.path.symlink_to(victim)
        with self.assertRaises(m.Blocked):
            m.prepare_dropin(self.helper, self.state, self.path)
        self.assertEqual(victim.read_bytes(), b'operator content')
        self.path.unlink()
        self.path.parent.chmod(0o777)
        with self.assertRaises(m.Blocked):
            m.prepare_dropin(self.helper, self.state, self.path)
        self.assertEqual(self.created, [])


class HealthAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.policies = dict(global_=dict(auto_trade_enabled=False, kill_latched=False,
                                        revision=7, risk_per_trade=1000),
                             stock=dict(live_enabled=False, revision=14, slots=2))
        self.policies['global'] = self.policies.pop('global_')
        config = dict(self.policies['global'], rollout_enabled=True, broker_orders_enabled=True)
        self.payloads = {
            (8080, '/health'): dict(ok=True, engine_attached=True),
            (8081, '/health'): dict(ok=True, service='kadima-control-api'),
            (8081, '/api/kadima/config'): config,
            (8081, '/api/kadima/stock-fast'): dict(controls=deepcopy(self.policies['stock']),
                fast_master_permission=False, global_auto_requested=False, kill_latched=False,
                celestial_rollout=True, worker_alive=False, ts=NOW.isoformat()),
            (8081, '/api/kadima/top20'): dict(config=deepcopy(config), positions=[],
                ts=NOW.isoformat(), system=dict(reconciliation=dict(ok=False))),
            (8081, '/api/kadima/audit?limit=1'): dict(rows=[], total=0),
            (8081, '/api/kadima/prices?limit=1'): dict(rows=[], total=0, capture_enabled=True),
            (8081, '/api/kadima/learning?limit=1'): dict(rows=[], total=0, mode='ADVISORY',
                worker=dict(ok=False), worker_fresh=False),
        }
        self.calls = []

    def get(self, port, path):
        self.calls.append((port, path))
        self.assertIn((port, path), self.payloads, 'Only reviewed GET endpoints may be used')
        return deepcopy(self.payloads[port, path])

    def verify(self):
        return m.verify_health(self.get, self.policies, NOW)

    def test_before_market_worker_can_be_inactive_and_all_gets_are_reviewed(self):
        result = self.verify()
        self.assertEqual(set(self.calls), set(self.payloads))
        self.assertIs(result['learning_worker_fresh'], False)
        self.assertIs(result['runtime_broker_reconciliation_verified'], False)

    def test_clock_is_sampled_after_control_gets_so_new_snapshots_are_not_future(self):
        clock = [NOW]
        def get(port, path):
            clock[0] += timedelta(seconds=2)
            result = self.get(port, path)
            if path in ('/api/kadima/stock-fast', '/api/kadima/top20'):
                result['ts'] = clock[0].isoformat()
            return result
        def now():
            self.assertIn((8081, '/api/kadima/top20'), self.calls)
            self.assertIn((8081, '/api/kadima/stock-fast'), self.calls)
            return clock[0]
        result = m.verify_health(get, self.policies, now)
        self.assertIs(result['api_health_verified'], True)

    def test_global_or_stock_policy_drift_blocks_without_mutation(self):
        for port, path, container, name, value in (
                (8081, '/api/kadima/config', None, 'auto_trade_enabled', True),
                (8081, '/api/kadima/config', None, 'revision', 8),
                (8081, '/api/kadima/config', None, 'risk_per_trade', 2000),
                (8081, '/api/kadima/stock-fast', 'controls', 'revision', 15),
                (8081, '/api/kadima/stock-fast', 'controls', 'live_enabled', True),
                (8081, '/api/kadima/stock-fast', None, 'fast_master_permission', True),
                (8081, '/api/kadima/stock-fast', None, 'global_auto_requested', True),
                (8081, '/api/kadima/stock-fast', None, 'celestial_rollout', False)):
            original = deepcopy(self.payloads[port, path])
            target = self.payloads[port, path][container] if container else self.payloads[port, path]
            target[name] = value
            before = deepcopy(self.payloads)
            with self.subTest(path=path, name=name), self.assertRaises(m.Blocked):
                self.verify()
            self.assertEqual(self.payloads, before)
            self.payloads[port, path] = original

    def test_managed_position_missing_positions_or_stale_top20_blocks(self):
        key = (8081, '/api/kadima/top20')
        original = deepcopy(self.payloads[key])
        cases = [dict(original, positions=[dict(symbol='MANUAL_CARRYFORWARD', remaining_qty=130)]),
                 {name: value for name, value in original.items() if name != 'positions'},
                 dict(original, ts=(NOW-timedelta(seconds=31)).isoformat()),
                 dict(original, ts=(NOW+timedelta(seconds=1)).isoformat()),
                 dict(original, ts=NOW.replace(tzinfo=None).isoformat())]
        for case in cases:
            self.payloads[key] = case
            with self.subTest(case=case), self.assertRaises((m.Blocked, ValueError, TypeError)):
                self.verify()

    def test_wrong_health_disabled_capture_or_nonadvisory_learning_blocks(self):
        for key, name, value in (
                ((8080, '/health'), 'engine_attached', False),
                ((8080, '/health'), 'ok', False),
                ((8081, '/health'), 'service', 'other-service'),
                ((8081, '/api/kadima/config'), 'rollout_enabled', False),
                ((8081, '/api/kadima/config'), 'broker_orders_enabled', False),
                ((8081, '/api/kadima/prices?limit=1'), 'capture_enabled', False),
                ((8081, '/api/kadima/learning?limit=1'), 'mode', 'LIVE'),
                ((8081, '/api/kadima/audit?limit=1'), 'rows', None),
                ((8081, '/api/kadima/audit?limit=1'), 'total', True)):
            original = self.payloads[key][name]
            self.payloads[key][name] = value
            with self.subTest(key=key, name=name), self.assertRaises(m.Blocked):
                self.verify()
            self.payloads[key][name] = original

    def test_numeric_values_cannot_impersonate_explicit_boolean_health_flags(self):
        for key, name, value in (
                ((8080, '/health'), 'ok', 1),
                ((8080, '/health'), 'engine_attached', 1),
                ((8081, '/health'), 'ok', 1),
                ((8081, '/api/kadima/config'), 'rollout_enabled', 1),
                ((8081, '/api/kadima/config'), 'broker_orders_enabled', 1),
                ((8081, '/api/kadima/config'), 'auto_trade_enabled', 0),
                ((8081, '/api/kadima/stock-fast'), 'fast_master_permission', 0),
                ((8081, '/api/kadima/stock-fast'), 'global_auto_requested', 0),
                ((8081, '/api/kadima/prices?limit=1'), 'capture_enabled', 1)):
            original = self.payloads[key][name]
            self.payloads[key][name] = value
            with self.subTest(key=key, name=name), self.assertRaises(m.Blocked):
                self.verify()
            self.payloads[key][name] = original


class StartupTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state_dir = self.root/'state'
        self.dropin = self.root/'paused.conf'
        self.events = []
        self.active = False
        self.start_error = False
        self.stop_error = False
        self.reload_error = False
        self.statuses = []

        def root_parents(path):
            self.assertEqual(Path(path).resolve(), Path(path))

        def private_path(path, *, directory=False):
            path = Path(path)
            info = path.lstat()
            self.assertEqual(info.st_uid, os.getuid())
            self.assertFalse(info.st_mode & 0o077)
            self.assertFalse(path.is_symlink())
            self.assertTrue(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            if not directory:
                self.assertEqual(info.st_nlink, 1)
            return path

        def claim_attempt(path, now):
            self.events.append('claim')
            private_path(path, directory=True)
            target = Path(path)/'attempted.json'
            with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                   0o600), 'wb') as stream:
                stream.write(json.dumps(dict(started_at=now.isoformat())).encode())

        def record_status(path, value):
            private_path(path, directory=True)
            self.statuses.append(deepcopy(value))
            self.events.append('record')

        self.helper = SimpleNamespace(root_parents=root_parents,
            private_path=private_path, claim_attempt=claim_attempt,
            record_status=record_status, utc_now=lambda: NOW)
        self.admission = self.callback('admission', {})
        self.after_reload = self.callback('after_reload', {})
        self.prestart = self.callback('prestart', {})
        self.health = self.callback('health', {})
        self.guard = self.callback('guard', None)
        self.process = self.callback('process', dict(pid=1234))
        self.prepare_override = Mock(side_effect=self.install_override)

    def callback(self, name, result):
        def call(*args, **kwargs):
            self.events.append(name)
            return deepcopy(result)
        return Mock(side_effect=call)

    def install_override(self, *args, **kwargs):
        self.events.append('prepare_override')
        self.dropin.write_bytes(b'[Service]\nUnsetEnvironment=KADIMA_FORCE_START\n')
        return dict(path=str(self.dropin), installed=True)

    def observe(self, *args, **kwargs):
        self.events.append('observe')
        return dict(ActiveState='active' if self.active else 'inactive',
                    MainPID='1234' if self.active else '0')

    def invoke(self, command, **kwargs):
        self.events.append(tuple(command))
        self.assertEqual(command[0], '/usr/bin/systemctl')
        if command[1] == 'daemon-reload':
            self.assertEqual(command, ['/usr/bin/systemctl', 'daemon-reload'])
            return SimpleNamespace(returncode=1 if self.reload_error else 0)
        self.assertEqual(command[2:], ['kadima.service'], 'Only engine may be changed')
        if command[1] == 'start':
            self.active = True
            return SimpleNamespace(returncode=1 if self.start_error else 0)
        self.assertEqual(command[1], 'stop')
        if not self.stop_error:
            self.active = False
        return SimpleNamespace(returncode=1 if self.stop_error else 0)

    def run_start(self, *, apply=True, reuse_override=None):
        return m.start_transaction(apply=apply, helper=self.helper,
            state_dir=self.state_dir, admission=self.admission,
            prepare_override=self.prepare_override, after_reload=self.after_reload,
            prestart=self.prestart, observe=self.observe, invoke=self.invoke,
            health=self.health, guard=self.guard, process=self.process, reuse_override=reuse_override)

    def test_resume_preserves_original_attempt_and_override_without_reload(self):
        original = self.root/'original'
        original.mkdir(mode=0o700)
        previous = original/'attempted.json'
        previous.write_bytes(b'original-attempt')
        self.dropin.write_bytes(m.DROPIN_BYTES)
        self.state_dir = original/'resume-before-start'
        override = dict(path=str(self.dropin), original_present=False, retained=True)
        report = self.run_start(reuse_override=override)
        self.assertEqual(previous.read_bytes(), b'original-attempt')
        self.assertEqual(self.dropin.read_bytes(), m.DROPIN_BYTES)
        self.assertTrue((self.state_dir/'attempted.json').exists())
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'start', 'kadima.service')])
        self.assertTrue(report['resumed_before_start'])
        self.assertFalse(report['override_write_requested'])
        self.prepare_override.assert_not_called()
        with self.assertRaises(m.Blocked):
            self.run_start(reuse_override=override)

    def test_resume_failed_prestart_retains_override_without_service_action(self):
        self.dropin.write_bytes(m.DROPIN_BYTES)
        self.prestart.side_effect = m.Blocked('fresh_broker_readiness_required')
        with self.assertRaises(m.Blocked) as result:
            self.run_start(reuse_override=dict(path=str(self.dropin), original_present=False, retained=True))
        self.assertFalse(self.active)
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(self.dropin.read_bytes(), m.DROPIN_BYTES)
        self.assertTrue(result.exception.report['override_retained'])
        self.prepare_override.assert_not_called()

    def test_resume_health_failure_stops_only_engine_and_cannot_retry(self):
        self.dropin.write_bytes(m.DROPIN_BYTES)
        self.health.side_effect = m.Blocked('runtime_broker_reconciliation_failed')
        override = dict(path=str(self.dropin), original_present=False, retained=True)
        with self.assertRaises(m.Blocked):
            self.run_start(reuse_override=override)
        self.assertFalse(self.active)
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'start', 'kadima.service'),
                                                 ('/usr/bin/systemctl', 'stop', 'kadima.service')])
        with self.assertRaises(FileExistsError):
            self.run_start(reuse_override=override)
        self.assertEqual(self.dropin.read_bytes(), m.DROPIN_BYTES)

    def service_actions(self):
        return [event for event in self.events if isinstance(event, tuple)]

    def test_plan_checks_admission_without_claim_override_or_service_mutation(self):
        report = self.run_start(apply=False)
        self.admission.assert_called_once()
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.service_actions(), [])
        self.prepare_override.assert_not_called()
        self.health.assert_not_called()
        self.process.assert_not_called()

    def test_admission_failure_has_no_start_claim_or_override(self):
        self.admission.side_effect = RuntimeError('synthetic_admission_failure')
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.service_actions(), [])

    def test_broker_admission_failure_keeps_receipt_without_claiming_resume(self):
        self.dropin.write_bytes(m.DROPIN_BYTES)
        failure = pending_broker_failure()
        self.admission.side_effect = failure
        with self.assertRaises(m.Blocked) as result:
            self.run_start(reuse_override=dict(path=str(self.dropin), original_present=False, retained=True))
        self.assertEqual(result.exception.broker_readiness, failure.broker_readiness)
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(self.dropin.read_bytes(), m.DROPIN_BYTES)
        self.assertEqual(self.service_actions(), [])
        self.prepare_override.assert_not_called()

    def test_prestart_broker_failure_is_recorded_without_start_or_reload_on_resume(self):
        self.dropin.write_bytes(m.DROPIN_BYTES)
        failure = pending_broker_failure()
        self.prestart.side_effect = failure
        with self.assertRaises(m.Blocked) as result:
            self.run_start(reuse_override=dict(path=str(self.dropin), original_present=False, retained=True))
        self.assertTrue((self.state_dir/'attempted.json').is_file())
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(self.dropin.read_bytes(), m.DROPIN_BYTES)
        self.assertEqual(result.exception.report['broker_readiness'], failure.broker_readiness)
        self.assertEqual(self.statuses[-1]['broker_readiness'], failure.broker_readiness)
        self.assertFalse(result.exception.report['start_command_invoked'])
        self.assertEqual(result.exception.report['start_requested'], [])

    def test_success_orders_pause_override_before_engine_start_and_checks(self):
        report = self.run_start()
        self.assertTrue(self.active)
        self.assertTrue(self.dropin.is_file())
        self.assertTrue((self.state_dir/'attempted.json').is_file())
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'daemon-reload'),
                                                 ('/usr/bin/systemctl', 'start', 'kadima.service')])
        positions = {name: self.events.index(name) for name in
                     ('admission', 'claim', 'prepare_override', 'after_reload',
                      'prestart', 'process', 'health')}
        self.assertEqual(list(positions.values()), sorted(positions.values()))
        start = self.events.index(('/usr/bin/systemctl', 'start', 'kadima.service'))
        self.assertLess(positions['prestart'], start)
        self.assertLess(start, positions['process'])
        self.assertGreaterEqual(self.guard.call_count, 2)
        self.assertTrue(self.statuses)

    def test_existing_attempt_never_reinstalls_override_or_starts_service(self):
        self.state_dir.mkdir(mode=0o700)
        attempt = self.state_dir/'attempted.json'
        attempt.write_bytes(b'{"started_at":"earlier"}')
        attempt.chmod(0o600)
        with self.assertRaises((FileExistsError, RuntimeError)):
            self.run_start()
        self.prepare_override.assert_not_called()
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(attempt.read_bytes(), b'{"started_at":"earlier"}')

    def assert_only_engine_was_stopped(self):
        self.assertEqual(self.service_actions()[-1],
                         ('/usr/bin/systemctl', 'stop', 'kadima.service'))
        self.assertEqual(sum(event[1] == 'stop' for event in self.service_actions()), 1)
        self.assertTrue(self.dropin.exists(), 'Paused override must be retained after failure')

    def test_override_failure_never_starts_or_stops_engine(self):
        self.prepare_override.side_effect = RuntimeError('synthetic_override_failure')
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertEqual(self.service_actions(), [])
        self.assertTrue((self.state_dir/'attempted.json').exists())

    def test_reload_failure_retains_override_without_start_or_stop(self):
        self.reload_error = True
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertTrue(self.dropin.exists())
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'daemon-reload')])

    def test_effective_environment_failure_prevents_start_and_retains_override(self):
        self.after_reload.side_effect = RuntimeError('synthetic_effective_environment_failure')
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertTrue(self.dropin.exists())
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'daemon-reload')])
        self.prestart.assert_not_called()

    def test_last_broker_or_source_guard_failure_prevents_start(self):
        self.prestart.side_effect = RuntimeError('synthetic_pending_broker_order')
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertTrue(self.dropin.exists())
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'daemon-reload')])

    def test_engine_started_externally_during_preflight_is_never_stopped_by_cleanup(self):
        def external_start():
            self.active = True
        self.prestart.side_effect = external_start
        with self.assertRaises(m.Blocked) as failure:
            self.run_start()
        self.assertTrue(self.active)
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'daemon-reload')])
        self.assertEqual(failure.exception.report['start_requested'], [])
        self.assertEqual(failure.exception.report['stopped_after_failure'], [])

    def test_partial_start_command_failure_stops_only_engine(self):
        self.start_error = True
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertFalse(self.active)
        self.assert_only_engine_was_stopped()
        self.health.assert_not_called()

    def test_prestart_status_write_failure_does_not_start_or_stop_engine(self):
        record = self.helper.record_status
        def fail_before_start(path, report):
            if report['state'] == 'starting_paused_engine':
                raise OSError('synthetic_status_disk_failure')
            record(path, report)
        self.helper.record_status = fail_before_start
        with self.assertRaisesRegex(OSError, 'synthetic_status_disk_failure') as failure:
            self.run_start()
        self.assertFalse(self.active)
        self.assertEqual(self.service_actions(), [('/usr/bin/systemctl', 'daemon-reload')])
        self.assertTrue(self.dropin.is_file())
        self.assertIs(failure.exception.report['start_command_invoked'], False)
        self.assertEqual(failure.exception.report['stopped_after_failure'], [])
        self.assertIs(failure.exception.report['override_retained'], True)
        self.process.assert_not_called()
        self.health.assert_not_called()

    def test_process_identity_failure_stops_only_engine(self):
        self.process.side_effect = RuntimeError('synthetic_process_identity_failure')
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertFalse(self.active)
        self.assert_only_engine_was_stopped()
        self.health.assert_not_called()

    def test_health_failure_stops_only_engine(self):
        self.health.side_effect = RuntimeError('synthetic_unpaused_health')
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertFalse(self.active)
        self.assert_only_engine_was_stopped()

    def test_post_health_guard_failure_stops_only_engine(self):
        def guard():
            self.events.append('guard')
            if self.health.call_count:
                raise RuntimeError('synthetic_source_changed_after_health')
        self.guard.side_effect = guard
        with self.assertRaises(RuntimeError):
            self.run_start()
        self.assertFalse(self.active)
        self.assert_only_engine_was_stopped()

    def test_stop_failure_remains_truthful_and_retains_override(self):
        self.health.side_effect = RuntimeError('synthetic_health_failure')
        self.stop_error = True
        with self.assertRaises(RuntimeError) as failure:
            self.run_start()
        self.assertTrue(self.active)
        self.assert_only_engine_was_stopped()
        self.assertTrue(self.statuses)
        self.assertEqual(self.statuses[-1]['state'], 'blocked_paused_start')
        self.assertIs(self.statuses[-1]['engine_stopped_after_failure'], False)
        self.assertEqual(self.statuses[-1]['stopped_after_failure'], [])
        self.assertIs(self.statuses[-1]['override_retained'], True)
        self.assertEqual(failure.exception.report, self.statuses[-1])


class RepairProvenanceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('_paused_start_history_fixture',
            ROOT/'tests/test_stopped_history_repair.py')
        fixtures = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = fixtures
        spec.loader.exec_module(fixtures)
        self.case = fixtures.StoppedHistoryRepair()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.repair = fixtures.module
        self.broker = fixtures.broker_read
        self.state = self.case.root/'repair-state'
        self.state.mkdir(mode=0o700)
        first = self.case.receipt(started_at=(NOW-timedelta(seconds=6)).isoformat(),
                                  checked_at=(NOW-timedelta(seconds=5)).isoformat())
        second = self.case.receipt(started_at=(NOW-timedelta(seconds=3)).isoformat(),
                                   checked_at=(NOW-timedelta(seconds=2)).isoformat())
        self.case.broker.side_effect = [first, second]
        self.report = self.case.run_repair()
        self.report['observed_at'] = (NOW+timedelta(seconds=1)).isoformat()
        self.attempt = dict(started_at=NOW.isoformat())
        self.write_records()
        self.original_db = self.case.snapshot()

        def private(path, *, directory=False):
            path = Path(path)
            info = path.lstat()
            if (path.resolve() != path or path.is_symlink() or info.st_mode & 0o077 or
                    not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
                    (not directory and info.st_nlink != 1)):
                raise RuntimeError('synthetic_private_record_required')
            return path
        self.helper = SimpleNamespace(root_parents=lambda path: None, private_path=private,
            read_regular=lambda path, limit=4_000_000: Path(path).read_bytes())
        replacements = ((self.repair, 'STATE', self.state),
                        (self.repair, 'DB_IDENTITY', self.repair.identity(self.case.db.stat())),
                        (self.repair, 'ROWS_SHA256', self.case.expected['historical_rows_sha256']),
                        (m, 'BACKUP_ROOT', self.case.backups),
                        (m, 'utc_now', lambda: NOW+timedelta(seconds=2)))
        for module, name, value in replacements:
            replacement = patch.object(module, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def write_records(self):
        for name, value in (('status.json', self.report), ('attempted.json', self.attempt)):
            path = self.state/name
            path.write_text(json.dumps(value, sort_keys=True))
            path.chmod(0o600)

    def verify(self):
        return m.verify_repair(self.helper, self.repair, self.broker,
                               self.case.db, self.case.evidence)

    def test_exact_committed_archive_and_private_provenance_are_read_only(self):
        before = self.case.db.stat()
        broker_calls = self.case.broker.call_count
        policies, fingerprints = self.verify()
        after = self.case.db.stat()
        self.assertEqual(self.case.snapshot(), self.original_db)
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertEqual(policies['global'], dict(auto_trade_enabled=False, kill_latched=False))
        self.assertEqual(policies['stock'], dict(live_enabled=False, revision=14))
        self.assertEqual(set(fingerprints), {self.state/'status.json', self.state/'attempted.json'})
        self.assertEqual(self.case.broker.call_count, broker_calls, 'Historical validation is local only')

    def test_closed_position_drift_blocks_without_repairing_again(self):
        self.case.execute("UPDATE positions SET exit_price=121 WHERE signal_id=1")
        altered = self.case.snapshot()
        with self.assertRaises(RuntimeError):
            self.verify()
        self.assertEqual(self.case.snapshot(), altered)

    def test_archived_original_rows_drift_blocks_without_repairing_again(self):
        original = json.loads(self.case.rows('contract_note_reconciliations')[0]['original_position'])
        original['entry_price'] = 106
        self.case.execute('UPDATE contract_note_reconciliations SET original_position=?',
                          (json.dumps(original),))
        altered = self.case.snapshot()
        with self.assertRaises(RuntimeError):
            self.verify()
        self.assertEqual(self.case.snapshot(), altered)

    def test_incomplete_future_and_unsupported_repair_statuses_block(self):
        original = deepcopy(self.report)
        for name, value in (('state', 'blocked_after_commit'), ('ledger_repaired', False),
                            ('activation', True), ('original_rows_archived', False),
                            ('commit_attempted', False),
                            ('observed_at', (NOW+timedelta(minutes=2)).isoformat())):
            self.report = dict(original, **{name: value})
            self.write_records()
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.verify()
            self.assertEqual(self.case.snapshot(), self.original_db)

    def test_missing_private_backup_or_public_status_blocks(self):
        (self.state/'status.json').chmod(0o644)
        with self.assertRaises(RuntimeError):
            self.verify()
        (self.state/'status.json').chmod(0o600)
        Path(self.report['backup']).unlink()
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            self.verify()
        self.assertEqual(self.case.snapshot(), self.original_db)

    def test_pending_or_reordered_broker_provenance_blocks_without_live_read(self):
        original = deepcopy(self.report)
        for change in ('pending', 'reordered'):
            self.report = deepcopy(original)
            receipt = self.report['broker_receipts']['before_commit']
            if change == 'pending':
                receipt['readiness'] = False
            else:
                receipt['started_at'] = (NOW-timedelta(seconds=8)).isoformat()
            self.write_records()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.verify()
        self.assertEqual(self.case.broker.call_count, 2)
        self.assertEqual(self.case.snapshot(), self.original_db)


class RuntimeFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 3, 40, tzinfo=timezone.utc)
        self.started = self.now-timedelta(seconds=10)
        self.top = dict(system=dict(reconciliation=dict(ok=True, positions=[],
            ts=(self.now-timedelta(seconds=5)).isoformat())))
        self.learning = dict(worker_fresh=True, worker_at=(self.now-timedelta(seconds=2)).isoformat())

    def verify(self):
        return m.verify_runtime(self.top, self.learning, self.now, self.started)

    def test_runtime_window_requires_new_reconciliation_and_learner_heartbeat(self):
        result = self.verify()
        self.assertIs(result['runtime_broker_reconciliation_verified'], True)
        self.assertIs(result['runtime_workers_pending'], False)
        self.assertIs(result['manual_carryforward_external'], False)
        for payload, key in ((self.top['system']['reconciliation'], 'ts'), (self.learning, 'worker_at')):
            before = payload[key]
            payload[key] = (self.started-timedelta(seconds=1)).isoformat()
            with self.subTest(key=key), self.assertRaises(m.Warming):
                self.verify()
            payload[key] = before

    def test_only_explicit_checking_and_absent_initial_heartbeat_are_warming(self):
        self.top['system']['reconciliation'].update(ok=False, reason='checking_broker_books')
        with self.assertRaises(m.Warming):
            self.verify()
        self.top['system']['reconciliation'].update(reason='contract_master_unavailable')
        with self.assertRaises(m.Blocked) as exc:
            self.verify()
        self.assertNotIsInstance(exc.exception, m.Warming)
        self.top['system']['reconciliation'].update(ok=True)
        self.learning.update(worker_at=None, worker_fresh=False)
        with self.assertRaises(m.Warming):
            self.verify()

    def test_future_and_malformed_heartbeat_timestamps_fail_without_retry(self):
        for payload, key in ((self.top['system']['reconciliation'], 'ts'), (self.learning, 'worker_at')):
            before = payload[key]
            for value in ('invalid', self.now.replace(tzinfo=None).isoformat(),
                          (self.now+timedelta(seconds=1)).isoformat()):
                payload[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(m.Blocked) as exc:
                    self.verify()
                self.assertNotIsInstance(exc.exception, m.Warming)
            payload[key] = before

    def test_only_exact_manual_carryforward_remains_external(self):
        position = dict(exchange='NFO', token='73921', product='CARRYFORWARD', quantity=130,
            symbol='NIFTY29SEP2623250CE', source='MANUAL_CARRYFORWARD', managed=True,
            auto_managed=False, reason='carryforward_observed_not_managed', underlying='NIFTY')
        self.top['system']['reconciliation']['positions'] = [position]
        self.assertIs(self.verify()['manual_carryforward_external'], True)
        for key, changed in (('auto_managed', True), ('quantity', 65), ('quantity', 130.0),
                             ('source', 'MANUAL_ADOPTED'), ('product', 'INTRADAY')):
            before = position[key]
            position[key] = changed
            with self.subTest(key=key, changed=changed), self.assertRaises(m.Blocked) as exc:
                self.verify()
            self.assertNotIsInstance(exc.exception, m.Warming)
            position[key] = before

    def test_before_open_and_weekends_report_worker_verification_pending(self):
        for now in (self.now.replace(hour=3, minute=29), self.now-timedelta(days=1),
                    self.now.replace(hour=10, minute=11)):
            result = m.verify_runtime({}, {}, now, None)
            self.assertIs(result['runtime_workers_pending'], True)
            self.assertIs(result['runtime_broker_reconciliation_verified'], False)


class DatabaseOwnerTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, 'Requires the privileged CI ownership step')
    def test_real_read_only_wal_sidecars_belong_to_database_owner(self):
        with tempfile.TemporaryDirectory(prefix='kadima-owner-wal-', dir='/tmp') as name:
            directory = Path(name)
            db = directory/'owner.db'
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                connection.execute('CREATE TABLE owner_read(value TEXT)')
                connection.execute("INSERT INTO owner_read VALUES ('committed')")
                connection.commit()
            for suffix in ('-wal', '-shm'):
                self.assertFalse(Path(str(db)+suffix).exists())
            try:
                os.chown(directory, 1000, 1000)
                os.chown(db, 1000, 1000)
            except OSError as exc:
                if exc.errno in (errno.EPERM, errno.EINVAL):
                    self.skipTest('The local UID namespace cannot map the application owner')
                raise
            db.chmod(0o600)
            before = os.geteuid(), os.getegid()
            with m.database_owner(db), closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as connection:
                self.assertEqual((os.geteuid(), os.getegid()), (1000, 1000))
                self.assertEqual(connection.execute('SELECT value FROM owner_read').fetchall(), [('committed',)])
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(db)+suffix)
                    self.assertTrue(sidecar.is_file(), 'Exercise actual SQLite WAL sidecar creation')
                    info = sidecar.stat()
                    self.assertEqual((info.st_uid, info.st_gid), (1000, 1000))
            self.assertEqual((os.geteuid(), os.getegid()), before)
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(db)+suffix)
                if sidecar.exists():
                    info = sidecar.stat()
                    self.assertEqual((info.st_uid, info.st_gid), (1000, 1000))

    def test_owner_scope_restores_root_after_sql_failure(self):
        db = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000, st_gid=1000))
        effective = [0, 0]
        events = []
        def uid(value):
            effective[0] = value
            events.append(('uid', value))
        def gid(value):
            effective[1] = value
            events.append(('gid', value))
        with (patch.object(m.os, 'geteuid', side_effect=lambda: effective[0]),
              patch.object(m.os, 'getegid', side_effect=lambda: effective[1]),
              patch.object(m.os, 'seteuid', side_effect=uid),
              patch.object(m.os, 'setegid', side_effect=gid)):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'synthetic_sql_failure'):
                with m.database_owner(db):
                    self.assertEqual(effective, [1000, 1000])
                    raise sqlite3.OperationalError('synthetic_sql_failure')
        self.assertEqual(effective, [0, 0])
        self.assertEqual(events, [('gid', 1000), ('uid', 1000), ('uid', 0), ('gid', 0)])

    def test_unprivileged_other_owner_cannot_enter_sql_scope(self):
        db = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000, st_gid=1000))
        with (patch.object(m.os, 'geteuid', return_value=1001),
              patch.object(m.os, 'getegid', return_value=1001),
              patch.object(m.os, 'seteuid') as uid,
              patch.object(m.os, 'setegid') as gid):
            with self.assertRaises(m.Blocked):
                with m.database_owner(db):
                    self.fail('Unprivileged caller entered another owner database scope')
            uid.assert_not_called()
            gid.assert_not_called()

    def test_database_owner_read_does_not_change_existing_identity(self):
        db = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000, st_gid=1000))
        with (patch.object(m.os, 'geteuid', return_value=1000),
              patch.object(m.os, 'getegid', return_value=1000),
              patch.object(m.os, 'seteuid') as uid,
              patch.object(m.os, 'setegid') as gid):
            with m.database_owner(db):
                pass
            uid.assert_not_called()
            gid.assert_not_called()


if __name__ == '__main__':
    unittest.main()
