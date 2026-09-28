"""Start the reviewed dashboard, scanner and macro watcher beside a paused engine.

No broker order method, trading control write, runtime edit or watchdog start is
performed. Unknown unit shapes stop the operation with a redacted diagnostic.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PACKAGE = Path(__file__).resolve().parents[1]
TARGET = Path('/home/ubuntu/kadima-py')
ENGINE = 'kadima.service'
DASHBOARD = 'kadima-dashboard.service'
SCANNER = 'kadima-universe-scanner.service'
MACRO = 'kadima-macro-watch.service'
UNITS = (DASHBOARD, SCANNER, MACRO)
STATE = Path('/var/lib/kadima-paused-peripherals-20260928')
START_BLOB = '4598766524b40e732442dce2818104bf80109a47'
DEPENDENCIES = ('Wants', 'Requires', 'BindsTo', 'Upholds', 'OnFailure', 'OnSuccess',
                'Conflicts', 'PartOf', 'PropagatesStopTo', 'Triggers', 'TriggeredBy')
INFRASTRUCTURE = frozenset(('network.target', 'network-online.target', 'sysinit.target',
    'basic.target', 'system.slice', 'local-fs.target', 'remote-fs.target', 'systemd-journald.socket'))
EXTRA_ROUTES = frozenset(((8082, '/health'), (8081, '/universe_snapshot'), (8081, '/macro_snapshot')))
OBSERVER_OVERLAYS = {
    'kadima/macro_watch.py': '61f04cb86af8f187a6798e272d49f31776f4642d',
    'kadima/news_context.py': 'c8f31dd813856167749308455461973b76233983',
}
REVIEWED_RESTART_AT = datetime(2026, 9, 28, 10, 10, 13, tzinfo=timezone.utc)
REVIEWED_ORIGINAL_PROCESS = dict(pid='206362', started_ticks='104422145')
REVIEWED_RESTART_PROPERTIES = dict(ActiveState='active', SubState='running',
    MainPID='208838', Restart='always', NRestarts='1', Result='success',
    ExecMainStartTimestamp='Mon 2026-09-28 10:10:13 UTC',
    ExecMainExitTimestamp='', ExecMainCode='0', ExecMainStatus='0')
RESTART_PROPERTIES = (*REVIEWED_RESTART_PROPERTIES, 'ExecMainStartTimestampMonotonic')


class Blocked(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def utc_now():
    return datetime.now(timezone.utc)


def inactive(value):
    return value.get('ActiveState') == 'inactive' and value.get('MainPID') == '0'


def entrypoint(unit, raw, target=TARGET):
    """Admit only the existing observer entrypoints, never shell wrappers."""
    match = re.fullmatch(r'\{ path=([^;{}]+) ; argv\[\]=([^;{}]+) ; ([^{}]+) \}', raw)
    require(match is not None, 'peripheral_launch_shape_unsupported')
    executable, command = match.group(1).strip(), match.group(2).strip()
    require(not any(c in command for c in '\\"\'\n\r\x00'), 'peripheral_launch_shape_unsupported')
    argv = tuple(command.split())
    require(argv and argv[0] == executable and Path(executable).is_absolute(), 'peripheral_interpreter_unreviewed')
    require(re.search(r'(?:^| ; )ignore_errors=no(?: ; |$)', match.group(3)) is not None,
            'peripheral_launch_shape_unsupported')
    operands = list(argv[1:])
    while operands and operands[0] in ('-u', '-B'):
        operands.pop(0)
    allowed = {
        DASHBOARD: ['-m', 'uvicorn', 'gateway:app', '--host', '127.0.0.1', '--port', '8082', '--no-access-log'],
        SCANNER: ['-m', 'kadima.stock_universe_scanner'],
        MACRO: ['-m', 'kadima.macro_watch'],
    }
    require(unit in allowed and operands == allowed[unit], 'peripheral_entrypoint_unreviewed')
    return Path(executable), argv, Path('/opt/kadima-dashboard') if unit == DASHBOARD else target


def unit_dependencies(values, *, unit=None):
    require(set(values) == set(DEPENDENCIES), 'complete_dependency_properties_required')
    for key, value in values.items():
        require(isinstance(value, str), 'unit_dependency_shape_invalid')
        allowed = INFRASTRUCTURE | {ENGINE} if key in ('Wants', 'Requires') else (
            INFRASTRUCTURE if key == 'BindsTo' else (
                frozenset(('shutdown.target',)) if key == 'Conflicts' else frozenset()))
        # WorkingDirectory requires the root mount; the reviewed dashboard's
        # PrivateTmp=yes also adds Wants for its backing filesystem in systemd.
        # Admit this exact infrastructure mount, never arbitrary mount names.
        if key in ('Requires', 'Wants'):
            allowed = allowed | {'-.mount'}
        # AWS observation, 2026-09-28 15:30 IST: only the dashboard Wants
        # tmp.mount. Keep this exact mount scoped to that unit and relation.
        if unit == DASHBOARD and key == 'Wants':
            allowed = allowed | {'tmp.mount'}
        require(all(name in allowed for name in value.split()), 'unreviewed_peripheral_dependency:'+key)


def inspect_unit(unit, cm, user, *, engine=False):
    """Reuse the reviewed typed-empty/environment parser for each actual unit."""
    summary = dict(unit=unit, credentials_printed=False, runtime_imported=False)
    try:
        command = ['/usr/bin/systemctl', 'show', unit, '--all', '--no-pager', '--full']
        for name in cm.PROPERTIES:
            command.extend(['-p', name])
        raw = cm.invoke(command)
        # The old helper has one fixed D-Bus object path. Bind its typed-empty
        # proof queries to this unit; all parsing and ownership checks stay intact.
        object_path = '/org/freedesktop/systemd1/unit/'+''.join(
            c if c.isascii() and c.isalnum() else '_'+format(ord(c), '02x') for c in unit)
        def bound_invoke(argv):
            fixed = '/org/freedesktop/systemd1/unit/kadima_2eservice'
            return cm.invoke([object_path if part == fixed else part for part in argv])
        properties, proofs = cm.service_properties(raw, bound_invoke, summary)
        summary['unit'] = unit
        require(properties['LoadState'] == 'loaded', 'peripheral_unit_not_loaded')
        require(properties['User'] == 'ubuntu' and properties['Group'] in ('', 'ubuntu'), 'service_owner_unreviewed')
        require(properties['DynamicUser'] == 'no' and properties['NeedDaemonReload'] == 'no' and
                all(properties[name] == '' for name in cm.EMPTY_PROPERTIES), 'service_context_layering_unsupported')
        if engine:
            python = cm.parse_exec_start(properties['ExecStart'], TARGET)
            command_match = re.fullmatch(r'\{ path=([^;{}]+) ; argv\[\]=([^;{}]+) ; ([^{}]+) \}', properties['ExecStart'])
            argv, cwd = tuple(cm.words(command_match.group(2).strip())), TARGET
        else:
            python, argv, cwd = entrypoint(unit, properties['ExecStart'])
        require(properties['WorkingDirectory'] == str(cwd), 'service_directory_unreviewed')
        summary.update(interpreter=str(python), working_directory=str(cwd), direct_python_entrypoint=True)
        fingerprints = {}
        paths = [properties['FragmentPath'], *cm.words(properties['DropInPaths'])]
        require(len(paths) <= 20, 'too_many_unit_fragments')
        for name in paths:
            path = Path(name)
            require(path.is_absolute() and any(path.is_relative_to(prefix) for prefix in (
                Path('/etc/systemd/system'), Path('/run/systemd/system'), Path('/usr/lib/systemd/system'))),
                'unit_fragment_path_unreviewed')
            item = cm.read_regular(path)
            require(item.metadata[2] == 0 and not item.metadata[4] & 0o022, 'unit_fragment_metadata_unreviewed')
            fingerprints[path] = item
        env = cm.parse_environment(properties['Environment'])
        for path, optional in cm.parse_environment_files(properties['EnvironmentFiles']):
            item = cm.read_regular(path, missing=optional)
            if item is not None:
                require(item.metadata[2] in (0, user.pw_uid) and not item.metadata[4] & 0o022,
                        'environment_file_metadata_unreviewed')
                env.update(cm.parse_environment_file(item.raw))
            fingerprints[path] = item
        explicit_names = set(env)
        unsets = cm.words(properties['UnsetEnvironment'])
        for value in unsets:
            name, _, content = value.partition('=')
            require(cm.NAME.fullmatch(name) is not None, 'unset_environment_shape_unsupported')
            if '=' not in value or env.get(name) == content:
                env.pop(name, None)
        unsafe = lambda name: name.startswith('LD_') or (
            name.startswith('PYTHON') and name not in ('PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE')) or (
                name.lower() in ('http_proxy', 'https_proxy', 'all_proxy'))
        relevant = lambda name: name.startswith(('ANGEL_', 'KADIMA_')) or name == 'HOME' or unsafe(name)
        manager_raw = cm.invoke(['/usr/bin/systemctl', 'show-environment'])
        for line in manager_raw.splitlines():
            name = line.split('=', 1)[0]
            if not line.strip() or not relevant(name):
                continue
            values = cm.words(line)
            require(len(values) == 1, 'manager_environment_shape_unsupported')
            name, content = cm.assignment(values[0])
            require(name in explicit_names or name in unsets or name+'='+content in unsets,
                    'manager_only_runtime_environment_unsupported')
        require(not any(unsafe(k) and v for k, v in env.items()), 'interpreter_or_proxy_environment_unsupported')
        require(env.get('HOME', '/home/ubuntu') == '/home/ubuntu' and
                env.get('KADIMA_DATA_DIR', '/var/lib/kadima') == '/var/lib/kadima', 'observer_data_directory_unreviewed')
        if unit == DASHBOARD:
            require(env.get('KADIMA_DASHBOARD_CONFIG', '/etc/kadima-dashboard/config.json') ==
                    '/etc/kadima-dashboard/config.json', 'dashboard_configuration_unreviewed')
        if unit == SCANNER:
            require(env.get('KADIMA_CELESTIAL_ENABLED') == '1', 'full_universe_scanner_required')
        interpreter = cm.verify_interpreter(python, TARGET, user, cm.read_regular)
        dep_command = ['/usr/bin/systemctl', 'show', unit, '--all', '--no-pager', '--full']
        for name in DEPENDENCIES:
            dep_command.extend(['-p', name])
        rows = [line.split('=', 1) for line in cm.invoke(dep_command).splitlines()]
        require(len(rows) == len(DEPENDENCIES) and all(len(row) == 2 for row in rows), 'complete_dependency_properties_required')
        dependencies = dict(rows)
        summary['dependencies'] = dependencies
        unit_dependencies(dependencies, unit=unit)
        dependencies = {key: ' '.join(sorted(set(value.split()))) for key, value in dependencies.items()}
        summary['dependencies'] = dependencies
        summary.update(environment_names=sorted(env))
        stable = (argv, cwd, env, fingerprints, interpreter, tuple(sorted(manager_raw.splitlines())), dependencies)
        return dict(unit=unit, python=python, argv=argv, cwd=cwd, env=env,
                    fingerprints=fingerprints, stable=stable, summary=summary)
    except Exception as exc:
        summary['unit'] = unit
        exc.summary = summary
        raise


def process_identity(context, observe, user, *, environment_guard=None):
    value = observe(context['unit'])
    pid = value.get('MainPID', '')
    require(value.get('ActiveState') == 'active' and pid.isdigit() and int(pid) > 0, 'service_process_not_active')
    path = Path('/proc')/pid
    require(path.stat().st_uid == user.pw_uid and (path/'cwd').resolve() == context['cwd'], 'service_process_identity_changed')
    argv = tuple(part.decode() for part in (path/'cmdline').read_bytes().rstrip(b'\0').split(b'\0'))
    require(argv == context['argv'], 'service_process_arguments_changed')
    env = dict(part.split(b'=', 1) for part in (path/'environ').read_bytes().split(b'\0') if b'=' in part)
    require(all(env.get(k.encode()) == v.encode() for k, v in context['env'].items()), 'service_process_environment_changed')
    if environment_guard is not None:
        environment_guard({k.decode(): v.decode() for k, v in env.items()})
    started = (path/'stat').read_text().rsplit(') ', 1)[1].split()[19]
    return dict(pid=pid, started_ticks=started)


def verify_observer_sources(base, release, target, reviewed=PACKAGE/'momentum_recovery/reviewed_observers',
                            admitted=None):
    """Admit both observer entrypoints and their complete reviewed local imports.

    The audit backend's import roots omit these independently launched modules.
    Keep the observed commodity/news overlay, whose two exact blobs were read
    from GitHub and reviewed, without replacing any installed runtime file.
    """
    overlays = {}
    for name, expected in OBSERVER_OVERLAYS.items():
        raw = base.read_file(reviewed/Path(name).name)
        require(base.digest(raw) == expected, 'reviewed_observer_reference_changed:'+name)
        overlays[name] = raw
    # Include all explicit overlays as roots: news_context is deliberately not
    # present in the pinned PR24 tree, so local_modules(release) cannot find it.
    todo = ['kadima/__init__.py', 'kadima/stock_universe_scanner.py', *overlays]
    fingerprints = {}
    while todo:
        name = todo.pop()
        path = target/name
        if path in fingerprints:
            continue
        expected = (overlays[name] if name in overlays else
                    admitted[path] if admitted is not None and path in admitted else base.read_file(release/name))
        current = base.read_file(path)
        require(current == expected, 'unreviewed_observer_source:'+name)
        compile(current, name, 'exec')
        fingerprints[path] = current
        todo.extend(base.local_modules(current, release))
    return fingerprints


def verify_startup_record(start, helper, dependencies, now):
    """Require the successful recovery, preserving the original failed attempt."""
    fingerprints, override = start.verify_resume(helper, dependencies)
    state = start.RESUME_STATE
    helper.root_parents(state)
    helper.private_path(state, directory=True)
    records = {name: helper.read_regular(helper.private_path(state/name), 100_000)
               for name in ('status.json', 'attempted.json')}
    status, attempt = (json.loads(records[name]) for name in ('status.json', 'attempted.json'))
    expected = dict(action='repaired_paused_engine_start', state='repaired_engine_running_paused',
        activation=False, start_requested=[ENGINE], start_command_invoked=True,
        resumed_before_start=True, override_write_requested=False, override_retained=True,
        runtime_source_changed=False, ledger_repaired=False, peripheral_services_started=False,
        peripheral_startup_pending=True, stopped_after_failure=[])
    require(isinstance(status, dict) and all(type(status.get(k)) is type(v) and status[k] == v
            for k, v in expected.items()), 'verified_paused_recovery_required')
    same = lambda a, b: json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(b, sort_keys=True, allow_nan=False)
    require(same(status.get('override'), override) and
            same(status.get('startup_manual_holding_exception'), start.STARTUP_MANUAL_HOLDING) and
            not any(k in status for k in ('blocked', 'status_recording_failed', 'engine_stopped_after_failure')),
            'paused_recovery_record_unreviewed')
    require(isinstance(attempt, dict) and set(attempt) == {'started_at'}, 'paused_start_record_invalid')
    try:
        started = datetime.fromisoformat(attempt['started_at'])
        observed = datetime.fromisoformat(status['observed_at'])
        require(started.tzinfo is not None and observed.tzinfo is not None and
                started <= observed <= now, 'paused_start_record_invalid')
    except (KeyError, TypeError, ValueError):
        raise Blocked('paused_start_record_invalid') from None
    process = status.get('process')
    require(isinstance(process, dict) and set(process) == {'pid', 'started_ticks'} and
            all(isinstance(process[k], str) and process[k].isdigit() and int(process[k]) > 0
                for k in process), 'paused_engine_process_record_invalid')
    fingerprints.update({state/name: value for name, value in records.items()})
    return status, started, fingerprints


def restart_snapshot(invoke):
    command = ['/usr/bin/systemctl', 'show', ENGINE, '--all', '--full', '--no-pager']
    for name in RESTART_PROPERTIES:
        command.extend(['-p', name])
    rows = [line.split('=', 1) for line in invoke(command).splitlines()]
    require(len(rows) == len(RESTART_PROPERTIES) and all(len(row) == 2 for row in rows),
            'restart_property_shape_invalid')
    values = dict(rows)
    require(set(values) == set(RESTART_PROPERTIES), 'restart_property_shape_invalid')
    require(all(values[k] == v for k, v in REVIEWED_RESTART_PROPERTIES.items()),
            'reviewed_session_restart_changed')
    stamp = values['ExecMainStartTimestampMonotonic']
    require(stamp.isascii() and stamp.isdigit() and int(stamp) > 0,
            'restart_monotonic_timestamp_invalid')
    return values


def bind_engine_process(status, current, started_at, *, allow_restart, invoke, now, ticks_per_second):
    """Bind this operation to one process; never rewrite the startup receipt."""
    original = status['process']
    binding = dict(original_process=dict(original), process=dict(current),
        started_at=started_at.isoformat(), session_restart=False, revalidated=False)
    if current == original:
        return binding
    require(allow_restart, 'paused_engine_process_changed')
    require(original == REVIEWED_ORIGINAL_PROCESS and
            current.get('pid') == REVIEWED_RESTART_PROPERTIES['MainPID'] and
            set(current) == {'pid', 'started_ticks'} and
            isinstance(current['started_ticks'], str) and current['started_ticks'].isascii() and
            current['started_ticks'].isdigit() and
            int(current['started_ticks']) > int(original['started_ticks']),
            'reviewed_session_restart_process_required')
    require(now.tzinfo is not None and now.date() == REVIEWED_RESTART_AT.date() and
            started_at <= datetime.fromisoformat(status['observed_at']) < REVIEWED_RESTART_AT <= now,
            'reviewed_session_restart_chronology_required')
    properties = restart_snapshot(invoke)
    require(type(ticks_per_second) is int and ticks_per_second > 0 and
            abs(int(properties['ExecMainStartTimestampMonotonic']) / 1_000_000 -
                int(current['started_ticks']) / ticks_per_second) <= 1,
            'restart_process_timestamp_mismatch')
    binding.update(session_restart=True, started_at=REVIEWED_RESTART_AT.isoformat(),
                   systemd=properties)
    return binding


def guard_engine_binding(binding, current, invoke, now):
    require(current == binding['process'], 'paused_engine_process_changed')
    if binding['session_restart']:
        require(now.date() == REVIEWED_RESTART_AT.date() and now >= REVIEWED_RESTART_AT,
                'reviewed_session_restart_chronology_required')
        require(restart_snapshot(invoke) == binding['systemd'], 'reviewed_session_restart_changed')


def revalidate_restart_broker(binding, *, read, start, broker, now):
    if not binding['session_restart']:
        return
    value = binding.get('broker_readiness')
    if value is None or (now - datetime.fromisoformat(value['checked_at'])).total_seconds() >= 20:
        value = read()
        # Only the pinned, bounded receipt schema can enter the report.
        broker.validate_receipt(value, utc_now())
        binding['broker_readiness'] = json.loads(json.dumps(value))
    start.require_broker_readiness(value, broker.validate_receipt, utc_now())


def transaction(*, apply, helper, state_dir, admission, observe, invoke, health, guard, process,
                engine_validation=None):
    admission()
    require(all(inactive(observe(unit)) for unit in UNITS), 'peripherals_must_be_stopped')
    report = dict(action='start_paused_peripherals' if apply else 'paused_peripherals_plan',
        state='paused_peripherals_planned', activation=False, start_requested=[], processes={},
        stopped_after_failure=[], cleanup_failures=[], runtime_source_changed=False)
    if engine_validation is not None:
        report['engine_validation'] = engine_validation
    if not apply:
        return report
    helper.root_parents(state_dir)
    state_dir.mkdir(mode=0o700, exist_ok=True)
    helper.private_path(state_dir, directory=True)
    helper.claim_attempt(state_dir, helper.utc_now())
    attempted = []
    try:
        for unit in UNITS:
            guard()
            require(inactive(observe(unit)), 'peripheral_started_outside_operation')
            report['state'] = 'starting_paused_peripherals'
            report['start_requested'].append(unit)
            helper.record_status(state_dir, report)
            attempted.append(unit)
            require(invoke(['/usr/bin/systemctl', 'start', unit]).returncode == 0, 'peripheral_start_command_failed')
            report['processes'][unit] = process(unit)
        report['health'] = health()
        guard()
        require(all(process(unit) == identity for unit, identity in report['processes'].items()),
                'peripheral_process_changed_during_health')
        report['state'] = 'paused_peripherals_running'
        helper.record_status(state_dir, report)
        return report
    except BaseException as exc:
        report['state'] = 'paused_peripherals_blocked'
        report['blocked'] = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        for unit in reversed(attempted):
            try:
                done = invoke(['/usr/bin/systemctl', 'stop', unit]).returncode == 0 and inactive(observe(unit))
            except Exception:
                done = False
            report['stopped_after_failure' if done else 'cleanup_failures'].append(unit)
        try:
            helper.record_status(state_dir, report)
        except Exception:
            report['status_recording_failed'] = True
        exc.report = report
        raise


def load_start():
    path = PACKAGE/'momentum_recovery/start_repaired_paused.py'
    require(path.resolve() == path and not path.is_symlink(), 'startup_helper_path_unreviewed')
    raw = path.read_bytes()
    require(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() == START_BLOB,
            'reviewed_startup_helper_required')
    spec = importlib.util.spec_from_file_location('_peripheral_start_helper', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshot_health(get, after, now):
    require(get(8082, '/health').get('ok') is True, 'dashboard_gateway_health_unverified')
    scanner, macro = get(8081, '/universe_snapshot'), get(8081, '/macro_snapshot')
    now = now() if callable(now) else now
    for name, value in (('scanner', scanner), ('macro', macro)):
        try:
            stamp = datetime.fromisoformat(value['ts'])
            require(stamp.tzinfo is not None and after <= stamp <= now and
                    (now-stamp).total_seconds() <= 180, name+'_snapshot_not_fresh_after_start')
        except (KeyError, ValueError, TypeError):
            raise Blocked(name+'_snapshot_timestamp_invalid') from None
    require(isinstance(scanner.get('tracked'), list) and isinstance(scanner.get('prices'), dict) and
            scanner.get('execution') == 'central_executor_gated', 'scanner_snapshot_schema_invalid')
    require(isinstance(macro.get('news'), list) and isinstance(macro.get('economic_calendar'), list),
            'macro_snapshot_schema_invalid')
    return dict(dashboard_gateway_health_verified=True, scanner_snapshot_at=scanner['ts'],
                macro_snapshot_at=macro['ts'], scanner_priced_count=len(scanner['prices']),
                scanner_coverage=scanner.get('coverage'), live_feed_verified=False,
                market_data_verification_pending=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reviewed-session-restart', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    os.umask(0o077)
    sys.dont_write_bytecode = True
    report = dict(state='blocked', activation=False, start_requested=[])
    safe_errors = (Blocked,)
    try:
        start = load_start()
        safe_errors += (start.Blocked,)
        repair = start.load_repair()
        safe_errors += (repair.Blocked,)
        ready = repair.load_readiness()
        safe_errors += (ready.Blocked,)
        helper, prep, source, note, cm, broker, hold = (ready.load_reviewed(name) for name in ready.PINS)
        safe_errors += tuple(module.Blocked for module in (helper, prep, source, note, cm, broker, hold)
                             if hasattr(module, 'Blocked'))
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
            startup_status, engine_after, startup_records = verify_startup_record(
                start, helper, start.dependency_snapshot(cm.invoke), utc_now())
            fingerprints.update(startup_records)
            require(helper.read_regular(start.DROPIN) == start.DROPIN_BYTES, 'paused_override_changed')
            fingerprints[start.DROPIN] = start.DROPIN_BYTES
            contexts = {unit: inspect_unit(unit, cm, user, engine=unit == ENGINE) for unit in (ENGINE, *UNITS)}
            start.paused_environment(contexts[ENGINE]['env'])
            def engine_process():
                return process_identity(contexts[ENGINE], helper.observe_unit, user,
                    environment_guard=start.paused_environment)
            engine_binding = bind_engine_process(startup_status, engine_process(), engine_after,
                allow_restart=args.reviewed_session_restart, invoke=cm.invoke, now=utc_now(),
                ticks_per_second=os.sysconf('SC_CLK_TCK'))
            report['engine_validation'] = engine_binding
            engine_after = datetime.fromisoformat(engine_binding['started_at'])
            raw = ready.private_evidence(args.evidence, helper, user.pw_uid)
            require(hashlib.sha256(raw).hexdigest() == repair.EVIDENCE_SHA256, 'exact_reviewed_evidence_required')
            evidence = note.evidence(json.loads(raw), utc_now())
            note.account_matches(evidence, contexts[ENGINE]['env'])
            policies, records = start.verify_repair(helper, repair, broker, Path('/var/lib/kadima/kadima.db'), evidence)
            fingerprints.update(records)
            release = Path(tempfile.mkdtemp(prefix='kadima-peripheral-source-', dir='/opt'))/'release'
            helper.stage_release(helper.canonical(args.history), release, source.RELEASE)
            installer = source.load_helper('_peripheral_source_installer', release/'trade_audit_update/install.py', source.INSTALLER_BLOB)
            base = installer.base
            safe_errors += (base.Blocked,)
            fingerprints.update(ready.verify_sources(installer, release, TARGET,
                PACKAGE/'momentum_recovery/reviewed_dashboard', (user.pw_uid, user.pw_gid), source))
            require(ready.blob(base.read_file(TARGET/'kadima/config.py')) == ready.CONFIG_BLOB, 'installed_configuration_changed')
            fingerprints.update(verify_observer_sources(base, release, TARGET, admitted=fingerprints))
            token_raw = base.read_file(prep.TOKEN_FILE)
            token = json.loads(token_raw)['api_token']
            require(isinstance(token, str) and bool(token), 'api_token_required')
            fingerprints[prep.TOKEN_FILE] = token_raw
            dashboard_path = Path('/etc/kadima-dashboard/config.json')
            dashboard_config = cm.read_regular(dashboard_path)
            require(dashboard_config.metadata[2] == 0 and not dashboard_config.metadata[4] & 0o022,
                    'dashboard_config_metadata_unreviewed')
            require(json.loads(dashboard_config.raw).get('api_token') == token,
                    'dashboard_api_binding_changed')
            fingerprints[dashboard_path] = dashboard_config.raw

            def invariant():
                prep.verify_timer_hold()
                hold.verify_held_cron(helper)
                require(inactive(helper.observe_unit('kadima-eod-watchdog.service')), 'watchdog_must_remain_stopped')
                triggers = ready.watchdog_triggers(helper)
                require(not triggers['references'] and not triggers['running_pids'] and not triggers['inspection_errors'] and
                        all(value.get('ActiveState') == 'inactive' for value in triggers['timers'].values()),
                        'watchdog_triggers_must_remain_held')
                base.check_fingerprints(fingerprints)
                require(ready.private_evidence(args.evidence, helper, user.pw_uid) == raw, 'reviewed_evidence_changed')
                for unit, before in contexts.items():
                    require(inspect_unit(unit, cm, user, engine=unit == ENGINE)['stable'] == before['stable'],
                            'service_context_changed:'+unit)
                guard_engine_binding(engine_binding, engine_process(), cm.invoke, utc_now())
                current_policies, _ = start.verify_repair(helper, repair, broker,
                    Path('/var/lib/kadima/kadima.db'), evidence)
                require(current_policies == policies, 'persisted_policies_changed')

            def engine_health():
                deadline = time.monotonic()+15
                while True:
                    try:
                        value = start.verify_health(lambda port, path: start.local_get(port, path, token), policies, utc_now, engine_after)
                        break
                    except start.Warming:
                        require(time.monotonic() < deadline, 'current_engine_reconciliation_required')
                        guard_engine_binding(engine_binding, engine_process(), cm.invoke, utc_now())
                        time.sleep(0.5)
                if start.runtime_window(utc_now()):
                    require(value['runtime_broker_reconciliation_verified'] is True and not value['runtime_workers_pending'],
                            'current_engine_reconciliation_required')
                return value

            def guard():
                invariant()
                engine_health()
                context = contexts[ENGINE]
                revalidate_restart_broker(engine_binding,
                    read=lambda: broker.read_broker(context['python'], context['env'], user, TARGET, evidence),
                    start=start, broker=broker, now=utc_now())
                invariant()
                engine_binding['revalidated'] = True

            def admission():
                guard()

            def extra_get(port, path):
                require((port, path) in EXTRA_ROUTES, 'unreviewed_health_endpoint')
                request = urllib.request.Request('http://127.0.0.1:'+str(port)+path,
                    headers={'Authorization': 'Bearer '+token}, method='GET')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), start.NoRedirect())
                with opener.open(request, timeout=3) as response:
                    raw_response = response.read(2_000_001)
                require(len(raw_response) <= 2_000_000, 'health_response_oversized')
                value = json.loads(raw_response)
                require(isinstance(value, dict), 'health_response_invalid')
                return value

            boundary = utc_now()
            def health():
                deadline, progress = time.monotonic()+120, time.monotonic()+20
                while True:
                    try:
                        value = snapshot_health(extra_get, boundary, utc_now)
                        value['engine'] = engine_health()
                        return value
                    except urllib.error.HTTPError as exc:
                        raise Blocked('peripheral_health_http_status:'+str(exc.code)) from None
                    except (OSError, urllib.error.URLError, Blocked) as exc:
                        # Source/config/policy failures are not retried here;
                        # this wait is only for new observer snapshots and APIs.
                        retry = not isinstance(exc, Blocked) or 'snapshot_' in str(exc)
                        require(retry and time.monotonic() < deadline, 'peripheral_health_unverified')
                        if time.monotonic() >= progress:
                            print('Waiting for dashboard health and fresh scanner/macro snapshots.', flush=True)
                            progress = time.monotonic()+20
                        time.sleep(0.5)

            print('STATUS: '+str(STATE/'status.json'), flush=True)
            report = transaction(apply=args.apply, helper=helper, state_dir=STATE, admission=admission,
                observe=helper.observe_unit, invoke=lambda command: subprocess.run(command, capture_output=True, text=True, timeout=30),
                health=health, guard=guard,
                process=lambda unit: process_identity(contexts[unit], helper.observe_unit, user),
                engine_validation=engine_binding)
            report['service_contexts'] = {unit: value['summary'] for unit, value in contexts.items()}
    except Exception as exc:
        report.update(getattr(exc, 'report', {}))
        report['blocked'] = str(exc) if isinstance(exc, safe_errors) else type(exc).__name__
        if hasattr(exc, 'summary'):
            report['service_context'] = exc.summary
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    print('Dashboard, scanner and macro services started; engine entries remain paused and watchdog held.'
          if args.apply else 'Plan only; no service or configuration changed.')


if __name__ == '__main__':
    main()
