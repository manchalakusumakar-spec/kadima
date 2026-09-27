"""Pause only new entries and inspect AWS without stopping trading services.

Default is read-only inventory. --pause-entries is an explicit operator action.
This never checks or certifies broker flatness, deploys code, repairs the ledger,
resumes trading, or disables protection for existing positions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import urllib.request

TARGET = Path('/home/ubuntu/kadima-py')
TOKEN_FILE = Path('/etc/kadima-dashboard/config.json')
TIMERS = ('kadima-session-auto.timer', 'kadima-deploy-20260928.timer')
WORKERS = ('kadima-session-auto.service', 'kadima-deploy-20260928.service')
SERVICES = ('kadima.service', 'kadima-universe-scanner.service', 'kadima-dashboard.service')
READ_ROUTES = ('top20', 'stock-fast')
WRITE_ROUTES = ('pause', 'stock-fast/config')


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def read_regular(path, limit=2_000_000):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'canonical_path_required')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'regular_file_required')
        raw = stream.read(limit+1)
    require(len(raw) <= limit, 'file_size_limit')
    return raw


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(route, token, payload=None):
    require(route in (READ_ROUTES if payload is None else WRITE_ROUTES), 'unsupported_control_route')
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request('http://127.0.0.1:8081/api/kadima/'+route,
        data=body, headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'},
        method='GET' if payload is None else 'POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=10) as response:
        raw = response.read(2_000_001)
    require(len(raw) <= 2_000_000, 'control_response_too_large')
    result = json.loads(raw)
    require(isinstance(result, dict), 'control_response_invalid')
    return result


def snapshot(token, now=None):
    global_state, stock_state = api('top20', token), api('stock-fast', token)
    now = now or datetime.now(timezone.utc)
    for state in (global_state, stock_state):
        stamp = datetime.fromisoformat(state['ts'])
        require(stamp.tzinfo is not None and 0 <= (now-stamp).total_seconds() <= 30,
                'fresh_control_snapshot_required')
    global_controls = global_state.get('config', {})
    stock_controls = stock_state.get('controls', {})
    require(type(global_controls.get('auto_trade_enabled')) is bool and
            type(global_controls.get('kill_latched')) is bool and
            type(stock_controls.get('live_enabled')) is bool and
            type(stock_controls.get('revision')) is int, 'control_state_unknown')
    positions = global_state.get('positions')
    require(isinstance(positions, list), 'api_positions_unknown')
    return dict(snapshot_at=global_state['ts'], auto_trade_enabled=global_controls['auto_trade_enabled'],
                kill_latched=global_controls['kill_latched'], stock_live_enabled=stock_controls['live_enabled'],
                stock_revision=stock_controls['revision'], api_position_count=len(positions),
                broker_flat_verified=False)


def unit(name):
    require(name in TIMERS+WORKERS+SERVICES, 'unknown_unit')
    result = subprocess.run(['/usr/bin/systemctl', 'show', name, '-p', 'LoadState',
                             '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'MainPID'],
                            capture_output=True, text=True, timeout=15)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    require(result.returncode == 0 or values.get('LoadState') == 'not-found', 'unit_state_unavailable')
    return {k:values.get(k) for k in ('LoadState','ActiveState','UnitFileState','MainPID')}


def engine_identity():
    data = unit('kadima.service')
    pid = data.get('MainPID') or ''
    require(data.get('ActiveState') == 'active' and pid.isdigit() and int(pid)>0, 'engine_must_remain_running')
    proc = Path('/proc')/pid
    require(proc.stat().st_uid == pwd.getpwnam('ubuntu').pw_uid and (proc/'cwd').resolve() == TARGET,
            'engine_identity_unverified')
    args = (proc/'cmdline').read_bytes().split(b'\0')
    require(any(arg in (b'run.py', str(TARGET/'run.py').encode()) for arg in args), 'engine_entrypoint_unverified')
    # Protect against a process restart/PID reuse during pause admission.
    fields = (proc/'stat').read_text().rsplit(') ',1)[1].split()
    return pid, fields[19]


def workers_inactive():
    for name in WORKERS:
        data = unit(name)
        require(data.get('ActiveState') == 'inactive' and
                data.get('LoadState') in ('not-found','loaded','masked'), 'competing_worker_active_or_unknown')
        if data.get('LoadState') != 'not-found':
            require(data.get('MainPID') == '0', 'competing_worker_process')


def hold_timers():
    workers_inactive()
    for name in TIMERS:
        data = unit(name)
        require(data.get('LoadState') in ('not-found','loaded','masked'), 'timer_state_unknown')
        require(data.get('ActiveState') in ('active','inactive'), 'timer_transition_in_progress')
        if data.get('LoadState') == 'loaded':
            result = subprocess.run(['/usr/bin/systemctl','disable','--now',name],
                                    capture_output=True, text=True, timeout=30)
            require(result.returncode == 0, 'timer_pause_failed')
    verify_timer_hold()


def verify_timer_hold():
    workers_inactive()
    for name in TIMERS:
        data = unit(name)
        require(data.get('ActiveState') == 'inactive', 'entry_timer_still_active')
        if data.get('LoadState') != 'not-found':
            require(data.get('UnitFileState') in ('disabled','masked','masked-runtime'), 'entry_timer_not_disabled')


def pause_entries(token):
    identity = engine_identity()
    before = snapshot(token)
    hold_timers()
    require(engine_identity() == identity, 'engine_changed_during_pause')
    result = api('pause', token, {})
    require(result.get('auto_trade_enabled') is False, 'global_pause_unconfirmed')
    # Get a fresh revision after pausing global entries; never overwrite a
    # concurrent holding-time or live-intent change with a stale revision.
    state = snapshot(token)
    require(state['auto_trade_enabled'] is False, 'global_pause_not_persistent')
    if state['stock_live_enabled']:
        result = api('stock-fast/config', token,
                     {'patch':{'live_enabled':False},'expected_revision':state['stock_revision']})
        require(result.get('live_enabled') is False, 'stock_pause_unconfirmed')
    after = snapshot(token)
    require(not after['auto_trade_enabled'] and not after['stock_live_enabled'], 'entries_not_paused')
    require(after['kill_latched'] == before['kill_latched'], 'kill_state_changed_review_required')
    verify_timer_hold()
    require(engine_identity() == identity, 'engine_changed_during_pause')
    return after


def source_inventory():
    candidates = [TARGET/'run.py']
    for folder in ('kadima','session_auto','multi_position_update'):
        directory = TARGET/folder
        if directory.is_dir() and not directory.is_symlink():
            candidates += sorted(directory.rglob('*.py'))
    require(len(candidates) <= 400, 'source_inventory_limit')
    result = {}
    for path in candidates:
        relative = str(path.relative_to(TARGET))
        try:
            raw = read_regular(path)
            result[relative] = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        except Exception as exc:
            result[relative] = {'unavailable': type(exc).__name__}
    for label,path in (('dashboard_gateway',Path('/opt/kadima-dashboard/gateway.py')),
                       ('dashboard_html',Path('/var/www/html/dashboard.html'))):
        try:
            raw = read_regular(path)
            result[label] = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        except Exception as exc:
            result[label] = {'unavailable':type(exc).__name__}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pause-entries', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    token = json.loads(read_regular(TOKEN_FILE,100_000)).get('api_token')
    require(isinstance(token,str) and 0 < len(token) < 8192, 'api_credential_unavailable')
    # No broker requests, database writes, source writes or engine stops occur.
    with os.fdopen(os.open('/run/kadima-maintenance-prepare.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600),'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        report = dict(timestamp=datetime.now(timezone.utc).isoformat(),
                      action='pause_entries_only' if args.pause_entries else 'read_only',
                      source=source_inventory(), deployment_performed=False,
                      broker_flat_verified=False, server_stop_requested=False,
                      pause_verified=False)
        try:
            report['controls'] = pause_entries(token) if args.pause_entries else snapshot(token)
            report['services'] = {name:unit(name) for name in SERVICES+TIMERS+WORKERS}
            report['pause_verified'] = args.pause_entries
            print(json.dumps(report,indent=2,sort_keys=True))
            if args.pause_entries:
                print('ENTRY PAUSE VERIFIED. Engine remains running; no code deployed. Broker flatness remains unverified.')
        except BaseException as exc:
            report['blocked'] = str(exc) if isinstance(exc,Blocked) else type(exc).__name__
            print(json.dumps(report,indent=2,sort_keys=True))
            if args.pause_entries:
                print('Pause may be partial. Keep current holds; do not auto-resume. Engine stop was not requested.')
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:',str(exc) if isinstance(exc,Blocked) else type(exc).__name__)
        raise SystemExit(1)
