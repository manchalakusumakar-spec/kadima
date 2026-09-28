"""Restore reviewed NSE/BSE session automation after the paused recovery.

Run after hours. Restarts only the engine, with both intents paused and the
permit revoked. The existing scheduler subsequently arms through guarded APIs.
No broker order, ledger repair, strategy threshold or runtime source is changed.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

PACKAGE = Path(__file__).resolve().parents[1]
TARGET = Path('/home/ubuntu/kadima-py')
STATE = Path('/var/lib/kadima-autotrading-20260928')
ENGINE = 'kadima.service'
TIMER = 'kadima-session-auto.timer'
JOB = 'kadima-session-auto.service'
SCHEDULER = Path('/opt/kadima-session-auto')
PINS = {
    'momentum_recovery/start_paused_peripherals.py': '491146b5a903bcedfb1113577f2a8a7284354286',
    'session_auto/install.py': '9e98a404d3f60eb33320f8465b9378bdb9c564ff',
    'session_auto/runner.py': '86855711912ad1153c23d2880b1bb1b39bfd628a',
    'session_auto/gate.py': '5633eba4177f6ade457d6bdd32ec4e154f27e68a',
    'session_auto/policy.json': '5fe2677459f9639c621ea3fc57c8fe61bc96484c',
}
STOCK_API_BLOB = '57805126938637754e6fa22aa772fd132aa3a891'
ENABLED = ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_SESSION_AUTOTRADE',
           'KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_GAP_LIVE', 'KADIMA_MOVE_CAPTURE_LIVE')
HELD = ('KADIMA_FORCE_START', 'KADIMA_MCX_LIVE', 'KADIMA_CAS_LIVE')
LIVE_ENV = STATE/'trading.env'
LIVE_ENV_BYTES = ''.join(name+'=1\n' for name in ENABLED).encode()
DROPIN_BYTES = ('[Service]\nUnsetEnvironment='+' '.join(HELD)+'\nEnvironmentFile='+str(LIVE_ENV)+'\n').encode()


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def now():
    return datetime.now(timezone.utc)


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def load(relative):
    path = PACKAGE/relative
    require(path.resolve() == path and not path.is_symlink(), 'helper_path_changed')
    require(digest(path.read_bytes()) == PINS[relative], 'reviewed_helper_changed:'+relative)
    spec = importlib.util.spec_from_file_location('_session_activation_'+path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def after_hours(gate, policy, at):
    local = at.astimezone(gate.IST)
    # Leave at least 30 minutes before the next normal engine start. Rechecked
    # throughout the transaction; this is an installation boundary, not a gate bypass.
    gate.validate_policy(policy)
    require(policy['valid_from'] <= local.date().isoformat() <= policy['valid_through'],
            'calendar_update_required')
    require(local.hour >= 16 or local.hour < 8 or (local.hour == 8 and local.minute < 30),
            'after_hours_installation_required')
    require(gate.calendar_reason(policy, at) is not None, 'entry_window_must_be_closed')


def live_environment(env):
    require(all(env.get(name) == '1' for name in ENABLED), 'session_live_flags_not_loaded')
    require(all(name not in env for name in HELD), 'unreviewed_live_or_force_flag')


def expected_environment(before, after):
    require(after == dict(before, **dict.fromkeys(ENABLED, '1')), 'unexpected_environment_change')
    live_environment(after)


def activation_health(get, policies, current, *, fast):
    require(get(8080, '/health').get('engine_attached') is True and
            get(8080, '/health').get('ok') is True and
            get(8081, '/health').get('service') == 'kadima-control-api' and
            get(8081, '/health').get('ok') is True, 'engine_api_unhealthy')
    cfg = get(8081, '/api/kadima/config')
    expected = dict(policies['global'], rollout_enabled=True, broker_orders_enabled=True)
    require(cfg == expected and cfg.get('auto_trade_enabled') is False and
            cfg.get('kill_latched') is False, 'global_pause_or_policy_changed')
    stock = get(8081, '/api/kadima/stock-fast')
    top = get(8081, '/api/kadima/top20')
    require(stock.get('controls') == policies['stock'] and stock['controls'].get('live_enabled') is False and
            stock.get('fast_master_permission') is fast and stock.get('celestial_rollout') is True and
            stock.get('broker_order_permission') is True and stock.get('global_auto_requested') is False and
            stock.get('kill_latched') is False, 'stock_pause_or_permission_changed')
    require(top.get('positions') == [] and top.get('config') == cfg, 'managed_exposure_or_policy_changed')
    current = current() if callable(current) else current
    for value in (stock, top):
        try:
            at = datetime.fromisoformat(value['ts'])
            valid = at.tzinfo is not None and 0 <= (current-at).total_seconds() <= 30
        except (KeyError, TypeError, ValueError):
            valid = False
        require(valid, 'control_snapshot_stale')
    return dict(global_auto_requested=False, stock_live_requested=False,
                fast_master_permission=fast, api_health_verified=True,
                market_readiness_pending=True)


def replace_file(path, raw, helper, mode=0o644):
    helper.root_parents(path)
    require(path.resolve() == path and not path.is_symlink(), 'configuration_path_changed')
    fd, temporary = tempfile.mkstemp(prefix='.activation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno()); os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
        helper.sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def transaction(ops, *, apply):
    """No scheduler is enabled until restart, policies and all postchecks pass."""
    ops.guard(fast=False)
    ops.broker_check()
    ops.guard(fast=False)
    report = dict(action='restore_session_auto_trading', state='auto_trading_plan',
        scheduler_enabled=False, live_entries_enabled_now=False, runtime_source_changed=False,
        ledger_changed=False, broker_order_called=False, watchdog_held=True)
    if not apply:
        return report
    ops.claim(report)
    try:
        ops.backup()
        ops.revoke()
        ops.guard(fast=False)
        ops.stop_engine()
        ops.broker_check()
        ops.guard(fast=False, stopped=True)
        ops.install_flags()
        ops.start_engine()
        report['health'] = ops.wait_health()
        ops.guard(fast=True)
        ops.broker_check()
        ops.guard(fast=True)
        ops.clear_schedule_hold()
        # Permit stays revoked. Releasing the scheduler lock is explicit and
        # occurs only after all other maintenance guards are satisfied.
        ops.release_scheduler_lock()
        ops.enable_schedule()
        report['scheduler'] = ops.verify_schedule()
        ops.guard(fast=True, scheduled=True)
        report.update(state='auto_trading_deployed_waiting_for_session', scheduler_enabled=True,
            automatic_session_arming=True, calendar_through=ops.policy['valid_through'],
            broker_readiness=ops.last_broker, enabled_lanes=['celestial', 'stock_fast', 'gap', 'move_capture'],
            held_lanes=['mcx', 'cas'], market_readiness_pending=True)
        ops.record(report)
        return report
    except BaseException as exc:
        report.update(state='auto_trading_setup_incomplete', blocked=type(exc).__name__)
        report['recovery'] = ops.fail_closed()
        ops.record(report)
        exc.report = report
        raise


class Deployment:
    def __init__(self, args, locks):
        self.args = args
        self.p = load('momentum_recovery/start_paused_peripherals.py')
        self.start = self.p.load_start()
        self.repair = self.start.load_repair()
        self.ready = self.repair.load_readiness()
        self.helper, self.prep, self.source, self.note, self.cm, self.broker, self.hold = (
            self.ready.load_reviewed(name) for name in self.ready.PINS)
        self.safe_errors = tuple(m.Blocked for m in (self.p, self.start, self.repair, self.ready,
            self.helper, self.prep, self.source, self.note, self.cm, self.broker, self.hold) if hasattr(m, 'Blocked'))
        self.user = pwd.getpwnam('ubuntu')
        self.scheduler_lock = None
        for name in self.source.LOCKS:
            path = Path('/run/lock')/(name+'.lock')
            self.helper.root_parents(path)
            stream = locks.enter_context(os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w'))
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                    not info.st_mode & 0o022, 'private_lock_required')
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if name == 'kadima-session-auto-run':
                self.scheduler_lock = stream
        self.scheduler_install = load('session_auto/install.py')
        for name in ('runner.py', 'gate.py', 'policy.json'):
            require(digest((PACKAGE/'session_auto'/name).read_bytes()) == PINS['session_auto/'+name],
                    'scheduler_reference_changed')
        self.gate = load('session_auto/gate.py')
        self.policy = json.loads((PACKAGE/'session_auto/policy.json').read_bytes())
        after_hours(self.gate, self.policy, now())
        _, self.files = self.ready.verify_maintenance(self.helper, self.ready.AUDIT_STATE)
        _, prior = self.source.verify_prior_state(self.helper, self.ready.PRIOR_STATE)
        self.files.update(prior)
        self.contexts = {unit: self.context(unit) for unit in (ENGINE, *self.p.UNITS)}
        self.start.paused_environment(self.contexts[ENGINE]['env'])
        self.identities = {unit: self.process(unit, fast=False) for unit in self.contexts}
        self.verify_peripheral_receipt()
        self.original_dropin = self.helper.read_regular(self.start.DROPIN)
        require(self.original_dropin == self.start.DROPIN_BYTES, 'paused_override_changed')
        self.evidence_raw = self.ready.private_evidence(args.evidence, self.helper, self.user.pw_uid)
        require(hashlib.sha256(self.evidence_raw).hexdigest() == self.repair.EVIDENCE_SHA256,
                'exact_reviewed_evidence_required')
        self.evidence = self.note.evidence(json.loads(self.evidence_raw), now())
        self.note.account_matches(self.evidence, self.contexts[ENGINE]['env'])
        self.policies, records = self.start.verify_repair(self.helper, self.repair, self.broker,
            Path('/var/lib/kadima/kadima.db'), self.evidence)
        require(self.policies['global'].get('score_threshold') == 65 and
                self.policies['global'].get('adopt_external_positions') is True, 'reviewed_trading_policy_required')
        self.files.update(records)
        release = Path(tempfile.mkdtemp(prefix='kadima-auto-source-', dir='/opt'))/'release'
        self.helper.stage_release(self.helper.canonical(args.history), release, self.source.RELEASE)
        installer = self.source.load_helper('_auto_source_installer', release/'trade_audit_update/install.py', self.source.INSTALLER_BLOB)
        self.base = installer.base
        self.safe_errors += (self.base.Blocked,)
        self.files.update(self.ready.verify_sources(installer, release, TARGET,
            PACKAGE/'momentum_recovery/reviewed_dashboard', (self.user.pw_uid, self.user.pw_gid), self.source))
        self.files.update(self.p.verify_observer_sources(self.base, release, TARGET, admitted=self.files))
        stock_api = TARGET/'kadima/stock_fast_api.py'
        stock_api_raw = self.base.read_file(stock_api)
        require(digest(stock_api_raw) == STOCK_API_BLOB and
                stock_api_raw == self.base.read_file(release/'kadima/stock_fast_api.py'),
                'stock_control_api_source_changed')
        self.files[stock_api] = stock_api_raw
        require(digest(self.base.read_file(TARGET/'kadima/config.py')) == self.ready.CONFIG_BLOB,
                'installed_configuration_changed')
        self.scheduler_files = {
            SCHEDULER/'runner.py': (PACKAGE/'session_auto/runner.py').read_bytes(),
            SCHEDULER/'gate.py': (PACKAGE/'session_auto/gate.py').read_bytes(),
            self.gate.POLICY: (PACKAGE/'session_auto/policy.json').read_bytes(),
            TARGET/'kadima/market_session_gate.py': (PACKAGE/'session_auto/gate.py').read_bytes(),
            Path('/etc/systemd/system')/JOB: self.scheduler_install.service_text().encode(),
            Path('/etc/systemd/system')/TIMER: self.scheduler_install.timer_text().encode(),
            Path('/etc/kadima/session-auto.env'): self.scheduler_install.ENV_CONTENT.encode(),
        }
        for path, expected in self.scheduler_files.items():
            item = self.cm.read_regular(path)
            require(item.raw == expected and item.metadata[2] == (self.user.pw_uid if path.is_relative_to(TARGET) else 0) and not item.metadata[4] & 0o022,
                    'installed_scheduler_changed:'+str(path))
            # Runtime module ownership remains ubuntu; its bytes are already
            # admitted by the existing composed-source verifier above.
        self.files.update(self.scheduler_files)
        for unit in (TIMER, JOB):
            value = self.helper.observe_unit(unit)
            require(value.get('LoadState') == 'loaded' and value.get('DropInPaths') == '' and
                    value.get('FragmentPath') == '/etc/systemd/system/'+unit, 'scheduler_unit_layering_changed')
            raw = self.cm.invoke(['/usr/bin/systemctl', 'show', unit, '--value', '-p', 'NeedDaemonReload']).strip()
            require(raw == 'no', 'scheduler_daemon_reload_pending')
        for directory, mode, public in ((SCHEDULER, 0o755, False), (self.gate.POLICY.parent, 0o755, True),
                (self.gate.LEASE.parent, 0o755, True), (Path('/var/lib/kadima-session-auto'), 0o700, False)):
            self.scheduler_install.ensure_root_directory(directory, mode, public=public, create=False)
            require(directory.is_dir(), 'installed_scheduler_directory_missing')
        token_raw = self.base.read_file(self.prep.TOKEN_FILE)
        self.token = json.loads(token_raw)['api_token']
        config_path = Path('/etc/kadima-dashboard/config.json')
        config = self.cm.read_regular(config_path)
        require(config.metadata[2] == 0 and not config.metadata[4] & 0o022 and
                json.loads(config.raw).get('api_token') == self.token and isinstance(self.token, str) and self.token,
                'dashboard_api_binding_changed')
        self.files.update({self.prep.TOKEN_FILE: token_raw, config_path: config.raw})
        self.schedule_state = Path('/var/lib/kadima-session-auto/state.json')
        self.disabled = Path('/var/lib/kadima-session-auto/disabled')
        self.mutable_schedule = {}
        for path in (self.schedule_state, self.disabled, self.gate.LEASE):
            self.helper.root_parents(path)
            item = self.cm.read_regular(path, missing=True)
            if item:
                require(item.metadata[2] == 0 and not item.metadata[4] & 0o022, 'scheduler_state_owner_changed')
            self.mutable_schedule[path] = item
        self.flags_written = False
        require(not LIVE_ENV.exists(), 'activation_environment_already_exists')
        self.engine_stopped = False
        self.last_broker = None

    def verify_peripheral_receipt(self):
        path = self.p.STATE/'status.json'
        self.helper.root_parents(path)
        raw = self.helper.read_regular(self.helper.private_path(path))
        value = json.loads(raw)
        require(value.get('state') == 'paused_peripherals_running' and value.get('activation') is False and
                value.get('start_requested') == list(self.p.UNITS) and value.get('stopped_after_failure') == [] and
                value.get('cleanup_failures') == [], 'successful_paused_peripheral_start_required')
        require(value.get('processes') == {u: self.identities[u] for u in self.p.UNITS} and
                value.get('engine_validation', {}).get('process') == self.identities[ENGINE],
                'process_changed_since_paused_rollout')
        self.files[path] = raw

    def context(self, unit):
        return self.p.inspect_unit(unit, self.cm, self.user, engine=unit == ENGINE)

    def process(self, unit, *, fast):
        guard = (live_environment if fast else self.start.paused_environment) if unit == ENGINE else None
        return self.p.process_identity(self.contexts[unit], self.helper.observe_unit, self.user, environment_guard=guard)

    def get(self, port, path):
        if (port, path) in self.start.ROUTES:
            return self.start.local_get(port, path, self.token)
        require((port, path) in self.p.EXTRA_ROUTES, 'unreviewed_health_route')
        req = urllib.request.Request('http://127.0.0.1:'+str(port)+path,
            headers={'Authorization': 'Bearer '+self.token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), self.start.NoRedirect())
        with opener.open(req, timeout=3) as response:
            raw = response.read(2_000_001)
        require(len(raw) <= 2_000_000, 'health_response_oversized')
        return json.loads(raw)

    def guard(self, *, fast, stopped=False, scheduled=False):
        after_hours(self.gate, self.policy, now())
        if not scheduled:
            self.prep.verify_timer_hold()
        else:
            for unit in ('kadima-deploy-20260928.service', 'kadima-deploy-20260928.timer'):
                value = self.helper.observe_unit(unit)
                require(value.get('ActiveState') == 'inactive' and
                        (value.get('LoadState') == 'not-found' or
                         value.get('UnitFileState') in ('disabled', 'static', 'masked', 'masked-runtime')),
                        'competing_deployment_changed')
        self.hold.verify_held_cron(self.helper)
        require(self.p.inactive(self.helper.observe_unit('kadima-eod-watchdog.service')), 'watchdog_must_stay_held')
        triggers = self.ready.watchdog_triggers(self.helper)
        require(not triggers['references'] and not triggers['running_pids'] and not triggers['inspection_errors'] and
                all(x.get('ActiveState') == 'inactive' for x in triggers['timers'].values()), 'watchdog_trigger_changed')
        self.base.check_fingerprints(self.files)
        require(self.ready.private_evidence(self.args.evidence, self.helper, self.user.pw_uid) == self.evidence_raw,
                'historical_evidence_changed')
        for unit, previous in self.contexts.items():
            require(self.context(unit)['stable'] == previous['stable'], 'service_context_changed:'+unit)
            if stopped and unit == ENGINE:
                require(self.p.inactive(self.helper.observe_unit(unit)), 'engine_not_stopped')
            else:
                require(self.process(unit, fast=fast) == self.identities[unit], 'service_process_changed:'+unit)
        policies, _ = self.start.verify_repair(self.helper, self.repair, self.broker,
            Path('/var/lib/kadima/kadima.db'), self.evidence)
        require(policies == self.policies, 'persisted_policies_changed')
        expected = DROPIN_BYTES if self.flags_written else self.original_dropin
        require(self.helper.read_regular(self.start.DROPIN) == expected, 'activation_override_changed')
        if not stopped:
            activation_health(self.get, self.policies, now, fast=fast)
            self.p.snapshot_health(self.get, now()-timedelta(seconds=180), now)

    def broker_check(self):
        context = self.contexts[ENGINE]
        self.last_broker = self.start.require_broker_readiness(
            self.broker.read_broker(context['python'], context['env'], self.user, TARGET, self.evidence),
            self.broker.validate_receipt, now())

    def claim(self, report):
        self.helper.root_parents(STATE)
        STATE.mkdir(mode=0o700, exist_ok=True)
        self.helper.private_path(STATE, directory=True)
        self.helper.claim_attempt(STATE, now())
        self.record(report)

    def record(self, report):
        self.helper.record_status(STATE, report)

    def backup(self):
        self.helper.create_private(STATE/'paused-override.original', self.original_dropin)
        for path, item in self.mutable_schedule.items():
            require(self.cm.read_regular(path, missing=True) == item, 'scheduler_state_changed')
            if item:
                self.helper.create_private(STATE/(path.name+'.original'), item.raw)

    def revoke(self):
        replace_file(self.gate.LEASE, b'{"allowed":false,"reason":"activation_installation"}\n', self.helper)

    def command(self, *args):
        result = subprocess.run(['/usr/bin/systemctl', *args], capture_output=True, timeout=45)
        require(result.returncode == 0, 'systemctl_failed:'+args[0])

    def stop_engine(self):
        self.command('stop', ENGINE)
        self.engine_stopped = True
        require(self.p.inactive(self.helper.observe_unit(ENGINE)), 'engine_stop_unverified')

    def install_flags(self):
        require(self.helper.read_regular(self.start.DROPIN) == self.original_dropin, 'pause_override_changed')
        old_context = self.contexts[ENGINE]
        self.helper.create_private(LIVE_ENV, LIVE_ENV_BYTES)
        self.files[LIVE_ENV] = LIVE_ENV_BYTES
        replace_file(self.start.DROPIN, DROPIN_BYTES, self.helper)
        self.flags_written = True
        self.command('daemon-reload')
        current = self.context(ENGINE)
        expected_environment(old_context['env'], current['env'])
        require(current['argv'] == old_context['argv'] and current['cwd'] == old_context['cwd'] and
                current['stable'][4:] == old_context['stable'][4:], 'engine_launch_or_dependencies_changed')
        for path, old in old_context['fingerprints'].items():
            if path != self.start.DROPIN:
                require(current['fingerprints'].get(path) == old, 'engine_configuration_changed')
        require(set(current['fingerprints']) == set(old_context['fingerprints']) | {LIVE_ENV}, 'engine_configuration_layers_changed')
        self.contexts[ENGINE] = current
        self.guard(fast=True, stopped=True)

    def start_engine(self):
        self.broker.validate_receipt(self.last_broker, now())
        self.command('start', ENGINE)
        self.engine_stopped = False
        self.identities[ENGINE] = self.process(ENGINE, fast=True)

    def wait_health(self):
        deadline = time.monotonic()+90
        while True:
            try:
                result = activation_health(self.get, self.policies, now, fast=True)
                self.p.snapshot_health(self.get, now()-timedelta(seconds=180), now)
                return result
            except (OSError, urllib.error.URLError, self.p.Blocked) as exc:
                if isinstance(exc, self.p.Blocked) and 'snapshot_' not in str(exc):
                    raise
                require(time.monotonic() < deadline, 'engine_health_start_timeout')
                time.sleep(0.5)

    def clear_schedule_hold(self):
        for path in (self.disabled, self.schedule_state):
            require(self.cm.read_regular(path, missing=True) == self.mutable_schedule[path], 'schedule_hold_changed')
            path.unlink(missing_ok=True)
        self.helper.sync_directory(self.schedule_state.parent)

    def release_scheduler_lock(self):
        fcntl.flock(self.scheduler_lock, fcntl.LOCK_UN)

    def enable_schedule(self):
        self.broker.validate_receipt(self.last_broker, now())
        after_hours(self.gate, self.policy, now())
        self.command('enable', '--now', TIMER)
        self.command('start', JOB)

    def verify_schedule(self):
        value = self.helper.observe_unit(TIMER)
        require(value.get('ActiveState') == 'active' and value.get('UnitFileState') == 'enabled',
                'session_timer_not_enabled_and_active')
        status = self.gate.read_json(Path('/var/lib/kadima-session-auto/status.json'))
        at = datetime.fromisoformat(status['observed_at'])
        require(at.tzinfo is not None and 0 <= (now()-at).total_seconds() <= 30 and
                status.get('status') == self.gate.calendar_reason(self.policy, now()) and status.get('scheduled') is True,
                'fresh_waiting_scheduler_status_required')
        require(self.gate.read_json(self.gate.LEASE).get('allowed') is False and not self.disabled.exists(),
                'after_hours_permit_must_be_revoked')
        return dict(timer_active=True, timer_enabled=True, **status)

    def fail_closed(self):
        recovery = {}
        for label, action in (('timer_disabled', lambda: self.command('disable', '--now', TIMER)),
                              ('scheduler_stopped', lambda: self.command('stop', JOB)),
                              ('permit_revoked', self.revoke)):
            try:
                action(); recovery[label] = True
            except Exception:
                recovery[label] = False
        # Do not stop position protection or attempt a blind second restart.
        # The installed entry gate denies orders with the revoked permit.
        try:
            engine_state = self.helper.observe_unit(ENGINE).get('ActiveState', 'unknown')
        except Exception:
            engine_state = 'unknown'
        recovery.update(engine_state=engine_state, flags_retained=self.flags_written,
                        manual_recovery_required=True)
        return recovery


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    os.umask(0o077)
    sys.dont_write_bytecode = True
    ops = None
    try:
        with ExitStack() as locks:
            ops = Deployment.__new__(Deployment)
            ops.__init__(args, locks)
            print('STATUS: '+str(STATE/'status.json'), flush=True)
            report = transaction(ops, apply=args.apply)
    except Exception as exc:
        safe = (Blocked,) + getattr(ops, 'safe_errors', ())
        report = getattr(exc, 'report', dict(state='blocked', scheduler_enabled=False, live_entries_enabled_now=False))
        report['blocked'] = str(exc) if isinstance(exc, safe) else type(exc).__name__
        if hasattr(exc, 'broker_readiness'):
            report['broker_readiness'] = exc.broker_readiness
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    print('AUTO TRADING DEPLOYED: scheduler enabled; waiting for next eligible session and fresh readiness.'
          if args.apply else 'Plan only; no service or trading configuration changed.')


if __name__ == '__main__':
    main()
