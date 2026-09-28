"""Install only pinned PR19 sources and leave the stopped services stopped.

The operator keeps the external manual holding under their own management.
This is not flat-account admission, historical repair, a restart, or activation.
No broker reader, database connection, old installer main, or restart is called.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
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
STATE = Path('/var/lib/kadima-stopped-source-20260928')
RELEASE = {'commit': 'bd294a7048749258fbaa2cc786f20a441a2a8073',
           'tree': '1afbeca8a8658039b031817be65a11e47567e4c5'}
SCHEDULED_BLOB = '019f35decefe72f56cfb600b5321b2c014e48c6a'
PREP_BLOB = '363fe8b8c2e1dbcafca11f89afb2666c2a8f87f3'
INSTALLER_BLOB = '1a01595195c4440732b1a277fe75a2ecc7f058ea'
CORE_UNITS = ('kadima-universe-scanner.service', 'kadima.service')
OPTIONAL_UNITS = ('kadima-macro-watch.service', 'kadima-eod-watchdog.service')
LOCKS = ('kadima-stopped-source-install', 'kadima-stock-fast-install',
         'kadima-activation', 'kadima-session-auto-run', 'kadima-session-auto-install',
         'kadima-multi-position-install', 'kadima-auth-recovery', 'kadima-kill-reset',
         'kadima-contract-note-repair', 'kadima-mcx-live-install')


class Blocked(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def load_helper(name, path, expected):
    import hashlib
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
            'helper_path_unreviewed')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'helper_file_required')
        raw = stream.read(4_000_001)
    digest = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    require(len(raw) <= 4_000_000 and digest == expected, 'reviewed_helper_required')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def paused_controls(prep, token):
    prep.verify_timer_hold()
    controls = prep.snapshot(token)
    require(controls.get('auto_trade_enabled') is False and
            controls.get('stock_live_enabled') is False and
            controls.get('kill_latched') is False, 'paused_clear_controls_required')
    # A weekend API may be unrestored; its count is never flat-account proof.
    return controls


def selected_units(observe):
    units = []
    for name in CORE_UNITS + OPTIONAL_UNITS:
        data = observe(name)
        load, active = data.get('LoadState'), data.get('ActiveState')
        if name in OPTIONAL_UNITS and load == 'not-found':
            require(active == 'inactive', 'optional_unit_state_unknown')
            continue
        require(load in ('loaded', 'masked'), 'maintenance_unit_unavailable')
        require(active in ('active', 'inactive'), 'maintenance_unit_transition')
        if name in CORE_UNITS:
            require(active == 'active', 'core_service_must_be_running_for_admission')
        units.append(name)
    return tuple(units)


def ensure_stopped(units, observe):
    for name in units:
        data = observe(name)
        require(data.get('LoadState') in ('loaded', 'masked') and
                data.get('ActiveState') == 'inactive' and data.get('MainPID') == '0',
                'maintenance_service_not_stopped')


def verify_installed(base, changes, fingerprints):
    replaced = {item.path: item for item in changes}
    for path, original in fingerprints.items():
        item = replaced.get(path)
        expected = item.replacement if item is not None else original
        require(base.read_file(path, missing=True) == expected,
                'installed_source_or_preserved_dependency_changed')
    for item in changes:
        require(base.read_file(item.path) == item.replacement, 'installed_file_mismatch')
        metadata = item.path.stat()
        require((metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) ==
                (item.uid, item.gid, item.mode), 'installed_file_metadata_changed')
        compile(item.replacement, str(item.path), 'exec')


def backup_sources(base, changes, backup):
    """Back up only files to be replaced; never copy or open the database."""
    records = []
    for index, item in enumerate(changes):
        item.unchanged()
        if item.original is not None:
            with os.fdopen(os.open(backup/str(index), os.O_WRONLY | os.O_CREAT |
                                  os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
                stream.write(item.original)
                stream.flush()
                os.fsync(stream.fileno())
        records.append(dict(path=str(item.path), existed=item.original is not None,
                            uid=item.uid, gid=item.gid, mode=item.mode,
                            old_hash=base.digest(item.original) if item.original is not None else None,
                            new_hash=base.digest(item.replacement)))
    with os.fdopen(os.open(backup/'files.json', os.O_WRONLY | os.O_CREAT |
                          os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
        json.dump(records, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(backup, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_source_changes(base, changes, fingerprints, units, backup, *,
                         observe, invoke, record, guard):
    """No restart on success, abort or rollback. Callers own admission locks."""
    replaced = []
    stopped = False
    try:
        guard()
        base.check_fingerprints(fingerprints)
        backup_sources(base, changes, backup)
        guard()
        base.check_fingerprints(fingerprints)
        record({'state': 'stopping', 'stage': 'services'})
        result = invoke(['/usr/bin/systemctl', 'stop', *units])
        require(result.returncode == 0, 'service_stop_failed')
        ensure_stopped(units, observe)
        stopped = True
        base.check_fingerprints(fingerprints)
        record({'state': 'installing', 'stage': 'sources'})
        for item in changes:
            ensure_stopped(units, observe)
            item.unchanged()
            replaced.append(item)
            base.write_atomic(item, item.replacement)
        ensure_stopped(units, observe)
        verify_installed(base, changes, fingerprints)
        return [str(item.path) for item in changes]
    except BaseException:
        # A failed stop can be partial. Never write source unless every selected
        # service was observed stopped, and never restart to make recovery pass.
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
        record({'state': 'blocked', 'stage': 'source_maintenance',
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
        return dict(state='plan_verified', changed_files=len(changes),
                    source_installed=False, service_stop_requested=False,
                    broker_flat_verified=False, activation=False)
    helper.root_parents(state)
    state.mkdir(mode=0o700, exist_ok=True)
    helper.private_path(state, directory=True)
    helper.claim_attempt(state, helper.utc_now())
    backup = state/'backup'
    backup.mkdir(mode=0o700)
    helper.private_path(backup, directory=True)

    def record(value):
        helper.record_status(state, dict(value, source_commit=RELEASE['commit'],
            broker_flat_verified=False, ledger_repaired=False,
            manual_holding_external=True, activation=False, restart_requested=False))

    record({'state': 'preparing', 'stage': 'source_backup'})
    installed = apply_source_changes(base, changes, fingerprints, units, backup,
        observe=observe, invoke=invoke, record=record, guard=admission)
    # After the API is stopped, source and service observations are sufficient
    # only for this maintenance result, never for startup/trading readiness.
    try:
        ensure_stopped(units, observe)
        verify_installed(base, changes, fingerprints)
    except BaseException:
        # The source transaction already returned after its own verification.
        # Preserve installed source if a subsequent observation changed; never
        # roll back code that might have been started by another operator.
        record({'state': 'blocked', 'stage': 'stopped_verification',
                'source_installed': True, 'rollback': 'not_attempted_after_verified_install',
                'startup_review_pending': True})
        raise
    result = dict(state='source_installed_services_stopped', source_installed=True,
                  changed_files=len(installed), stopped_units=list(units),
                  broker_flat_verified=False, ledger_repaired=False,
                  historical_repair_pending=True, startup_review_pending=True,
                  dashboard_changed=False, activation=False, restart_requested=False)
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
    helper = load_helper('_stopped_stage', PACKAGE/'momentum_recovery/scheduled_deploy.py', SCHEDULED_BLOB)
    prep = load_helper('_stopped_prep', PACKAGE/'momentum_recovery/prepare_maintenance.py', PREP_BLOB)
    history = helper.canonical(args.history)
    with ExitStack() as locks:
        for name in LOCKS:
            path = Path('/run/lock')/(name+'.lock')
            require(path.resolve(strict=False) == path, 'lock_path_unreviewed')
            handle = locks.enter_context(os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR |
                                              os.O_NOFOLLOW, 0o600), 'w'))
            metadata = os.fstat(handle.fileno())
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and
                    metadata.st_nlink == 1 and not metadata.st_mode & 0o022,
                    'lock_owner_or_permissions_unreviewed')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not args.apply or not (STATE/'attempted.json').exists(),
                'attempt_already_recorded_review_status')
        directory = Path(tempfile.mkdtemp(prefix='kadima-stopped-source-', dir='/opt'))
        helper.private_path(directory, directory=True)
        helper.root_parents(directory)
        release = directory/'release'
        manifest = helper.stage_release(history, release, RELEASE)
        helper.create_private(directory/'release-manifest.json', json.dumps(manifest, sort_keys=True).encode())
        installer = load_helper('_stopped_reviewed_installer', release/'momentum_recovery/install.py', INSTALLER_BLOB)
        base = installer.base
        user = pwd.getpwnam('ubuntu')
        changes, fingerprints = base.backend_plan(release, TARGET, (user.pw_uid, user.pw_gid))
        units = selected_units(helper.observe_unit)
        identity = prep.engine_identity()
        engine = base.process(base.ENGINE, TARGET, user.pw_uid)
        scanner = base.process(base.SCANNER, TARGET, user.pw_uid)
        scanner_args = [arg for arg in scanner[1][1:] if arg not in ('-u', '-B')]
        require(scanner_args == ['-m', 'kadima.stock_universe_scanner'], 'scanner_entrypoint_unreviewed')
        raw_token = prep.read_regular(prep.TOKEN_FILE, 100_000)
        token = json.loads(raw_token).get('api_token')
        require(isinstance(token, str) and 0 < len(token) < 8192, 'control_credentials_unavailable')

        def admission():
            require(prep.engine_identity() == identity and
                    base.process(base.ENGINE, TARGET, user.pw_uid) == engine and
                    base.process(base.SCANNER, TARGET, user.pw_uid) == scanner,
                    'engine_or_scanner_changed')
            require(prep.read_regular(prep.TOKEN_FILE, 100_000) == raw_token,
                    'control_credentials_changed')
            paused_controls(prep, token)
            base.check_fingerprints(fingerprints)

        def observe(name):
            # Repeat held timer/worker checks after shutdown as well.
            prep.verify_timer_hold()
            return helper.observe_unit(name)

        if args.apply:
            print('STATUS: '+str(STATE/'status.json'), flush=True)
        result = execute(base, changes, fingerprints, units, STATE, helper,
            apply=args.apply, manual_holding_external=args.manual_holding_external,
            admission=admission, observe=observe,
            invoke=lambda command: subprocess.run(command, capture_output=True, text=True, timeout=90))
        result.update(source_commit=RELEASE['commit'], staged_release=str(release),
                      status_path=str(STATE/'status.json') if args.apply else None)
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.apply:
            print('PR19 SOURCE INSTALLED; SERVICES LEFT STOPPED. No broker order, ledger repair, restart or activation.')
            print('Historical repair, UI review and reviewed startup remain pending. Manual holding stays external.')
        else:
            print('PLAN ONLY. No runtime source, service, database or broker change.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:', str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        print('No restart requested. Review maintenance status and services before another action.')
        raise SystemExit(1)
