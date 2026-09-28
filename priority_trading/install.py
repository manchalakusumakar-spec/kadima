"""Install the composed MCX/CAS/owned-watchdog release after ordinary activation.

Uses the existing source/context/broker admission helpers unchanged. Requires
paused, locally flat KADIMA state, no pending broker order, and either a flat
broker book or the previously authorized exact external manual holding.
No ledger repair, kill reset, manual holding adoption or test order is performed.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
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
ROOT = Path('/home/ubuntu/kadima-py')
STATE = Path('/var/lib/kadima-priority-trading-v1')
UNIT = 'kadima-owned-watchdog.service'
TIMER = 'kadima-owned-watchdog.timer'
OWNER = Path('/var/lib/kadima-execution-owner')
ACTIVATION_BLOB = '3b13cc0db6062f6de5607e57b825349e0ef83710'


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def now():
    return datetime.now(timezone.utc)


def blob(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def load_activation():
    path = PACKAGE/'momentum_recovery/enable_session_trading.py'
    require(blob(path.read_bytes()) == ACTIVATION_BLOB, 'activation_helper_changed')
    spec = importlib.util.spec_from_file_location('_priority_activation', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def environment_bytes(env):
    """systemd EnvironmentFile double-quoted values; never shell expansion."""
    import re
    rows = []
    for key, value in sorted(env.items()):
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) is not None and
                isinstance(value, str) and not any(c in value for c in '\x00\r\n'), 'environment_value_unsupported')
        rows.append(key+'="'+value.replace('\\', '\\\\').replace('"', '\\"')+'"\n')
    return ''.join(rows).encode()


def watchdog_service(python):
    require(str(python) == '/home/ubuntu/kadima-py/venv/bin/python3', 'watchdog_interpreter_unreviewed')
    return f'''[Unit]
Description=KADIMA owned-position exit watchdog
After=network-online.target
[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/kadima-py
EnvironmentFile={STATE}/watchdog.env
ExecStart={python} -B -m kadima.eod_watchdog
TimeoutStartSec=55
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
'''.encode()


def watchdog_timer():
    return b'''[Unit]
Description=Check KADIMA owned exposure at broker-aware exit times
[Timer]
OnCalendar=Mon..Fri *-*-* 15:18..39:00 Asia/Kolkata
OnCalendar=Mon..Fri *-*-* 22:40..59:00 Asia/Kolkata
OnCalendar=Mon..Fri *-*-* 23:00..25:00 Asia/Kolkata
AccuracySec=1s
Persistent=true
Unit=kadima-owned-watchdog.service
[Install]
WantedBy=timers.target
'''


def transaction(ops, apply):
    ops.guard()
    ops.broker_check()
    report = dict(state='priority_trading_plan', changed_files=len(ops.changes),
                  manual_holding_external=True, ledger_repaired=False,
                  scheduler_enabled=False, watchdog_enabled=False,
                  mcx_capital_inr=ops.args.mcx_capital, live_execution_verified=False)
    if not apply:
        return report
    ops.claim(report)
    try:
        ops.backup()
        ops.hold_scheduler()
        ops.guard()
        ops.stop_engine()
        ops.broker_check()
        ops.guard(stopped=True)
        ops.install()
        ops.guard(stopped=True, installed=True)
        ops.start_engine()
        report['health'] = ops.wait_health()
        ops.guard(installed=True)
        ops.broker_check()
        ops.prepare_schedule()
        ops.release_scheduler_lock()
        ops.start_schedules()
        report['schedule'] = ops.verify_schedules()
        report.update(state='priority_trading_installed_scheduled', scheduler_enabled=True,
                      watchdog_enabled=True, enabled_lanes=['mcx', 'cas'],
                      market_readiness_required=True, fills_verified=False,
                      ordinary_entries_stop='15:15 IST', ordinary_exit_due='15:18 IST',
                      mcx_entries_stop='22:35 IST', mcx_exit_due='22:40 IST',
                      calendar_through='2026-12-31')
        ops.record(report)
        return report
    except BaseException as exc:
        report.update(state='priority_trading_incomplete', error_type=type(exc).__name__,
                      recovery=ops.fail_closed())
        ops.record(report)
        exc.report = report
        raise


class Deployment:
    def __init__(self, args, locks):
        self.args = args
        require(type(args.mcx_capital) is int and 0 < args.mcx_capital <= 15000, 'mcx_capital_must_be_1_to_15000')
        self.old = load_activation()
        self.p = self.old.load('momentum_recovery/start_paused_peripherals.py')
        self.start = self.p.load_start()
        self.repair = self.start.load_repair()
        self.ready = self.repair.load_readiness()
        self.helper, self.prep, self.source, self.note, self.cm, self.broker, self.hold = (
            self.ready.load_reviewed(name) for name in self.ready.PINS)
        self.safe_errors = tuple(m.Blocked for m in (self.old, self.p, self.start, self.repair,
            self.ready, self.helper, self.prep, self.source, self.note, self.cm, self.broker, self.hold)
            if hasattr(m, 'Blocked'))
        self.user = pwd.getpwnam('ubuntu')
        for name in self.source.LOCKS:
            lock = self.old.maintenance_lock(locks, self.helper, Path('/run/lock')/(name+'.lock'))
            if name == 'kadima-session-auto-run': self.scheduler_lock = lock
        self.gate = self.old.load('session_auto/gate.py')
        self.policy = json.loads((PACKAGE/'session_auto/policy.json').read_bytes())
        self.old.after_hours(self.gate, self.policy, now())
        require(not STATE.exists(), 'priority_attempt_already_recorded_review_status')
        self.files = {}
        for path, expected in ((self.old.STATE/'status.json', None),
                               (self.old.LIVE_ENV, self.old.LIVE_ENV_BYTES),
                               (self.start.DROPIN, self.old.DROPIN_BYTES)):
            item = self.cm.read_regular(path)
            require(item.metadata[2] == 0 and not item.metadata[4] & 0o022, 'activation_file_metadata_changed')
            raw = item.raw
            if expected is not None: require(raw == expected, 'ordinary_activation_file_changed')
            else:
                receipt = json.loads(raw)
                require(receipt.get('state') == 'auto_trading_deployed_waiting_for_session' and
                        receipt.get('scheduler_enabled') is True, 'successful_ordinary_activation_required')
            self.files[path] = raw
        self.contexts = {u: self.context(u) for u in (self.old.ENGINE, *self.p.UNITS)}
        self.old.live_environment(self.contexts[self.old.ENGINE]['env'])
        self.identities = {u: self.process(u) for u in self.contexts}
        self.evidence_raw = self.ready.private_evidence(args.evidence, self.helper, self.user.pw_uid)
        require(hashlib.sha256(self.evidence_raw).hexdigest() == self.repair.EVIDENCE_SHA256,
                'reviewed_historical_evidence_required')
        self.evidence = self.note.evidence(json.loads(self.evidence_raw), now())
        self.note.account_matches(self.evidence, self.contexts[self.old.ENGINE]['env'])
        self.policies, records = self.start.verify_repair(self.helper, self.repair, self.broker,
            Path('/var/lib/kadima/kadima.db'), self.evidence)
        self.files.update(records)
        release = Path(tempfile.mkdtemp(prefix='kadima-priority-reference-', dir='/opt'))/'release'
        self.helper.stage_release(self.helper.canonical(args.history), release, self.source.RELEASE)
        installer = self.source.load_helper('_priority_reference_installer', release/'trade_audit_update/install.py', self.source.INSTALLER_BLOB)
        self.base = installer.base
        self.safe_errors += (self.base.Blocked,)
        self.files.update(self.ready.verify_sources(installer, release, ROOT,
            PACKAGE/'momentum_recovery/reviewed_dashboard', (self.user.pw_uid, self.user.pw_gid), self.source))
        self.files.update(self.p.verify_observer_sources(self.base, release, ROOT, admitted=self.files))
        require(blob(self.base.read_file(ROOT/'kadima/config.py')) == self.ready.CONFIG_BLOB,
                'runtime_configuration_changed')
        self.scheduler_install = self.old.load('session_auto/install.py')
        for relative in ('runner.py', 'gate.py'):
            path = self.old.SCHEDULER/relative
            raw = self.cm.read_regular(path)
            require(raw.raw == (PACKAGE/'session_auto'/relative).read_bytes() and
                    raw.metadata[2] == 0 and not raw.metadata[4] & 0o022, 'installed_scheduler_changed')
            self.files[path] = raw.raw
        require(self.gate.read_json(self.gate.POLICY) == self.policy, 'installed_calendar_changed')
        for unit, raw in ((self.old.JOB, self.scheduler_install.service_text().encode()),
                          (self.old.TIMER, self.scheduler_install.timer_text().encode())):
            path = Path('/etc/systemd/system')/unit
            require(self.helper.read_regular(path) == raw and
                    self.helper.observe_unit(unit).get('DropInPaths') == '', 'scheduler_service_changed')
            self.files[path] = raw
        self.files[self.gate.POLICY] = self.helper.read_regular(self.gate.POLICY)
        self.token = json.loads(self.helper.read_regular(self.prep.TOKEN_FILE))['api_token']
        require(self.contexts[self.old.ENGINE]['env'].get('KADIMA_API_TOKEN') == self.token,
                'engine_auth_binding_changed')
        self.changes = []
        manifest = json.loads((PACKAGE/'priority_trading/manifest.json').read_bytes())
        for relative, pin in manifest['runtime'].items():
            incoming = (PACKAGE/'priority_trading/runtime'/relative).read_bytes()
            require(blob(incoming) == pin['new'], 'priority_source_hash_changed:'+relative)
            original = self.base.read_file(ROOT/relative, missing=True)
            require((blob(original) if original is not None else None) == pin['old'],
                    'priority_source_base_changed:'+relative)
            compile(incoming, relative, 'exec')
            item = self.base.change_for(ROOT/relative, incoming, (self.user.pw_uid, self.user.pw_gid))
            require(item.uid == self.user.pw_uid and not item.mode & 0o022, 'runtime_source_owner_or_mode_changed')
            self.changes.append(item)
            self.files[item.path] = original
        for relative, digest in manifest['scheduler'].items():
            incoming = (PACKAGE/'priority_trading/scheduler'/relative).read_bytes()
            require(blob(incoming) == digest, 'priority_scheduler_hash_changed')
            item = self.base.change_for(self.old.SCHEDULER/relative, incoming, (0, 0))
            require(item.uid == 0 and not item.mode & 0o022 and
                    (relative in ('runner.py', 'gate.py') or item.original is None),
                    'scheduler_extension_unreviewed')
            self.changes.append(item)
        self.original_context = self.contexts[self.old.ENGINE]
        self.env = dict(self.original_context['env'], KADIMA_MCX_LIVE='1', KADIMA_CAS_LIVE='1',
                        KADIMA_PRIORITY_TRADING='1', KADIMA_MCX_CAPITAL_INR=str(args.mcx_capital))
        self.new_env = self.old.LIVE_ENV_BYTES + environment_bytes({key: self.env[key] for key in
            ('KADIMA_MCX_LIVE', 'KADIMA_CAS_LIVE', 'KADIMA_PRIORITY_TRADING', 'KADIMA_MCX_CAPITAL_INR')})
        self.dropin = ('[Service]\nUnsetEnvironment=KADIMA_FORCE_START\nEnvironmentFile='+str(self.old.LIVE_ENV)+'\n').encode()
        for unit in (UNIT, TIMER):
            require(self.helper.observe_unit(unit).get('LoadState') == 'not-found' and
                    not (Path('/etc/systemd/system')/unit).exists(), 'new_watchdog_unit_already_present')
        require(not OWNER.exists(), 'execution_owner_directory_already_present')
        self.schedule_state = Path('/var/lib/kadima-session-auto/state.json')
        self.disabled = Path('/var/lib/kadima-session-auto/disabled')
        require(not self.disabled.exists(), 'scheduler_operator_disabled')
        self.last_broker = None

    def context(self, unit):
        return self.p.inspect_unit(unit, self.cm, self.user, engine=unit == self.old.ENGINE)

    def process(self, unit):
        return self.p.process_identity(self.contexts[unit], self.helper.observe_unit, self.user)

    def get(self, port, path):
        require((port, path) in self.start.ROUTES or (port, path) in self.p.EXTRA_ROUTES or
                (port, path) == (8081, '/api/kadima/priority/status'), 'health_route_unreviewed')
        req = urllib.request.Request('http://127.0.0.1:'+str(port)+path,
                                     headers={'Authorization': 'Bearer '+self.token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), self.start.NoRedirect())
        with opener.open(req, timeout=4) as response: raw = response.read(2_000_001)
        require(len(raw) <= 2_000_000, 'oversized_health_response')
        return json.loads(raw)

    def guard(self, stopped=False, installed=False):
        self.old.after_hours(self.gate, self.policy, now())
        self.base.check_fingerprints(self.files)
        self.hold.verify_held_cron(self.helper)
        require(self.p.inactive(self.helper.observe_unit('kadima-eod-watchdog.service')), 'legacy_watchdog_running')
        for unit, previous in self.contexts.items():
            require(self.context(unit)['stable'] == previous['stable'], 'service_context_changed:'+unit)
            if unit == self.old.ENGINE and stopped:
                require(self.p.inactive(self.helper.observe_unit(unit)), 'engine_not_stopped')
            else:
                require(self.process(unit) == self.identities[unit], 'service_process_changed:'+unit)
        policies, _ = self.start.verify_repair(self.helper, self.repair, self.broker,
            Path('/var/lib/kadima/kadima.db'), self.evidence)
        require(policies == self.policies, 'persisted_trading_policy_changed')
        # Checks local exposure without importing runtime code into root Python.
        with self.start.database_owner(Path('/var/lib/kadima/kadima.db')):
            self.base.local_flat(Path('/var/lib/kadima/kadima.db'))
        if not stopped:
            self.old.activation_health(self.get, self.policies, now, fast=True)

    def broker_check(self):
        c = self.contexts[self.old.ENGINE]
        self.last_broker = self.start.require_broker_readiness(
            self.broker.read_broker(c['python'], c['env'], self.user, ROOT, self.evidence),
            self.broker.validate_receipt, now())

    def command(self, *args):
        result = subprocess.run(['/usr/bin/systemctl', *args], capture_output=True, timeout=60)
        require(result.returncode == 0, 'systemctl_failed:'+args[0])

    def claim(self, report):
        self.helper.root_parents(STATE)
        STATE.mkdir(mode=0o700)
        self.helper.claim_attempt(STATE, now())
        self.record(report)

    def record(self, report):
        self.helper.record_status(STATE, report)

    def backup(self):
        rows = []
        for i, item in enumerate(self.changes):
            item.unchanged()
            if item.original is not None: self.helper.create_private(STATE/('source-'+str(i)), item.original)
            rows.append(dict(path=str(item.path), old=blob(item.original) if item.original is not None else None,
                             new=blob(item.replacement), uid=item.uid, gid=item.gid, mode=item.mode))
        self.helper.create_private(STATE/'files.json', json.dumps(rows, indent=2).encode())
        for name, path in (('override.original', self.start.DROPIN), ('trading-env.original', self.old.LIVE_ENV),
                           ('schedule.original', self.schedule_state)):
            raw = self.base.read_file(path, missing=True)
            if raw is not None: self.helper.create_private(STATE/name, raw)

    def revoke(self):
        self.old.replace_file(self.gate.LEASE, b'{"allowed":false,"reason":"priority_installation"}\n', self.helper)

    def hold_scheduler(self):
        self.command('disable', '--now', self.old.TIMER)
        self.command('stop', self.old.JOB)
        self.revoke()

    def stop_engine(self):
        self.command('stop', self.old.ENGINE)
        require(self.p.inactive(self.helper.observe_unit(self.old.ENGINE)), 'engine_stop_unverified')

    def install(self):
        for item in self.changes:
            item.unchanged()
            self.base.write_atomic(item, item.replacement)
            self.files[item.path] = item.replacement
        self.old.replace_file(self.old.LIVE_ENV, self.new_env, self.helper, mode=0o600)
        self.old.replace_file(self.start.DROPIN, self.dropin, self.helper)
        self.files[self.old.LIVE_ENV], self.files[self.start.DROPIN] = self.new_env, self.dropin
        self.helper.create_private(STATE/'watchdog.env', environment_bytes(self.env))
        self.helper.root_parents(OWNER)
        OWNER.mkdir(mode=0o755)
        os.chmod(OWNER, 0o755)
        path = OWNER/'owner.lock'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchown(fd, self.user.pw_uid, self.user.pw_gid); os.close(fd)
        for unit, raw in ((UNIT, watchdog_service(self.original_context['python'])), (TIMER, watchdog_timer())):
            self.helper.create_private(Path('/etc/systemd/system')/unit, raw)
            self.files[Path('/etc/systemd/system')/unit] = raw
        self.command('daemon-reload')
        current = self.context(self.old.ENGINE)
        require(current['env'] == self.env and current['argv'] == self.original_context['argv'] and
                current['cwd'] == self.original_context['cwd'] and
                current['stable'][4:] == self.original_context['stable'][4:], 'new_engine_context_unexpected')
        self.contexts[self.old.ENGINE] = current

    def start_engine(self):
        self.broker.validate_receipt(self.last_broker, now())
        self.command('start', self.old.ENGINE)
        self.identities[self.old.ENGINE] = self.process(self.old.ENGINE)

    def wait_health(self):
        deadline = time.monotonic()+90
        while True:
            try:
                base = self.old.activation_health(self.get, self.policies, now, fast=True)
                result = self.get(8081, '/api/kadima/priority/status')
                require(result.get('release') == 'priority-trading-v1' and
                        all(result.get(k) is True for k in ('priority_requested', 'cas_live_requested', 'mcx_live_requested')) and
                        result.get('mcx_capital_inr') == self.args.mcx_capital, 'priority_runtime_not_loaded')
                return dict(base, priority=result)
            except (OSError, ValueError, urllib.error.URLError, self.old.Blocked) as exc:
                if isinstance(exc, self.old.Blocked) and str(exc) not in (
                        'engine_api_unhealthy', 'control_snapshot_stale'):
                    raise
                require(time.monotonic() < deadline, 'priority_runtime_health_timeout')
                time.sleep(0.5)

    def prepare_schedule(self):
        require(not self.disabled.exists(), 'operator_disabled_during_installation')
        if self.schedule_state.exists():
            previous = self.gate.read_json(self.schedule_state)
            require(previous.get('phase') != 'OPERATOR_PAUSED', 'operator_pause_today_preserved')
        cfg = self.get(8081, '/api/kadima/config')
        stock = self.get(8081, '/api/kadima/stock-fast')['controls']
        state = dict(day=now().astimezone(self.gate.IST).date().isoformat(), phase='ACTIVE', expected=dict(
            global_revision=cfg['revision'], global_enabled=cfg['auto_trade_enabled'],
            stock_revision=stock['revision'], stock_enabled=stock['live_enabled']))
        self.old.replace_file(self.schedule_state, json.dumps(state).encode(), self.helper, mode=0o600)

    def release_scheduler_lock(self):
        fcntl.flock(self.scheduler_lock, fcntl.LOCK_UN)

    def start_schedules(self):
        # Read-only broker verification through the guarded watchdog endpoint
        # while KADIMA is locally flat and the entry permit is still revoked.
        self.command('start', UNIT)
        self.command('enable', '--now', TIMER)
        self.command('enable', '--now', self.old.TIMER)
        self.command('start', self.old.JOB)

    def verify_schedules(self):
        for unit in (TIMER, self.old.TIMER):
            value = self.helper.observe_unit(unit)
            require(value.get('ActiveState') == 'active' and value.get('UnitFileState') == 'enabled',
                    'timer_enablement_unverified')
        status = self.gate.read_json(Path('/var/lib/kadima-session-auto/status.json'))
        stamp = datetime.fromisoformat(status['observed_at'])
        require(stamp.tzinfo is not None and 0 <= (now()-stamp).total_seconds() <= 45 and
                status.get('status') == 'priority_lane_schedule' and
                set(status.get('lanes', {})) == {'equity', 'mcx'}, 'fresh_priority_schedule_status_required')
        return status

    def fail_closed(self):
        result = {}
        for key, action in (('scheduler_disabled', lambda: self.command('disable', '--now', self.old.TIMER)),
                            ('watchdog_disabled', lambda: self.command('disable', '--now', TIMER)),
                            ('entry_permit_revoked', self.revoke)):
            try: action(); result[key] = True
            except Exception: result[key] = False
        result.update(engine_state=self.helper.observe_unit(self.old.ENGINE).get('ActiveState'),
                      manual_recovery_required=True, source_rollback_attempted=False)
        return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--mcx-capital', type=int, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    os.umask(0o077)
    ops = None
    try:
        with ExitStack() as locks:
            ops = Deployment.__new__(Deployment)
            ops.__init__(args, locks)
            report = transaction(ops, args.apply)
    except Exception as exc:
        report = getattr(exc, 'report', dict(state='blocked', scheduler_enabled=False, watchdog_enabled=False))
        # Raw exceptions may contain provider/auth text; publish only authored reasons.
        report['blocked'] = str(exc) if isinstance(exc, (Blocked,)+getattr(ops, 'safe_errors', ())) else type(exc).__name__
        if hasattr(exc, 'broker_readiness'): report['broker_readiness'] = exc.broker_readiness
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    print('MCX + CAS + OWNED WATCHDOG INSTALLED; session/source/budget checks still govern every entry.'
          if args.apply else 'PLAN ONLY: no runtime source, ledger or service changed.')


if __name__ == '__main__':
    main()
