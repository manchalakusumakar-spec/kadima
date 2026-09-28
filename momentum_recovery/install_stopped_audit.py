"""Install pinned audit/learning sources while KADIMA remains stopped.

Requires the successful earlier PR19 source-only maintenance record. The manual
holding stays external. No runtime import, broker/DB call, repair or restart is
performed; runtime health, ledger repair and startup remain separate work.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
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


PACKAGE = Path(__file__).resolve().parents[1]
TARGET = Path('/home/ubuntu/kadima-py')
PRIOR_STATE = Path('/var/lib/kadima-stopped-source-20260928')
STATE = Path('/var/lib/kadima-stopped-audit-20260928')
RELEASE = {'commit': '8837cd6dc04e16123e94d8b78a3a716f83ed022c',
           'tree': '334e676b2750ba9d7b9feb56d5a2068904c8774a'}
PRIOR_COMMIT = 'bd294a7048749258fbaa2cc786f20a441a2a8073'
SCHEDULED_BLOB = '019f35decefe72f56cfb600b5321b2c014e48c6a'
PREP_BLOB = '363fe8b8c2e1dbcafca11f89afb2666c2a8f87f3'
INSTALLER_BLOB = '3e2130a008acf5e206a98b055c330228d1e4a11a'
RUN_BLOB = '3dd1a763faf472831b33db0bbf21d0d2acca16f7'
PRIOR_UNITS = ('kadima-universe-scanner.service', 'kadima.service',
               'kadima-macro-watch.service', 'kadima-eod-watchdog.service')
DASHBOARD = 'kadima-dashboard.service'
UNITS = (DASHBOARD,) + PRIOR_UNITS
UI_PREDECESSORS = {'gateway.py': '059f182fce93c71269e92501cc00af4f710f83ec',
                   'dashboard.html': '4c9d221436b7675eee5fda80d1546311273f6832'}
UI_REPLACEMENTS = {'gateway.py': 'f45a083d859d286d09cf251f34b9e15c096227ab',
                   'dashboard.html': '3816a160f46420aaf28cb7f8de8579fdd3cf31e5'}
LOCKS = ('kadima-stopped-audit-install', 'kadima-stopped-source-install',
         'kadima-stock-fast-install', 'kadima-activation', 'kadima-session-auto-run',
         'kadima-session-auto-install', 'kadima-multi-position-install',
         'kadima-auth-recovery', 'kadima-kill-reset', 'kadima-contract-note-repair',
         'kadima-mcx-live-install')


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def load_helper(name, path, expected):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'helper_path_unreviewed')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'helper_file_required')
        raw = stream.read(4_000_001)
    require(len(raw) <= 4_000_000 and digest(raw) == expected, 'reviewed_helper_required')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_prior_state(helper, path):
    """Read immutable maintenance provenance, never the trading database."""
    helper.root_parents(path)
    helper.private_path(path, directory=True)
    fingerprints = {}
    for name in ('status.json', 'attempted.json'):
        source = path/name
        helper.private_path(source)
        fingerprints[source] = helper.read_regular(source, 100_000)
    status = json.loads(fingerprints[path/'status.json'])
    attempt = json.loads(fingerprints[path/'attempted.json'])
    require(isinstance(status, dict) and isinstance(attempt, dict), 'prior_maintenance_record_invalid')
    require(status.get('state') == 'source_installed_services_stopped' and
            status.get('source_commit') == PRIOR_COMMIT and
            status.get('source_installed') is True and
            status.get('manual_holding_external') is True and
            status.get('activation') is False and status.get('restart_requested') is False and
            status.get('ledger_repaired') is False, 'successful_prior_maintenance_required')
    units = status.get('stopped_units')
    require(isinstance(units, list) and len(units) == len(PRIOR_UNITS) and
            set(units) == set(PRIOR_UNITS), 'prior_stopped_units_unreviewed')
    try:
        started = datetime.fromisoformat(attempt['started_at'])
        observed = datetime.fromisoformat(status['observed_at'])
        now = helper.utc_now()
        require(started.tzinfo is not None and observed.tzinfo is not None and
                now.tzinfo is not None and started <= observed <= now,
                'prior_attempt_status_mismatch')
    except (KeyError, TypeError, ValueError):
        raise Blocked('prior_attempt_status_mismatch') from None
    return status, fingerprints


def ensure_stopped(units, observe):
    for name in units:
        state = observe(name)
        require(state.get('LoadState') in ('loaded', 'masked') and
                state.get('ActiveState') == 'inactive' and state.get('MainPID') == '0',
                'maintenance_service_not_stopped')


def build_plan(installer, release, target, ui_root, owner):
    """Use the pinned PR24 dependency traversal; preserve custom UI by exact pins."""
    base = installer.base
    changes, fingerprints = base.backend_plan(release, target, owner)
    run = target/'run.py'
    raw = base.read_file(run)
    require(digest(raw) == RUN_BLOB and digest(base.read_file(release/'run.py')) == RUN_BLOB,
            'reviewed_pr19_entrypoint_required')
    fingerprints[run] = raw
    for name, destination in (('gateway.py', base.GATEWAY), ('dashboard.html', base.PAGE)):
        original = base.read_file(destination)
        require(digest(original) == UI_PREDECESSORS[name], 'installed_dashboard_source_unreviewed')
        replacement = base.read_file(ui_root/name)
        require(digest(replacement) == UI_REPLACEMENTS[name], 'reviewed_dashboard_composition_required')
        if name.endswith('.py'):
            compile(replacement, str(destination), 'exec')
        fingerprints[destination] = original
        if original != replacement:
            changes.append(base.change_for(destination, replacement, owner))
    return changes, fingerprints


def verify_installed(base, changes, fingerprints):
    replaced = {item.path: item for item in changes}
    for path, original in fingerprints.items():
        item = replaced.get(path)
        expected = item.replacement if item is not None else original
        require(base.read_file(path, missing=True) == expected,
                'installed_source_or_preserved_dependency_changed')
    for item in changes:
        require(base.read_file(item.path) == item.replacement, 'installed_file_mismatch')
        info = item.path.stat()
        require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) ==
                (item.uid, item.gid, item.mode), 'installed_file_metadata_changed')
        if item.path.suffix == '.py':
            compile(item.replacement, str(item.path), 'exec')


def backup_sources(base, changes, backup):
    manifest = []
    for index, item in enumerate(changes):
        item.unchanged()
        if item.original is not None:
            with os.fdopen(os.open(backup/str(index), os.O_WRONLY | os.O_CREAT |
                                  os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
                stream.write(item.original)
                stream.flush()
                os.fsync(stream.fileno())
        manifest.append(dict(path=str(item.path), existed=item.original is not None,
            uid=item.uid, gid=item.gid, mode=item.mode,
            old_hash=digest(item.original) if item.original is not None else None,
            new_hash=digest(item.replacement)))
    with os.fdopen(os.open(backup/'files.json', os.O_WRONLY | os.O_CREAT |
                          os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
        json.dump(manifest, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(backup, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def apply_source_changes(base, changes, fingerprints, units, backup, *,
                         observe, invoke, record, guard):
    replaced = []
    stopped = False
    try:
        guard()
        base.check_fingerprints(fingerprints)
        backup_sources(base, changes, backup)
        guard()
        base.check_fingerprints(fingerprints)
        record({'state': 'stopping', 'stage': 'dashboard_and_services'})
        result = invoke(['/usr/bin/systemctl', 'stop', *units])
        require(result.returncode == 0, 'service_stop_failed')
        ensure_stopped(units, observe)
        stopped = True
        base.check_fingerprints(fingerprints)
        record({'state': 'installing', 'stage': 'audit_learning_and_dashboard_sources'})
        for item in changes:
            ensure_stopped(units, observe)
            item.unchanged()
            replaced.append(item)
            base.write_atomic(item, item.replacement)
        ensure_stopped(units, observe)
        verify_installed(base, changes, fingerprints)
        return [str(item.path) for item in changes]
    except BaseException:
        rollback = 'not_needed'
        if replaced:
            rollback = 'complete'
            try:
                ensure_stopped(units, observe)
                for item in reversed(replaced):
                    ensure_stopped(units, observe)
                    current = base.read_file(item.path, missing=True)
                    require(current in (item.original, item.replacement),
                            'concurrent_source_edit_during_rollback')
                    if current == item.original:
                        continue
                    if item.original is None:
                        item.path.unlink()
                    else:
                        base.write_atomic(item, item.original)
                ensure_stopped(units, observe)
                base.check_fingerprints(fingerprints)
            except BaseException:
                rollback = 'unverified'
        record({'state': 'blocked', 'stage': 'audit_source_maintenance',
                'rollback': rollback, 'all_services_observed_stopped': stopped,
                'restart_requested': False})
        raise


def execute(base, changes, fingerprints, units, state, helper, *, apply,
            manual_holding_external, admission, observe, invoke):
    if apply:
        require(manual_holding_external, 'manual_holding_external_acknowledgement_required')
        require(not (state/'attempted.json').exists(), 'attempt_already_recorded_review_status')
    admission()
    base.check_fingerprints(fingerprints)
    if not apply:
        return dict(state='plan_verified', changed_files=len(changes), source_installed=False,
                    service_stop_requested=False, broker_flat_verified=False, activation=False)
    helper.root_parents(state)
    state.mkdir(mode=0o700, exist_ok=True)
    helper.private_path(state, directory=True)
    helper.claim_attempt(state, helper.utc_now())
    backup = state/'backup'
    backup.mkdir(mode=0o700)
    helper.private_path(backup, directory=True)

    def record(value):
        helper.record_status(state, dict(value, source_commit=RELEASE['commit'],
            predecessor_commit=PRIOR_COMMIT, broker_flat_verified=False,
            ledger_repaired=False, manual_holding_external=True,
            activation=False, restart_requested=False, runtime_health_verified=False))

    record({'state': 'preparing', 'stage': 'source_backup'})
    installed = apply_source_changes(base, changes, fingerprints, units, backup,
        observe=observe, invoke=invoke, record=record, guard=admission)
    try:
        ensure_stopped(units, observe)
        verify_installed(base, changes, fingerprints)
    except BaseException:
        record({'state': 'blocked', 'stage': 'stopped_verification', 'source_installed': True,
                'rollback': 'not_attempted_after_verified_install', 'startup_review_pending': True})
        raise
    result = dict(state='audit_learning_source_installed_services_stopped',
                  source_installed=True, changed_files=len(installed), stopped_units=list(units),
                  broker_flat_verified=False, ledger_repaired=False,
                  historical_repair_pending=True, startup_review_pending=True,
                  dashboard_changed=True, activation=False, restart_requested=False,
                  runtime_health_verified=False)
    record(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--manual-holding-external', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    require(not args.apply or args.manual_holding_external,
            'manual_holding_external_acknowledgement_required')
    sys.dont_write_bytecode = True
    os.umask(0o077)
    helper = load_helper('_stopped_audit_stage', PACKAGE/'momentum_recovery/scheduled_deploy.py', SCHEDULED_BLOB)
    prep = load_helper('_stopped_audit_prep', PACKAGE/'momentum_recovery/prepare_maintenance.py', PREP_BLOB)
    history = helper.canonical(args.history)
    with ExitStack() as locks:
        for name in LOCKS:
            path = Path('/run/lock')/(name+'.lock')
            require(path.resolve(strict=False) == path, 'lock_path_unreviewed')
            handle = locks.enter_context(os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR |
                                              os.O_NOFOLLOW, 0o600), 'w'))
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                    not stat.S_IMODE(info.st_mode) & 0o022, 'private_lock_required')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not args.apply or not (STATE/'attempted.json').exists(),
                'attempt_already_recorded_review_status')
        _, prior_fingerprints = verify_prior_state(helper, PRIOR_STATE)
        prep.verify_timer_hold()
        ensure_stopped(PRIOR_UNITS, helper.observe_unit)
        directory = Path(tempfile.mkdtemp(prefix='kadima-stopped-audit-', dir='/opt'))
        helper.private_path(directory, directory=True)
        helper.root_parents(directory)
        release = directory/'release'
        manifest = helper.stage_release(history, release, RELEASE)
        helper.create_private(directory/'release-manifest.json', json.dumps(manifest, sort_keys=True).encode())
        installer = load_helper('_stopped_audit_installer', release/'trade_audit_update/install.py', INSTALLER_BLOB)
        base = installer.base
        user = pwd.getpwnam('ubuntu')
        changes, fingerprints = build_plan(installer, release, TARGET,
            PACKAGE/'momentum_recovery/reviewed_dashboard', (user.pw_uid, user.pw_gid))
        fingerprints.update(prior_fingerprints)
        # These credentials/configuration are preserved; they are never printed.
        fingerprints[base.TOKEN_FILE] = base.read_file(base.TOKEN_FILE)
        dashboard = base.process(DASHBOARD, base.GATEWAY.parent, user.pw_uid)
        require(dashboard[1][1:4] == ['-m', 'uvicorn', 'gateway:app'],
                'dashboard_entrypoint_unreviewed')

        def admission():
            prep.verify_timer_hold()
            ensure_stopped(PRIOR_UNITS, helper.observe_unit)
            require(base.process(DASHBOARD, base.GATEWAY.parent, user.pw_uid) == dashboard,
                    'dashboard_process_changed')
            base.check_fingerprints(fingerprints)

        def observe(name):
            prep.verify_timer_hold()
            return helper.observe_unit(name)

        if args.apply:
            print('STATUS: '+str(STATE/'status.json'), flush=True)
        result = execute(base, changes, fingerprints, UNITS, STATE, helper,
            apply=args.apply, manual_holding_external=args.manual_holding_external,
            admission=admission, observe=observe,
            invoke=lambda command: subprocess.run(command, capture_output=True, text=True, timeout=90))
        result.update(source_commit=RELEASE['commit'], staged_release=str(release),
                      status_path=str(STATE/'status.json') if args.apply else None)
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.apply:
            print('AUDIT + LEARNING + DASHBOARD SOURCES INSTALLED; ALL FIVE SERVICES LEFT STOPPED.')
            print('No broker call, DB change, ledger repair, restart or activation. Runtime verification remains pending.')
        else:
            print('PLAN ONLY. No installed source, service, database or broker change.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:', str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        print('No restart requested. Review maintenance status and services before another action.')
        raise SystemExit(1)
