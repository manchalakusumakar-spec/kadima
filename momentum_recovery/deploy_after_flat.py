"""Operator-run, pinned repair -> momentum -> audit rollout after account flatness.

This is a separate manual entrypoint. It does not alter the scheduled runner's
clock window, existing repair admission, source allowlists, or execution gates.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile


PACKAGE = Path(__file__).resolve().parents[1]
HELPER_BLOB = '019f35decefe72f56cfb600b5321b2c014e48c6a'
BUNDLE = Path('/opt/kadima-deploy-after-flat-20260927')
STATE = Path('/var/lib/kadima-deploy-after-flat-20260927')
LOCK = Path('/run/kadima-deploy-after-flat.lock')
TIMERS = ('kadima-session-auto.timer', 'kadima-deploy-20260928.timer')
SERVICES = ('kadima-session-auto.service', 'kadima-deploy-20260928.service')


def load_reviewed_helper():
    path = PACKAGE/'momentum_recovery/scheduled_deploy.py'
    raw = path.read_bytes()
    digest = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    if digest != HELPER_BLOB:
        raise RuntimeError('reviewed_deployment_helper_required')
    spec = importlib.util.spec_from_file_location('_manual_deploy_helpers', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def check_competing_services(helper):
    for name in SERVICES:
        data = helper.observe_unit(name)
        helper.require(data.get('ActiveState') == 'inactive', 'competing_service_active:'+name)
        helper.require(data.get('LoadState') in ('not-found', 'loaded', 'masked'),
                       'competing_service_unknown:'+name)
        if data.get('LoadState') != 'not-found':
            helper.require(data.get('MainPID') == '0', 'competing_service_process:'+name)


def check_timers_disabled(helper):
    check_competing_services(helper)
    for name in TIMERS:
        data = helper.observe_unit(name)
        helper.require(data.get('ActiveState') == 'inactive', 'competing_timer_active:'+name)
        if data.get('LoadState') == 'not-found':
            continue
        helper.require(data.get('LoadState') in ('loaded', 'masked') and
                       data.get('UnitFileState') in ('disabled', 'masked', 'masked-runtime'),
                       'competing_timer_not_disabled:'+name)
    helper.check_session_auto()


def hold_entry_timers(helper, invoke):
    # Called only after the pinned broker check succeeds. Never stop a running
    # deployment/session worker or the engine to make this check pass.
    check_competing_services(helper)
    for name in TIMERS:
        data = helper.observe_unit(name)
        helper.require(data.get('LoadState') in ('not-found', 'loaded', 'masked'),
                       'timer_state_unknown:'+name)
        if data.get('LoadState') == 'not-found':
            continue
        helper.require(data.get('ActiveState') in ('active', 'inactive'),
                       'timer_transition_in_progress:'+name)
        if data.get('LoadState') == 'loaded':
            result = invoke(['/usr/bin/systemctl', 'disable', '--now', name])
            helper.require(result.returncode == 0, 'timer_hold_failed:'+name)
    check_timers_disabled(helper)


def prepare_bundle(helper, history, evidence, bundle=BUNDLE, state=STATE):
    history, evidence = helper.canonical(history), helper.canonical(evidence)
    info = evidence.lstat()
    helper.require(info.st_uid in (0, pwd.getpwnam('ubuntu').pw_uid) and
                   not info.st_mode & 0o077 and stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                   'private_reviewed_evidence_required')
    raw = helper.read_regular(evidence, 100_000)
    helper.require(isinstance(json.loads(raw), dict), 'evidence_object_required')
    for path in (bundle, state):
        helper.root_parents(path)
    if bundle.exists():
        helper.verify_bundle(bundle)
        helper.require(helper.read_regular(bundle/'evidence.json') == raw, 'reviewed_evidence_changed')
        helper.require(helper.blob_hash(helper.read_regular(bundle/'scheduled_deploy.py')) == HELPER_BLOB,
                       'bundle_helper_changed')
    else:
        helper.require(not state.exists(), 'orphan_attempt_state_review_required')
        temporary = Path(tempfile.mkdtemp(prefix='.kadima-flat-', dir=bundle.parent))
        try:
            (temporary/'releases').mkdir(mode=0o700)
            manifests = {name: helper.stage_release(history, temporary/'releases'/name, pin)
                         for name, pin in helper.RELEASES.items()}
            raw_helper = helper.read_regular(PACKAGE/'momentum_recovery/scheduled_deploy.py')
            helper.create_private(temporary/'scheduled_deploy.py', raw_helper)
            helper.create_private(temporary/'evidence.json', raw)
            # Preserve the original verified bundle schema; this metadata does
            # not arm a timer or authorize the scheduled run entrypoint.
            manifest = dict(version=1, scheduled_at=helper.SCHEDULED_AT.isoformat(), releases=manifests,
                            runner_sha256=hashlib.sha256(raw_helper).hexdigest(),
                            evidence_sha256=hashlib.sha256(raw).hexdigest())
            helper.create_private(temporary/'manifest.json', json.dumps(manifest, sort_keys=True).encode())
            helper.verify_bundle(temporary)
            helper.reviewed_repair(temporary).evidence(json.loads(raw), helper.utc_now())
            os.rename(temporary, bundle)
            helper.sync_directory(bundle.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    if not state.exists():
        state.mkdir(mode=0o700)
        helper.sync_directory(state.parent)
    helper.private_path(state, directory=True)
    return bundle, state


def broker_preflight_command(bundle):
    return ['/usr/bin/python3', '-I', '-B',
            str(bundle/'releases/repair/momentum_recovery/reconcile_contract_note.py'),
            '--evidence', str(bundle/'evidence.json'), '--broker-check-only']


def execute(helper, bundle, state, *, apply, invoke):
    # A prior attempt can have committed a repair even if later steps failed.
    # Never silently skip/retry it or restore the execution database.
    helper.require(not (state/'attempted.json').exists(), 'attempt_already_recorded_review_status')
    latest = {'state': 'checking', 'stage': 'admission', 'activation': False}
    def record(value):
        latest.clear()
        latest.update(value)
        helper.record_status(state, dict(value, mode='manual', activation=False))
    def admission(*, held=False):
        helper.verify_bundle(bundle)
        helper.check_controls()
        check_competing_services(helper)
        if held:
            check_timers_disabled(helper)
    try:
        record(latest.copy())
        admission()
        record({'state': 'checking', 'stage': 'broker_flat_preflight'})
        result = invoke(broker_preflight_command(bundle))
        helper.require(result.returncode == 0, 'broker_or_historical_admission_failed_no_deployment')
        if not apply:
            record({'state': 'plan_verified', 'stage': 'complete'})
            print('PLAN VERIFIED: broker/local historical admission checked; nothing installed or activated.')
            return
        # The operator requested manual deployment after closure, superseding a
        # competing timer. Both timers remain disabled after success/failure.
        record({'state': 'holding', 'stage': 'entry_timers'})
        hold_entry_timers(helper, invoke)
        admission(held=True)
        helper.claim_attempt(state, helper.utc_now())
        helper.run_stages(helper.stage_commands(bundle), invoke, record,
                          lambda: admission(held=True))
        record({'state': 'checking', 'stage': 'final_verification'})
        admission(held=True)
        for unit in ('kadima.service', 'kadima-universe-scanner.service', 'kadima-dashboard.service'):
            helper.require(helper.observe_unit(unit).get('ActiveState') == 'active',
                           'final_service_not_active:'+unit)
        helper.check_controls(final=True)
        helper.final_database_check(bundle)
        record({'state': 'verified', 'stage': 'complete'})
        print('DEPLOYMENT VERIFIED: historical repair, momentum, trade audit and price history.')
        print('Auto Trade and Stock Live remain paused; both entry/deployment timers remain disabled.')
        print('This does not deploy the separate CAS, dynamic-stop, index-scalper or memory branches.')
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, helper.Blocked) else type(exc).__name__
        record(dict(latest, state='blocked', reason=reason))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    helper = load_reviewed_helper()
    helper.require(os.geteuid() == 0, 'sudo_required')
    helper.root_parents(LOCK)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise helper.Blocked('manual_deployment_already_running') from None
        bundle, state = prepare_bundle(helper, args.history, args.evidence)
        print('STATUS: '+str(state/'status.json'), flush=True)
        execute(helper, bundle, state, apply=args.apply,
                invoke=lambda command: subprocess.run(command, cwd=bundle, check=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Child repair/installer tools emit their existing sanitized diagnostics.
        print('BLOCKED:', str(exc) if type(exc).__name__ == 'Blocked' else type(exc).__name__)
        raise SystemExit(1)
