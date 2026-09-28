"""Start only the reviewed engine after historical repair, with entries paused.

No runtime source, trade policy, ledger or broker order is changed. A retained
systemd drop-in removes optional live lanes; Celestial and its session permit
gate stay enabled. Dashboard/scanner/macro startup remains separately reported.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

PACKAGE = Path(__file__).resolve().parents[1]
ENGINE = 'kadima.service'
STATE = Path('/var/lib/kadima-paused-start-20260928')
DROPIN = Path('/etc/systemd/system/kadima.service.d/zzzz-kadima-paused-start.conf')
BACKUP_ROOT = Path('/var/backups')
REPAIR_BLOB = '04e8a5508fb34badb15be2b343fefd2e71d5e702'
UNSET_FLAGS = ('KADIMA_FORCE_START', 'KADIMA_MCX_LIVE', 'KADIMA_FAST_MOMENTUM_LIVE',
               'KADIMA_CAS_LIVE', 'KADIMA_GAP_LIVE', 'KADIMA_MOVE_CAPTURE_LIVE')
DROPIN_BYTES = ('[Service]\nUnsetEnvironment='+' '.join(UNSET_FLAGS)+'\n').encode()
DEPENDENCIES = ('Wants', 'Requires', 'BindsTo', 'Upholds', 'OnFailure', 'OnSuccess',
                'Conflicts', 'PartOf', 'PropagatesStopTo', 'Triggers', 'TriggeredBy')
INFRASTRUCTURE = frozenset(('network.target', 'network-online.target', 'sysinit.target',
    'basic.target', 'system.slice', 'local-fs.target', 'remote-fs.target', 'systemd-journald.socket'))
ROUTES = frozenset(((8080, '/health'), (8081, '/health'),
    (8081, '/api/kadima/config'), (8081, '/api/kadima/stock-fast'),
    (8081, '/api/kadima/top20'), (8081, '/api/kadima/audit?limit=1'),
    (8081, '/api/kadima/prices?limit=1'), (8081, '/api/kadima/learning?limit=1')))


class Blocked(RuntimeError):
    pass


class Warming(Blocked):
    pass


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def utc_now():
    return datetime.now(timezone.utc)


def paused_environment(env):
    require(env.get('KADIMA_CELESTIAL_ENABLED') == '1' and
            env.get('KADIMA_SESSION_AUTOTRADE') == '1', 'paused_entry_gates_required')
    require(all(name not in env for name in UNSET_FLAGS), 'optional_live_flags_must_be_unset')


def dependency_admission(properties):
    require(set(properties) == set(DEPENDENCIES), 'complete_dependency_properties_required')
    for key, value in properties.items():
        require(isinstance(value, str), 'unit_dependency_shape_invalid')
        names = value.split()
        allowed = INFRASTRUCTURE if key in ('Wants', 'Requires', 'BindsTo') else (
            frozenset(('shutdown.target',)) if key == 'Conflicts' else frozenset())
        require(all(name in allowed for name in names), 'unreviewed_engine_dependency:'+key)


def dependency_snapshot(invoke):
    command = ['/usr/bin/systemctl', 'show', ENGINE, '--no-pager', '--all', '--full']
    for name in DEPENDENCIES:
        command.extend(['-p', name])
    raw = invoke(command)
    rows = [line.split('=', 1) for line in raw.splitlines()]
    require(all(len(row) == 2 for row in rows) and len(rows) == len(DEPENDENCIES),
            'complete_dependency_properties_required')
    values = dict(rows)
    dependency_admission(values)
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def local_get(port, path, token):
    require((port, path) in ROUTES, 'unreviewed_health_endpoint')
    request = urllib.request.Request('http://127.0.0.1:'+str(port)+path,
        headers={'Authorization': 'Bearer '+token}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=3) as response:
        raw = response.read(2_000_001)
    require(len(raw) <= 2_000_000, 'health_response_oversized')
    value = json.loads(raw)
    require(isinstance(value, dict), 'health_response_invalid')
    return value


def runtime_window(now):
    local = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    wall = local.hour, local.minute, local.second, local.microsecond
    return local.weekday() < 5 and (9, 0, 0, 0) <= wall <= (15, 40, 0, 0)


def runtime_stamp(raw, now, name):
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise Blocked(name+'_timestamp_invalid') from None
    require(value.tzinfo is not None and value <= now, name+'_timestamp_invalid')
    return value


def verify_runtime(top, learning, now, started_at):
    if not runtime_window(now):
        return dict(runtime_broker_reconciliation_verified=False,
                    runtime_workers_pending=True)
    require(isinstance(started_at, datetime) and started_at.tzinfo is not None,
            'runtime_start_boundary_required')
    system = top.get('system')
    require(isinstance(system, dict), 'runtime_system_schema_invalid')
    reconciliation = system.get('reconciliation')
    if reconciliation is None:
        raise Warming('warming_broker_reconciliation')
    require(isinstance(reconciliation, dict), 'runtime_reconciliation_schema_invalid')
    checked = runtime_stamp(reconciliation.get('ts'), now, 'reconciliation')
    if checked < started_at or (now-checked).total_seconds() > 90:
        raise Warming('warming_broker_reconciliation')
    if reconciliation.get('ok') is False and reconciliation.get('reason') == 'checking_broker_books':
        raise Warming('warming_broker_reconciliation')
    require(reconciliation.get('ok') is True, 'runtime_broker_reconciliation_failed')
    positions = reconciliation.get('positions')
    require(isinstance(positions, list) and len(positions) <= 1,
            'runtime_broker_positions_unreviewed')
    allowed = dict(exchange='NFO', token='73921', product='CARRYFORWARD', quantity=130,
        symbol='NIFTY29SEP2623250CE', source='MANUAL_CARRYFORWARD', managed=True,
        auto_managed=False, reason='carryforward_observed_not_managed', underlying='NIFTY')
    for position in positions:
        require(isinstance(position, dict) and all(type(position.get(k)) is type(v) and
                position.get(k) == v for k, v in allowed.items()), 'runtime_manual_holding_unreviewed')
    worker_at = learning.get('worker_at')
    if worker_at is None and learning.get('worker_fresh') is False:
        raise Warming('warming_learning_worker')
    learned = runtime_stamp(worker_at, now, 'learning_worker')
    if learned < started_at or (now-learned).total_seconds() > 120 or learning.get('worker_fresh') is False:
        raise Warming('warming_learning_worker')
    require(learning.get('worker_fresh') is True, 'learning_worker_freshness_invalid')
    return dict(runtime_broker_reconciliation_verified=True, runtime_workers_pending=False,
                reconciliation_at=checked.isoformat(), learning_worker_at=learned.isoformat(),
                manual_carryforward_external=bool(positions))


def verify_health(get, policies, now, started_at=None):
    state, control = get(8080, '/health'), get(8081, '/health')
    require(state.get('ok') is True and state.get('engine_attached') is True and
            control.get('ok') is True and control.get('service') == 'kadima-control-api',
            'engine_api_health_unverified')
    cfg = get(8081, '/api/kadima/config')
    same = lambda a, b: json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(b, sort_keys=True, allow_nan=False)
    require(same(cfg, dict(policies['global'], rollout_enabled=True, broker_orders_enabled=True))
            and cfg.get('auto_trade_enabled') is False and cfg.get('kill_latched') is False,
            'runtime_global_pause_changed')
    stock = get(8081, '/api/kadima/stock-fast')
    require(same(stock.get('controls'), policies['stock']) and
            stock['controls'].get('live_enabled') is False and
            stock.get('fast_master_permission') is False and
            stock.get('global_auto_requested') is False and stock.get('kill_latched') is False and
            stock.get('celestial_rollout') is True, 'runtime_stock_pause_changed')
    top = get(8081, '/api/kadima/top20')
    require(same(top.get('config'), cfg) and top.get('positions') == [], 'managed_position_after_start')
    observed = now() if callable(now) else now
    for value in (top, stock):
        try:
            stamp = datetime.fromisoformat(value['ts'])
            require(stamp.tzinfo is not None and 0 <= (observed-stamp).total_seconds() <= 30,
                    'runtime_control_snapshot_stale')
        except (KeyError, TypeError, ValueError):
            raise Blocked('runtime_control_snapshot_invalid') from None
    audit = get(8081, '/api/kadima/audit?limit=1')
    prices = get(8081, '/api/kadima/prices?limit=1')
    learning = get(8081, '/api/kadima/learning?limit=1')
    for value in (audit, prices, learning):
        require(isinstance(value.get('rows'), list) and type(value.get('total')) is int
                and value['total'] >= 0, 'review_api_schema_invalid')
    require(prices.get('capture_enabled') is True and learning.get('mode') == 'ADVISORY',
            'review_feature_unverified')
    runtime = verify_runtime(top, learning, now() if callable(now) else now, started_at)
    return dict(api_health_verified=True, persisted_controls_unchanged=True,
                managed_position_count=0, learning_worker_fresh=learning.get('worker_fresh') is True,
                live_feed_verified=False, broker_flat_verified=False,
                **runtime)


def prepare_dropin(helper, state_dir, path=DROPIN):
    """Create one removable override; never replace an existing operator file."""
    helper.root_parents(path.parent)
    path.parent.mkdir(mode=0o755, exist_ok=True)
    info = path.parent.lstat()
    require(path.parent.resolve() == path.parent and stat.S_ISDIR(info.st_mode) and
            info.st_uid == 0 and not info.st_mode & 0o022, 'override_directory_unreviewed')
    require(not path.exists() and not path.is_symlink(), 'paused_override_already_exists')
    manifest = dict(path=str(path), original_present=False,
                    replacement_sha256=hashlib.sha256(DROPIN_BYTES).hexdigest())
    helper.create_private(state_dir/'override.json', json.dumps(manifest, sort_keys=True).encode())
    helper.create_private(path, DROPIN_BYTES)
    require(helper.read_regular(path) == DROPIN_BYTES, 'paused_override_write_unverified')
    return dict(path=str(path), retained=True, original_present=False)


def inactive(value):
    return value.get('ActiveState') == 'inactive' and value.get('MainPID') == '0'


def start_transaction(*, apply, helper, state_dir, admission, prepare_override,
                      after_reload, prestart, observe, invoke, health, guard, process):
    admission()
    require(inactive(observe(ENGINE)), 'engine_must_be_stopped_before_start')
    report = dict(action='repaired_paused_engine_start' if apply else 'repaired_paused_start_plan',
        state='paused_start_planned', activation=False, ledger_repaired=False,
        runtime_source_changed=False, start_requested=[], stopped_after_failure=[],
        start_command_invoked=False,
        peripheral_services_started=False, peripheral_startup_pending=True)
    if not apply:
        return report
    helper.root_parents(state_dir)
    state_dir.mkdir(mode=0o700, exist_ok=True)
    helper.private_path(state_dir, directory=True)
    helper.claim_attempt(state_dir, helper.utc_now())
    def record():
        helper.record_status(state_dir, report)
    try:
        report['state'] = 'preparing_paused_override'
        report['override_write_requested'] = True
        record()
        report['override'] = prepare_override()
        require(invoke(['/usr/bin/systemctl', 'daemon-reload']).returncode == 0,
                'paused_override_reload_failed')
        after_reload()
        prestart()
        require(inactive(observe(ENGINE)), 'engine_started_outside_operation')
        report['state'] = 'starting_paused_engine'
        report['start_requested'] = [ENGINE]
        record()
        report['start_command_invoked'] = True
        require(invoke(['/usr/bin/systemctl', 'start', ENGINE]).returncode == 0, 'engine_start_command_failed')
        report['process'] = process()
        guard()
        report['health'] = health()
        guard()
        require(process() == report['process'], 'engine_process_changed_during_health')
        report['state'] = 'repaired_engine_running_paused'
        record()
        return report
    except BaseException as exc:
        report['state'] = 'blocked_paused_start'
        report['blocked'] = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        if report['start_command_invoked']:
            try:
                result = invoke(['/usr/bin/systemctl', 'stop', ENGINE])
                report['engine_stopped_after_failure'] = result.returncode == 0 and inactive(observe(ENGINE))
                if report['engine_stopped_after_failure']:
                    report['stopped_after_failure'] = [ENGINE]
            except Exception:
                report['engine_stopped_after_failure'] = False
        report['override_retained'] = True if report.get('override') else (
            None if report.get('override_write_requested') else False)
        try:
            record()
        except Exception:
            report['status_recording_failed'] = True
        exc.report = report
        raise


@contextmanager
def database_owner(db):
    """Read WAL state as its owner so SQLite cannot create root-owned sidecars.

This standalone helper is single-threaded. Only pure, read-only SQL inspection
occurs inside the scope; root access is restored before any other operation.
"""
    info = db.stat()
    before = os.geteuid(), os.getegid()
    owner = info.st_uid, info.st_gid
    require(before[0] == 0 or before == owner, 'database_owner_inspection_required')
    try:
        if before != owner:
            os.setegid(owner[1])
            os.seteuid(owner[0])
        yield
    finally:
        if before != owner:
            os.seteuid(before[0])
            os.setegid(before[1])


def verify_repair(helper, repair, broker, db, evidence):
    """Read root provenance and SQLite archive; never apply another repair."""
    helper.root_parents(repair.STATE)
    helper.private_path(repair.STATE, directory=True)
    raw = {name: helper.read_regular(helper.private_path(repair.STATE/name), 100_000)
           for name in ('status.json', 'attempted.json')}
    status, attempt = json.loads(raw['status.json']), json.loads(raw['attempted.json'])
    require(status.get('state') == 'historical_repair_committed_services_stopped' and
            status.get('ledger_repaired') is True and status.get('original_rows_archived') is True and
            status.get('unknown_intent_fill_fabricated') is False and
            status.get('restart_requested') is False and status.get('activation') is False and
            status.get('commit_attempted') is True, 'successful_historical_repair_required')
    started, observed = datetime.fromisoformat(attempt['started_at']), datetime.fromisoformat(status['observed_at'])
    require(started.tzinfo is not None and observed.tzinfo is not None and started <= observed <= utc_now(),
            'repair_record_chronology_invalid')
    receipts = status.get('broker_receipts')
    require(isinstance(receipts, dict) and set(receipts) == {'before_backup', 'before_commit'},
            'repair_broker_provenance_missing')
    for value in receipts.values():
        broker.validate_receipt(value, datetime.fromisoformat(value['checked_at']))
        require(value['readiness'] is True and value['pending_orders'] == 0, 'repair_broker_provenance_invalid')
    require(datetime.fromisoformat(receipts['before_backup']['checked_at']) <=
            datetime.fromisoformat(receipts['before_commit']['started_at']) <= started <= observed,
            'repair_broker_chronology_invalid')
    backup = Path(status['backup'])
    require(backup.is_relative_to(BACKUP_ROOT) and backup.name == 'before.db', 'repair_backup_path_unreviewed')
    helper.root_parents(backup)
    helper.private_path(backup.parent, directory=True)
    helper.private_path(backup)
    require(db.resolve() == db and repair.identity(db.stat()) == repair.DB_IDENTITY,
            'repaired_database_identity_changed')
    with database_owner(db), closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        row = c.execute('SELECT * FROM contract_note_reconciliations WHERE intent=?', (evidence['intent'],)).fetchone()
        require(row is not None, 'repair_archive_missing')
        expected = (json.loads(row['original_position']), json.loads(row['original_order']))
        receipt = dict(schema='stopped_historical_repair_v1', manual_holding_external=True, **receipts)
        require(repair.digest_rows(expected) == repair.ROWS_SHA256, 'repair_original_rows_changed')
        repair.verify_committed(c, evidence, expected, receipt)
        repair.controls(c)
        policies = {key: json.loads(c.execute('SELECT payload FROM '+table+' WHERE id=1').fetchone()[0])
                    for key, table in (('global', 'celestial_config'), ('stock', 'stock_fast_controls'))}
    require(repair.identity(db.stat()) == repair.DB_IDENTITY, 'repaired_database_identity_changed')
    return policies, {repair.STATE/name: value for name, value in raw.items()}


def load_repair():
    path = PACKAGE/'momentum_recovery/repair_stopped_history.py'
    require(path.resolve() == path and not path.is_symlink(), 'repair_helper_path_unreviewed')
    raw = path.read_bytes()
    require(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() == REPAIR_BLOB,
            'reviewed_repair_helper_required')
    spec = importlib.util.spec_from_file_location('_paused_start_repair', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    os.umask(0o077)
    sys.dont_write_bytecode = True
    report = dict(state='blocked', activation=False, start_requested=[])
    try:
        repair = load_repair()
        ready = repair.load_readiness()
        helper, prep, source, note, context_module, broker, hold = (ready.load_reviewed(name) for name in ready.PINS)
        user = pwd.getpwnam('ubuntu')
        with ExitStack() as locks:
            for name in source.LOCKS:
                path = Path('/run/lock')/(name+'.lock')
                require(path.resolve(strict=False) == path, 'lock_path_unreviewed')
                stream = locks.enter_context(os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w'))
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                        not info.st_mode & 0o022, 'private_lock_required')
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, fingerprints = ready.verify_maintenance(helper, ready.AUDIT_STATE)
            _, prior = source.verify_prior_state(helper, ready.PRIOR_STATE)
            fingerprints.update(prior)
            source.ensure_stopped(ready.UNITS, helper.observe_unit)
            context = context_module.inspect_context(user=user, target=ready.TARGET)
            dependencies = dependency_snapshot(context_module.invoke)
            raw = ready.private_evidence(args.evidence, helper, user.pw_uid)
            require(hashlib.sha256(raw).hexdigest() == repair.EVIDENCE_SHA256, 'exact_reviewed_evidence_required')
            evidence = note.evidence(json.loads(raw), utc_now())
            note.account_matches(evidence, context.env)
            policies, records = verify_repair(helper, repair, broker, context.db_path, evidence)
            fingerprints.update(records)
            directory = Path(tempfile.mkdtemp(prefix='kadima-paused-start-source-', dir='/opt'))
            release = directory/'release'
            helper.stage_release(helper.canonical(args.history), release, source.RELEASE)
            installer = source.load_helper('_paused_start_installer', release/'trade_audit_update/install.py', source.INSTALLER_BLOB)
            base = installer.base
            fingerprints.update(ready.verify_sources(installer, release, ready.TARGET,
                PACKAGE/'momentum_recovery/reviewed_dashboard', (user.pw_uid, user.pw_gid), source))
            require(ready.blob(base.read_file(ready.TARGET/'kadima/config.py')) == ready.CONFIG_BLOB,
                    'installed_configuration_changed')
            token_raw = base.read_file(prep.TOKEN_FILE)
            token = json.loads(token_raw)['api_token']
            require(isinstance(token, str) and bool(token), 'api_token_required')
            fingerprints[prep.TOKEN_FILE] = token_raw
            configured = [context]
            startup_after = [None]

            def invariant():
                prep.verify_timer_hold()
                hold.verify_held_cron(helper)
                source.ensure_stopped(tuple(unit for unit in ready.UNITS if unit != ENGINE), helper.observe_unit)
                triggers = ready.watchdog_triggers(helper)
                require(not triggers['references'] and not triggers['running_pids'] and not triggers['inspection_errors'] and
                        all(value.get('ActiveState') == 'inactive' for value in triggers['timers'].values()),
                        'watchdog_triggers_must_remain_held')
                base.check_fingerprints(fingerprints)
                require(ready.private_evidence(args.evidence, helper, user.pw_uid) == raw, 'reviewed_evidence_changed')
                current_policies, _ = verify_repair(helper, repair, broker, context.db_path, evidence)
                require(current_policies == policies, 'persisted_policies_changed')
                require(dependency_snapshot(context_module.invoke) == dependencies, 'engine_dependencies_changed')

            def fresh_broker():
                value = broker.read_broker(configured[0].python, configured[0].env, user, ready.TARGET, evidence)
                broker.validate_receipt(value, utc_now())
                require(value['books_complete'] is True and value['readiness'] is True and
                        value['pending_orders'] == 0, 'fresh_broker_readiness_required')
                return value

            def admission():
                context.guard()
                invariant()
                require(context.env.get('KADIMA_CELESTIAL_ENABLED') == '1' and
                        context.env.get('KADIMA_SESSION_AUTOTRADE') == '1', 'paused_entry_gates_required')
                fresh_broker()
                invariant()
                context.guard()

            def after_reload():
                current = context_module.inspect_context(user=user, target=ready.TARGET)
                paused_environment(current.env)
                require(current.env == {k: v for k, v in context.env.items() if k not in UNSET_FLAGS},
                        'unexpected_environment_change')
                for path, item in context.fingerprints.items():
                    require(current.fingerprints.get(path) == item, 'existing_service_configuration_changed')
                require(set(current.fingerprints) == set(context.fingerprints) | {DROPIN} and
                        current.fingerprints[DROPIN].raw == DROPIN_BYTES, 'paused_override_not_loaded')
                configured[0] = current

            def prestart():
                configured[0].guard()
                invariant()
                receipt = fresh_broker()
                invariant()
                configured[0].guard()
                broker.validate_receipt(receipt, utc_now())
                startup_after[0] = utc_now()

            def process():
                current = helper.observe_unit(ENGINE)
                pid = current.get('MainPID', '')
                require(current.get('ActiveState') == 'active' and pid.isdigit() and int(pid) > 0,
                        'engine_process_not_active')
                path = Path('/proc')/pid
                require(path.stat().st_uid == user.pw_uid and (path/'cwd').resolve() == ready.TARGET,
                        'engine_process_identity_changed')
                command = (path/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
                require(command and command[0].decode() == str(configured[0].python) and
                        command[-1] in (b'run.py', str(ready.TARGET/'run.py').encode()) and
                        all(flag in (b'-u', b'-B') for flag in command[1:-1]), 'engine_process_arguments_changed')
                env = dict(pair.split(b'=', 1) for pair in (path/'environ').read_bytes().split(b'\0') if b'=' in pair)
                decoded = {k.decode(): v.decode() for k, v in env.items()}
                paused_environment(decoded)
                require(all(decoded.get(k) == v for k, v in configured[0].env.items()), 'engine_process_environment_changed')
                started = (path/'stat').read_text().rsplit(') ', 1)[1].split()[19]
                return dict(pid=pid, started_ticks=started)

            def running_guard():
                invariant()
                for path, item in configured[0].fingerprints.items():
                    require(context_module.read_regular(path, missing=item is None) == item,
                            'running_service_configuration_changed')
                process()

            def health():
                # ScripMaster.load precedes API binding and has a 60-second
                # download timeout. Allow that bounded initialization first.
                # Every request is GET-only and still validates paused policy.
                deadline = time.monotonic()+120
                next_progress = time.monotonic()+20
                while True:
                    try:
                        return verify_health(lambda port, path: local_get(port, path, token),
                                             policies, utc_now, startup_after[0])
                    except urllib.error.HTTPError as exc:
                        raise Blocked('engine_health_http_status:'+str(exc.code)) from None
                    except (Warming, OSError, urllib.error.URLError):
                        require(time.monotonic() < deadline, 'engine_api_start_timeout')
                        if time.monotonic() >= next_progress:
                            print('Waiting for paused engine APIs, fresh broker reconciliation and learning heartbeat.', flush=True)
                            next_progress = time.monotonic()+20
                        time.sleep(0.5)

            print('STATUS: '+str(STATE/'status.json'), flush=True)
            report = start_transaction(apply=args.apply, helper=helper, state_dir=STATE, admission=admission,
                prepare_override=lambda: prepare_dropin(helper, STATE), after_reload=after_reload,
                prestart=prestart, observe=helper.observe_unit,
                invoke=lambda command: subprocess.run(command, capture_output=True, text=True, timeout=30),
                health=health, guard=running_guard, process=process)
    except Exception as exc:
        report.update(getattr(exc, 'report', {}))
        report['blocked'] = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    print('Engine started with entries paused; dashboard, scanner, macro and watchdog remain stopped.'
          if args.apply else 'Plan only; no service or configuration change.')


if __name__ == '__main__':
    main()
