"""Read-only account-bound readiness inspection, including unrestored API state.

This is not repair admission or a deployment permit. No apply mode exists.
Only the existing broker login and position/order-book reader can be invoked.
An optional private archive contains exactly the two installed UI source files.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import tarfile
import tempfile

PACKAGE = Path(__file__).resolve().parents[1]
TARGET = Path('/home/ubuntu/kadima-py')
PINS = {
    'prepare_maintenance.py': '363fe8b8c2e1dbcafca11f89afb2666c2a8f87f3',
    'reconcile_contract_note.py': '931fe21de19aa8ef21c46c713836780363fe1a48',
    'install.py': '1a01595195c4440732b1a277fe75a2ecc7f058ea',
}
UI = {'gateway.py': Path('/opt/kadima-dashboard/gateway.py'),
      'dashboard.html': Path('/var/www/html/dashboard.html')}
LOCKS = ('kadima-stock-fast-install', 'kadima-activation', 'kadima-session-auto-run',
         'kadima-session-auto-install', 'kadima-multi-position-install',
         'kadima-auth-recovery', 'kadima-kill-reset', 'kadima-contract-note-repair')


class Blocked(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def blob(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def load_reviewed(name):
    path = PACKAGE/'momentum_recovery'/name
    require(path.resolve() == path and not path.is_symlink(), 'review_helper_path_changed')
    raw = path.read_bytes()
    require(blob(raw) == PINS[name], 'review_helper_changed')
    spec = importlib.util.spec_from_file_location('_readiness_'+name.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_evidence(path, prep, owner):
    raw = prep.read_regular(path, 100_000)
    info = path.stat()
    require(info.st_uid in (0, owner) and not info.st_mode & 0o077 and
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'private_reviewed_evidence_required')
    return raw


def export_ui(prep, *, parent=Path('/tmp'), owner=None):
    # Fixed paths only; never include config, environment, ledger, audit logs,
    # broker credentials or contract-note evidence as additional archive files.
    sources = {name: prep.read_regular(path) for name, path in UI.items()}
    report = dict(observed_at=datetime.now(timezone.utc).isoformat(),
                  blobs={name: blob(raw) for name, raw in sources.items()})
    directory = Path(tempfile.mkdtemp(prefix='kadima-ui-review-', dir=parent))
    directory.chmod(0o700)
    path = directory/'dashboard-source.tar.gz'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, raw in {**sources, 'manifest.json': json.dumps(report, sort_keys=True).encode()}.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(raw), 0o600
            archive.addfile(item, io.BytesIO(raw))
    if owner is not None:
        # Let the authenticated SSH operator retrieve the private review file.
        # Hand over the directory only after the archive has been closed.
        os.chown(path, *owner)
        os.chown(directory, *owner)
    return dict(path=str(path), blobs=report['blobs'])


def local_state(db, note, evidence):
    require(db.is_absolute() and db.resolve() == db and db.is_file(), 'canonical_existing_db_required')
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=2) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        result = {
            'unresolved_positions': connection.execute(
                "SELECT COUNT(*) FROM positions WHERE status IS NULL OR status!='closed'").fetchone()[0],
            'unresolved_orders': connection.execute(
                "SELECT COUNT(*) FROM execution_orders WHERE state IS NULL OR state!='terminal'").fetchone()[0],
        }
        try:
            note.inspect(connection, evidence)
            result['reviewed_historical_case_matches'] = True
        except Exception:
            result['reviewed_historical_case_matches'] = False
        return result


def read_broker(note, python, env, user, invoke=subprocess.run):
    child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0',
                     KADIMA_FAST_MOMENTUM_LIVE='0', KADIMA_CAS_LIVE='0')
    try:
        result = invoke([python, '-B', '-c', note.BROKER_READ], cwd=TARGET, env=child_env,
                        user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                        capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        note.broker_failure('transport', 'child_timeout', 'TimeoutExpired', checkpoint='check_only')
    except OSError as exc:
        note.broker_failure('transport', 'child_unavailable', type(exc).__name__, checkpoint='check_only')
    receipt = note.broker_receipt(result, datetime.now(timezone.utc), 'check_only')
    # The diagnostic parent never emits arbitrary child output/receipt keys.
    return {key: receipt[key] for key in
            ('started_at', 'verified_at', 'position_rows', 'orderBook_rows', 'flat')}


def inspect_runtime(prep, note, base, evidence, user, report):
    _, fingerprints = base.backend_plan(PACKAGE, TARGET, (user.pw_uid, user.pw_gid))
    pid, argv, env = base.process(base.ENGINE, TARGET, user.pw_uid)
    identity = note.process_identity(pid)
    note.account_matches(evidence, env)
    note.reviewed_sources(fingerprints)
    require(TARGET/'kadima/session.py' in fingerprints and TARGET/'kadima/config.py' in fingerprints,
            'broker_import_graph_unverified')
    helper = base.load_helper(PACKAGE/'executor_fix/install.py', base.HELPER)
    python = helper.engine_interpreter(Path('/proc')/pid, TARGET)
    token_raw = prep.read_regular(prep.TOKEN_FILE, 100_000)
    token = json.loads(token_raw).get('api_token')
    require(isinstance(token, str) and 0 < len(token) < 8192, 'api_credential_unavailable')

    def guard():
        current_pid, current_argv, current_env = base.process(base.ENGINE, TARGET, user.pw_uid)
        require((current_pid, current_argv, current_env) == (pid, argv, env) and
                note.process_identity(pid) == identity, 'engine_changed_during_read')
        base.check_fingerprints(fingerprints)
        require(prep.read_regular(prep.TOKEN_FILE, 100_000) == token_raw, 'control_credentials_changed')
        prep.verify_timer_hold()
        state = prep.snapshot(token)
        require(not state['auto_trade_enabled'] and not state['stock_live_enabled'] and
                not state['kill_latched'], 'paused_clear_controls_required')
        return state

    report['controls'] = guard()
    report['engine_started_at'] = identity['started_at'].isoformat()
    report['source_admission_verified'] = True
    report['local'] = local_state(Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db', note, evidence)
    child_env = dict(env, KADIMA_NOTE_ACCOUNT_SHA256=evidence['account_sha256'])
    receipt = read_broker(note, python, child_env, user)
    report['controls'] = guard()
    report['broker'] = receipt
    report['broker_flat_observed'] = True
    # This observation never changes the stricter repair/API restoration gates.
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--export-dashboard', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    prep, note, installer = (load_reviewed(name) for name in PINS)
    user = pwd.getpwnam('ubuntu')
    report = dict(timestamp=datetime.now(timezone.utc).isoformat(), action='read_only_broker_readiness',
                  deployment_performed=False, deployment_authorized=False,
                  service_stop_requested=False, broker_flat_observed=False)
    with ExitStack() as stack:
        for path in [Path('/run/lock')/(name+'.lock') for name in LOCKS]+[
                Path('/run/kadima-maintenance-prepare.lock')]:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stream = stack.enter_context(os.fdopen(fd, 'w'))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            prep.verify_timer_hold()
            if args.export_dashboard:
                report['dashboard_review'] = export_ui(prep, owner=(user.pw_uid, user.pw_gid))
            raw = private_evidence(args.evidence, prep, user.pw_uid)
            reviewed = note.evidence(json.loads(raw), datetime.now(timezone.utc))
            inspect_runtime(prep, note, installer.base, reviewed, user, report)
            require(private_evidence(args.evidence, prep, user.pw_uid) == raw, 'reviewed_evidence_changed')
        except BaseException as exc:
            # Detailed broker diagnostics have already been filtered by the
            # pinned reader. Never echo arbitrary SDK/SQL/source error messages.
            report['broker_flat_observed'] = False
            report['blocked'] = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
            print(json.dumps(report, indent=2, sort_keys=True))
            raise
    print(json.dumps(report, indent=2, sort_keys=True))
    print('READ ONLY: no repair, deployment, service stop, order action or activation. '
          'Broker books are an observation, not a deployment permit.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:', str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        raise SystemExit(1)
