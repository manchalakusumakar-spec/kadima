"""Operator-only, flat-account upgrade to capital-based multi-position entry.

Requires the completed PR #10 market-day scheduler. No order submission, control
arming, risk-amount edits or environment changes. Run outside the arming window.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time

ROOT = Path('/home/ubuntu/kadima-py')
SCHEDULER = Path('/opt/kadima-session-auto')
TIMER = 'kadima-session-auto.timer'
JOB = 'kadima-session-auto.service'
SESSION_HASH = '4af4e494da6d2cf69d52e6182452101901b8ca46'
HELPER_HASH = '587e138396139fc3abc6407b7329d686676df9a3'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def require_outside_session(gate, policy, now=None):
    reason = gate.calendar_reason(policy, now or datetime.now(gate.IST))
    if reason not in ('non_trading_day', 'outside_scheduled_window'):
        raise ValueError('maintenance_outside_scheduled_window_required')


def sources(package, target, helper, original_executor, new_executor, owner):
    """Compare installed session-aware bytes; never reinstall an unguarded base."""
    replacements = {
        'kadima/auto_executor.py': new_executor,
        'kadima/portfolio_capacity.py': helper.read_file(package/'kadima/portfolio_capacity.py'),
    }
    changes, fingerprints = [], {}
    for name, incoming in replacements.items():
        item = helper.change_for(target/name, incoming, owner)
        accepted = (original_executor, new_executor) if name.endswith('auto_executor.py') else (None, incoming)
        if item.original not in accepted:
            raise ValueError('unreviewed_multi_position_target')
        if item.original != incoming:
            changes.append(item)
        fingerprints[item.path] = item.original
    todo, visited = ['run.py', 'kadima/auto_executor.py', 'kadima/__init__.py'], set()
    while todo:
        name = todo.pop()
        if name in visited:
            continue
        visited.add(name)
        current = helper.read_file(target/name, missing=name in replacements)
        expected = replacements.get(name)
        if expected is None:
            expected = helper.read_file(package/name)
            if name != 'kadima/config.py' and current != expected:
                raise ValueError('unreviewed_multi_position_dependency')
        fingerprints[target/name] = current
        effective = expected if name in replacements else current
        compile(effective, name, 'exec')
        todo.extend(helper.local_modules(effective, package))
    return changes, fingerprints


def perform(changes, units, backup, *, helper, flat, check, imports, postcheck):
    """Keep existing timer enablement; pause execution only for guarded maintenance."""
    written, restart_attempted = [], False
    (backup/'files.json').write_text(json.dumps([
        dict(path=str(c.path), old=helper.digest(c.original) if c.original is not None else None,
             new=helper.digest(c.replacement), uid=c.uid, gid=c.gid, mode=c.mode)
        for c in changes], indent=2))
    for i, item in enumerate(changes):
        item.unchanged()
        if item.original is not None:
            (backup/str(i)).write_bytes(item.original)
    try:
        # Stop the timer first and wait for any in-flight permission check to end.
        helper.command(['systemctl', 'stop', TIMER, JOB], timeout=180)
        for unit in (TIMER, JOB):
            if helper.unit_state(unit).get('ActiveState') != 'inactive':
                raise ValueError('scheduler_not_fully_stopped')
        check()
        helper.command(['systemctl', 'stop', *units], timeout=60)
        for unit in units:
            state = helper.unit_state(unit)
            if state.get('ActiveState') != 'inactive' or state.get('MainPID') != '0':
                raise ValueError('engine_or_scanner_not_fully_stopped')
        flat()
        helper.check_fingerprints(check.fingerprints)
        for item in changes:
            item.unchanged()
            written.append(item)
            helper.write_atomic(item, item.replacement)
        imports()
        restart_attempted = True
        helper.command(['systemctl', 'start', *reversed(units)], timeout=60)
        postcheck()
        helper.command(['systemctl', 'start', TIMER], timeout=30)
        if helper.unit_state(TIMER).get('ActiveState') != 'active':
            raise ValueError('timer_restore_unconfirmed')
    except BaseException:
        if restart_attempted:
            # Guard remains installed. Do not roll back code under possible exposure.
            print('POST-RESTART FAILURE: source retained; inspect service/position state before recovery.')
            print('Verify timer state; no automatic source rollback was attempted.')
        else:
            for item in reversed(written):
                if item.original is None:
                    item.path.unlink(missing_ok=True)
                else:
                    helper.write_atomic(item, item.original)
            try:
                helper.command(['systemctl', 'start', *reversed(units)], timeout=60)
                helper.command(['systemctl', 'start', TIMER], timeout=30)
            except Exception:
                print('SERVICE RESTORATION UNCONFIRMED: inspect services before any other action.')
            print('UPGRADE ABORTED: original session-guarded source retained/restored.')
        print('BACKUP:', backup)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('sudo_required')
    package = Path(__file__).resolve().parents[1]
    raw_helper = (package/'stock_fast_update/install.py').read_bytes()
    if hashlib.sha1(b'blob '+str(len(raw_helper)).encode()+b'\0'+raw_helper).hexdigest() != HELPER_HASH:
        raise ValueError('reviewed_install_helper_required')
    helper = load(package/'stock_fast_update/install.py', 'multi_install_helper')
    session = helper.load_helper(package/'session_auto/install.py', SESSION_HASH)
    old_broker = helper.load_helper(package/'executor_fix/install.py', helper.HELPER)
    patcher = load(package/'multi_position_update/patch.py', 'multi_patcher')
    gate = load(package/'session_auto/gate.py', 'multi_session_gate')
    base = helper.read_file(package/'kadima/auto_executor.py')
    current_expected = session.patch_executor(base)
    new_executor = patcher.patch_executor(current_expected)
    user = pwd.getpwnam('ubuntu')
    changes, fingerprints = sources(package, ROOT, helper, current_expected, new_executor,
                                    (user.pw_uid, user.pw_gid))
    # Explicitly verify both gate copies, scheduler code and the deployed policy.
    for installed, reviewed in (
            (ROOT/'kadima/market_session_gate.py', package/'session_auto/gate.py'),
            (SCHEDULER/'gate.py', package/'session_auto/gate.py'),
            (SCHEDULER/'runner.py', package/'session_auto/runner.py')):
        raw = helper.read_file(installed)
        if raw != helper.read_file(reviewed):
            raise ValueError('reviewed_session_scheduler_required')
        fingerprints[installed] = raw
    for path in (gate.POLICY, session.ENV, helper.TOKEN_FILE):
        fingerprints[path] = helper.read_file(path)
    policy = gate.validate_policy(json.loads(fingerprints[gate.POLICY]))
    if fingerprints[session.ENV].decode() != session.ENV_CONTENT:
        raise ValueError('session_environment_changed')
    require_outside_session(gate, policy)
    token = json.loads(fingerprints[helper.TOKEN_FILE]).get('api_token')
    if not isinstance(token, str) or not 0 < len(token) < 8192:
        raise ValueError('local_auth_unavailable')
    pid, _, env = helper.process(helper.ENGINE, ROOT, user.pw_uid)
    python = old_broker.engine_interpreter(Path('/proc')/pid, ROOT)
    spid, sargv, senv = helper.process(helper.SCANNER, ROOT, user.pw_uid)
    if [v for v in sargv[1:] if v not in ('-u', '-B')] != ['-m', 'kadima.stock_universe_scanner']:
        raise ValueError('scanner_entrypoint_changed')
    for process_env in (env, senv):
        if any(process_env.get(k) != '1' for k in ('KADIMA_CELESTIAL_ENABLED',
                'KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_SESSION_AUTOTRADE')):
            raise ValueError('session_runtime_flags_not_confirmed')
    if env.get('KADIMA_DATA_DIR', '/var/lib/kadima') != senv.get('KADIMA_DATA_DIR', '/var/lib/kadima'):
        raise ValueError('shared_data_directory_required')
    db = Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db'

    def paused():
        require_outside_session(gate, policy)
        gd = helper.local_api('/api/kadima/top20', token)
        sd = helper.local_api('/api/kadima/stock-fast', token)
        now = datetime.now(timezone.utc)
        for value in (gd, sd):
            stamp = datetime.fromisoformat(value['ts'])
            if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= 30:
                raise ValueError('fresh_control_snapshot_required')
        cfg, stock = gd.get('config') or {}, sd.get('controls') or {}
        if (cfg.get('auto_trade_enabled') is not False or stock.get('live_enabled') is not False or
                cfg.get('kill_latched') is not False or cfg.get('broker_orders_enabled') is not True or
                cfg.get('rollout_enabled') is not True):
            raise ValueError('paused_reviewed_controls_required')
        helper.local_flat(db)

    paused()
    helper.command(['systemctl', 'is-enabled', '--quiet', TIMER])
    if helper.unit_state(TIMER).get('ActiveState') != 'active':
        raise ValueError('existing_timer_must_be_active')
    print('PLAN: remove one-position count cap; preserve session guards and per-trade limits.')
    print('PORTFOLIO: cash and remaining daily stop-risk headroom; unresolved orders block new entries.')
    if not changes:
        print('MULTI-POSITION ALREADY INSTALLED. No restart or settings reset.')
        return
    if not args.apply:
        print('PLAN ONLY. No broker login, changes or service actions.')
        return
    child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0')
    def child(code):
        return helper.command([python, '-B', '-c', code], cwd=ROOT, env=child_env,
            user=user.pw_uid, group=user.pw_gid, extra_groups=[user.pw_gid], timeout=60)
    def flat():
        helper.local_flat(db)
        result = child(helper.broker_preflight_code(old_broker.PREFLIGHT))
        if 'KADIMA_PREFLIGHT_OK' not in result.stdout.splitlines():
            raise ValueError('broker_flat_not_confirmed')
        helper.local_flat(db)
    flat()
    print('BROKER: flat books observed; no order placement/modification/cancellation by this updater.')
    def check():
        paused()
        helper.check_fingerprints(fingerprints)
        if (helper.process(helper.ENGINE, ROOT, user.pw_uid)[0] != pid or
                helper.process(helper.SCANNER, ROOT, user.pw_uid)[0] != spid):
            raise ValueError('process_changed_during_upgrade')
    check.fingerprints = fingerprints
    check()
    backup = Path(tempfile.mkdtemp(prefix='kadima-multi-position-', dir='/var/backups'))
    backup.chmod(0o700)
    def imports():
        child('import kadima.auto_executor; import kadima.portfolio_capacity; '
              'import kadima.market_session_gate; import kadima.stock_fast_api')
    def postcheck():
        for _ in range(20):
            try:
                paused()
                if any(helper.unit_state(u).get('ActiveState') != 'active'
                       for u in (helper.ENGINE, helper.SCANNER)):
                    raise ValueError('post_upgrade_service_not_active')
                if helper.read_file(ROOT/'kadima/auto_executor.py') != new_executor:
                    raise ValueError('post_upgrade_source_mismatch')
                return
            except Exception:
                time.sleep(1)
        raise ValueError('post_upgrade_verification_failed')
    perform(changes, [helper.SCANNER, helper.ENGINE], backup, helper=helper,
            flat=flat, check=check, imports=imports, postcheck=postcheck)
    print('MULTI-POSITION INSTALLED: count cap removed; capital/risk gates preserved.')
    print('PENDING ORDERS: new submissions wait for confirmed terminal state.')
    print('MARKET-DAY TIMER: restored active; schedule policy and controls unchanged.')
    print('BACKUP:', backup)


if __name__ == '__main__':
    try:
        handles = []
        for name in ('kadima-stock-fast-install', 'kadima-activation',
                     'kadima-session-auto-install', 'kadima-multi-position-install'):
            fd = os.open('/run/lock/'+name+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, 'w'); handles.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
    except Exception as exc:
        # Never print captured SDK output, credentials or process environments.
        import re
        value = str(exc)
        reason = value if type(exc) is ValueError and re.fullmatch(r'[a-z_]+', value) else type(exc).__name__
        print('BLOCKED:', reason)
        raise SystemExit(1)
