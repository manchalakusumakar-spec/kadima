"""One-time, operator-approved fresh-session recovery before a guarded kill reset.

Outside the arming window only. Sources, paused controls, local and fresh broker
books are checked first. Only the engine is restarted, with the kill still on;
the existing reset endpoint then performs its own in-process reconciliation.
No source/config patch, direct order, manual DB edit, auto resume or login loop.
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
import re
import subprocess
import sys
import time

ROOT = Path('/home/ubuntu/kadima-py')
SCHEDULER = Path('/opt/kadima-session-auto')
TIMER = 'kadima-session-auto.timer'
JOB = 'kadima-session-auto.service'
PINNED = {
    'stock_fast_update/install.py': '587e138396139fc3abc6407b7329d686676df9a3',
    'executor_fix/install.py': 'e3f8970bcaeedfc14fd8f4ee295df53f23ec7e61',
    'session_auto/install.py': '3d44e27376a4ebf26123b29d762d19125add8635',
    'multi_position_update/install.py': '9f57039339dcc1af92b158695b84832597796807',
    'multi_position_update/patch.py': '870c2c7ea59cf558efbff14fe340b09c3e3832c4',
    'multi_position_update/reset_kill.py': '2769fbfe49cd8cc7a10634203bf225317cec1195',
    'session_auto/gate.py': '5633eba4177f6ade457d6bdd32ec4e154f27e68a',
}
BROKER_CODES = {'AG8001', 'AG8002', 'AG8003', 'AB8050', 'AB8051',
                'AB1000', 'AB1004', 'AB1006', 'AB1007', 'AB1010', 'AB1011'}


class Blocked(Exception):
    """Only authored diagnostics may be printed."""


def digest(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def load_pinned(package, relative):
    path = package / relative
    if path.is_symlink() or path.resolve() != path:
        raise Blocked('noncanonical_recovery_helper')
    if digest(path.read_bytes()) != PINNED[relative]:
        raise Blocked('reviewed_recovery_helper_required')
    name = '_recovery_' + relative.replace('/', '_').replace('.', '_')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fresh_preflight_code(helper, broker):
    code = helper.broker_preflight_code(broker.PREFLIGHT)
    old = "code = str(r.get('errorcode') or '') if isinstance(r,dict) else ''"
    if code.count(old) != 1 or code.count('SESSION.ensure()') != 1:
        raise Blocked('broker_preflight_shape_changed')
    code = code.replace(old, "code = str(r.get('errorcode') or r.get('errorCode') or '') if isinstance(r,dict) else ''")
    code = code.replace('SESSION.ensure()', 'SESSION.ensure(force=True)')
    compile(code, 'fresh_broker_read_only_preflight', 'exec')
    return code


def broker_failure(exc, broker):
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'fresh_broker_check_timed_out'
    for line in (getattr(exc, 'stdout', '') or '').splitlines():
        if not line.startswith('KADIMA_DIAGNOSTIC '):
            continue
        try:
            data = json.loads(line[len('KADIMA_DIAGNOSTIC '):])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if (isinstance(data.get('stage'), str) and isinstance(data.get('reason'), str)
                and data['stage'] in broker.STAGES and data['reason'] in broker.REASONS):
            code = data.get('broker_code')
            return ':'.join((data['stage'], data['reason'],
                             code if isinstance(code, str) and code in BROKER_CODES else 'NO_RECOGNIZED_CODE'))
    return 'fresh_broker_check_failed_private_details_suppressed'


def require_same_controls(reset, client, expected):
    observed = reset.snapshot(client)
    if observed != expected:
        raise Blocked('controls_changed_during_session_recovery')
    if observed[0]['kill_latched'] is not True:
        raise Blocked('kill_must_remain_latched_until_guarded_reset')
    return observed


def recover(*, helper, reset, client, expected, check, fresh_flat,
            wait_engine, restore_safe, apply):
    """Injected I/O; one restart and at most one reset POST, never an order retry."""
    check()
    if expected[0]['kill_latched'] is False:
        return 'ALREADY CLEAR: no session restart or reset sent; run the guarded upgrade separately.'
    require_same_controls(reset, client, expected)
    if not apply:
        return 'PLAN ONLY: no login, reset, source change or service action.'

    # Do not stop position management until a fresh broker read also proves flat.
    fresh_flat()
    print('FRESH BROKER CHECK: new-login position and order books verified flat. No orders submitted.', flush=True)
    check()
    require_same_controls(reset, client, expected)
    timer_touched = False
    reset_result = None
    try:
        timer_touched = True
        helper.command(['systemctl', 'stop', TIMER, JOB], timeout=180)
        for unit in (TIMER, JOB):
            if helper.unit_state(unit).get('ActiveState') != 'inactive':
                raise Blocked('session_scheduler_not_stopped')
        check()
        require_same_controls(reset, client, expected)
        print('ENGINE SESSION: restarting only the verified engine; kill stays ON and both auto intents stay OFF.', flush=True)
        helper.command(['systemctl', 'restart', helper.ENGINE], timeout=90)
        wait_engine()
        require_same_controls(reset, client, expected)
        print('GUARDED RESET: fresh in-engine reconciliation; one reset attempt only.', flush=True)
        reset_result = reset.reset_once(client, apply=True)
        if not reset_result.startswith('RESET VERIFIED:'):
            raise Blocked('guarded_reset_not_confirmed')
        if not restore_safe(True):
            raise Blocked('post_reset_state_changed')
    finally:
        if timer_touched:
            # A lost/reset response is never followed by another write. Restore
            # only after fresh paused controls, local-flat and source checks.
            try:
                safe = restore_safe(reset_result is not None)
            except Exception:
                safe = False
            if safe:
                try:
                    helper.command(['systemctl', 'start', TIMER], timeout=30)
                    if helper.unit_state(TIMER).get('ActiveState') != 'active':
                        raise Blocked('timer_restore_unconfirmed')
                except Exception:
                    print('TIMER RESTORATION UNCONFIRMED: inspect service states; do not resume manually.', flush=True)
                    raise
                print('MARKET-DAY TIMER: restored active; no auto-resume request sent.', flush=True)
            else:
                print('TIMER REMAINS STOPPED: current state could not be verified; inspect before recovery.', flush=True)
                if sys.exc_info()[0] is None:
                    raise Blocked('timer_restore_requires_verified_state')
    return reset_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Blocked('sudo_required')
    package = Path(__file__).resolve().parents[1]
    reset = load_pinned(package, 'multi_position_update/reset_kill.py')
    reset.outside_arming_window()
    handles = []
    try:
        # Include the runner lock: a concurrent human --enable/--resume-today
        # must not race this operation. The timer retries after this exits.
        for name in ('kadima-stock-fast-install', 'kadima-activation',
                     'kadima-session-auto-install', 'kadima-multi-position-install',
                     'kadima-kill-reset', 'kadima-auth-recovery', 'kadima-session-auto-run'):
            fd = os.open('/run/lock/' + name + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, 'w'); handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('another_maintenance_or_session_check_is_running') from None

        helper = load_pinned(package, 'stock_fast_update/install.py')
        broker = load_pinned(package, 'executor_fix/install.py')
        session = load_pinned(package, 'session_auto/install.py')
        multi = load_pinned(package, 'multi_position_update/install.py')
        patcher = load_pinned(package, 'multi_position_update/patch.py')
        gate = load_pinned(package, 'session_auto/gate.py')
        original = session.patch_executor(helper.read_file(package / 'kadima/auto_executor.py'))
        updated = patcher.patch_executor(original)
        user = pwd.getpwnam('ubuntu')
        _, fingerprints = multi.sources(package, ROOT, helper, original, updated,
                                         (user.pw_uid, user.pw_gid))
        for installed, reviewed in ((ROOT / 'kadima/market_session_gate.py', package / 'session_auto/gate.py'),
                                    (SCHEDULER / 'gate.py', package / 'session_auto/gate.py'),
                                    (SCHEDULER / 'runner.py', package / 'session_auto/runner.py')):
            raw = helper.read_file(installed)
            if raw != helper.read_file(reviewed):
                raise Blocked('reviewed_scheduler_required')
            fingerprints[installed] = raw
        for path in (gate.POLICY, session.ENV, reset.AUTH):
            fingerprints[path] = helper.read_file(path)
        if fingerprints[session.ENV].decode() != session.ENV_CONTENT:
            raise Blocked('session_environment_changed')
        policy = gate.validate_policy(json.loads(fingerprints[gate.POLICY]))
        multi.require_outside_session(gate, policy)
        reset.verify_sources()

        pid, argv, env = helper.process(helper.ENGINE, ROOT, user.pw_uid)
        python = broker.engine_interpreter(Path('/proc') / pid, ROOT)
        spid, _, senv = helper.process(helper.SCANNER, ROOT, user.pw_uid)
        for process_env in (env, senv):
            if any(process_env.get(k) != '1' for k in ('KADIMA_CELESTIAL_ENABLED',
                    'KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_SESSION_AUTOTRADE')):
                raise Blocked('session_runtime_flags_not_confirmed')
        data_dir = Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))
        if data_dir != Path(senv.get('KADIMA_DATA_DIR', '/var/lib/kadima')):
            raise Blocked('shared_data_directory_required')
        if not data_dir.is_absolute() or data_dir.resolve() != data_dir:
            raise Blocked('noncanonical_data_directory')
        if env.get('KADIMA_FORCE_START', '0') != '0':
            raise Blocked('forced_start_requires_review')
        if (data_dir / 'KILL').exists():
            raise Blocked('separate_file_kill_present_not_released')
        db = data_dir / 'kadima.db'
        client = reset.Client()
        expected = reset.snapshot(client)
        helper.command(['systemctl', 'is-enabled', '--quiet', TIMER])
        if helper.unit_state(TIMER).get('ActiveState') != 'active':
            raise Blocked('existing_timer_must_be_active')
        protected_env = {k: v for k, v in env.items()
                         if k.startswith(('ANGEL_', 'KADIMA_', 'ORDER_PLACEMENT'))}

        def common():
            reset.outside_arming_window()
            multi.require_outside_session(gate, policy)
            helper.check_fingerprints(fingerprints)
            helper.local_flat(db)
            if (data_dir / 'KILL').exists():
                raise Blocked('separate_file_kill_present_not_released')
            if helper.process(helper.SCANNER, ROOT, user.pw_uid)[0] != spid:
                raise Blocked('scanner_changed_during_recovery')

        def check():
            common()
            if helper.process(helper.ENGINE, ROOT, user.pw_uid)[0] != pid:
                raise Blocked('engine_changed_before_recovery')

        code = fresh_preflight_code(helper, broker)
        child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0',
                         KADIMA_FAST_MOMENTUM_LIVE='0', KADIMA_CAS_LIVE='0')
        def fresh_flat():
            check()
            try:
                result = helper.command([python, '-B', '-c', code], cwd=ROOT, env=child_env,
                    user=user.pw_uid, group=user.pw_gid, extra_groups=[user.pw_gid], timeout=60)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise Blocked(broker_failure(exc, broker)) from None
            if 'KADIMA_PREFLIGHT_OK' not in result.stdout.splitlines():
                raise Blocked('fresh_broker_flat_not_confirmed')
            helper.local_flat(db)

        def running_identity(require_new=True):
            new_pid, new_argv, new_env = helper.process(helper.ENGINE, ROOT, user.pw_uid)
            if (require_new and new_pid == pid) or new_argv != argv:
                raise Blocked('new_engine_identity_not_confirmed')
            if {k: v for k, v in new_env.items() if k.startswith(('ANGEL_', 'KADIMA_', 'ORDER_PLACEMENT'))} != protected_env:
                raise Blocked('engine_settings_changed_during_restart')

        def wait_engine():
            # Only GETs are retried while the API binds; the reset POST is not.
            for attempt in range(30):
                common()
                try:
                    running_identity()
                    require_same_controls(reset, client, expected)
                    return
                except (OSError, Blocked, reset.Blocked, helper.Blocked):
                    if attempt == 29:
                        raise Blocked('restarted_engine_readiness_unconfirmed') from None
                    time.sleep(1)

        def restore_safe(reset_confirmed):
            common()
            running_identity(require_new=reset_confirmed)
            cfg, stock = reset.snapshot(client)
            expected_cfg = dict(expected[0])
            if reset_confirmed:
                expected_cfg.update(kill_latched=False, auto_trade_enabled=False,
                                    revision=expected_cfg['revision'] + 1)
            return cfg == expected_cfg and stock == expected[1]

        result = recover(helper=helper, reset=reset, client=client, expected=expected,
                         check=check, fresh_flat=fresh_flat, wait_engine=wait_engine,
                         restore_safe=restore_safe, apply=args.apply)
        print(result)
        print('SOURCE, LOT CAP, RISK SETTINGS, SCHEDULE POLICY AND FILE KILL: unchanged.')
    finally:
        for handle in reversed(handles):
            handle.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never surface captured provider output, credentials or full environments.
        value = str(exc)
        reason = value if re.fullmatch(r'[a-z_]+(?::[a-z_A-Z0-9]+){0,2}', value) else type(exc).__name__
        print('BLOCKED:', reason)
        print('No automatic retry or auto-resume. Inspect the result before continuing.')
        raise SystemExit(1)
