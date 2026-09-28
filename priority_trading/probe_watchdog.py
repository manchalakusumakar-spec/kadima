"""Diagnose the installed-but-held watchdog without reinstalling or activating.

Calls the existing guarded watchdog endpoint once, only with paused controls,
no managed positions, a revoked permit, and inactive scheduling services/timers.
This is not a generic read-only endpoint: the ordinary watchdog can reconcile
fills and request owned exits. No activation, restart, source edit or fallback
execution is performed here. Unknown response bodies and secrets are omitted.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import urllib.error
import urllib.request

ROOT = Path('/home/ubuntu/kadima-py')
STATE = Path('/var/lib/kadima-priority-trading-v1')
LEASE = Path('/run/kadima-session-auto/lease.json')
ENGINE = 'kadima.service'
HELD = ('kadima-session-auto.timer', 'kadima-owned-watchdog.timer',
        'kadima-session-auto.service', 'kadima-owned-watchdog.service',
        'kadima-eod-watchdog.service')
LOCKS = ('kadima-stopped-audit-install', 'kadima-stopped-source-install',
    'kadima-stock-fast-install', 'kadima-activation', 'kadima-session-auto-run',
    'kadima-session-auto-install', 'kadima-multi-position-install',
    'kadima-auth-recovery', 'kadima-kill-reset', 'kadima-contract-note-repair',
    'kadima-mcx-live-install')
DETAILS = frozenset(('control_api_token_not_configured', 'missing_bearer_token',
    'invalid_token', 'executor_not_registered', 'celestial_rollout_not_enabled',
    'watchdog_accepts_no_raw_order_arguments',
    'watchdog_ownership_or_broker_readiness_unverified'))
ERRORS = frozenset(('ValueError', 'TypeError', 'KeyError', 'AttributeError',
    'NameError', 'ImportError', 'ModuleNotFoundError', 'RuntimeError',
    'TimeoutError', 'PermissionError', 'OperationalError', 'HTTPError',
    'URLError', 'ConnectionError', 'JSONDecodeError'))


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def read(path, *, private=False, protected=False):
    path = Path(path)
    require(path.resolve() == path, 'noncanonical_file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'nonregular_file')
        if private:
            require(info.st_uid == 0 and not info.st_mode & 0o077, 'private_file_required')
        if protected:
            require(info.st_uid == 0 and not info.st_mode & 0o022, 'protected_file_required')
        raw = handle.read(2_000_001)
    require(len(raw) <= 2_000_000, 'file_too_large')
    return raw


def observe(unit):
    result = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--no-pager',
        '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'UnitFileState'],
        capture_output=True, check=True, timeout=10)
    return dict(line.split('=', 1) for line in result.stdout.decode().splitlines())


def held_services():
    units = {unit: observe(unit) for unit in HELD}
    for unit, value in units.items():
        require(value.get('ActiveState') in ('inactive', 'failed') and
                value.get('MainPID', '0') == '0', 'scheduling_not_held:'+unit)
        if unit.endswith('.timer'):
            require(value.get('UnitFileState') == 'disabled', 'timer_not_disabled:'+unit)
    return units


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(token, path, *, post=False):
    require(path in ('/api/kadima/config', '/api/kadima/top20',
                     '/api/kadima/stock-fast', '/api/kadima/watchdog'), 'unknown_route')
    require(post == (path == '/api/kadima/watchdog'), 'wrong_method')
    req = urllib.request.Request('http://127.0.0.1:8081'+path,
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'},
        data=b'{}' if post else None, method='POST' if post else 'GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=40 if post else 5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(2_000_001)
        code = response.code
    require(len(raw) <= 2_000_000, 'response_too_large')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        data = {}
    return code, data if isinstance(data, dict) else {}


def http_summary(code, data):
    result = dict(http_status=code)
    detail = data.get('detail')
    result['detail'] = detail if isinstance(detail, str) and detail in DETAILS else 'omitted'
    if type(data.get('ok')) is bool:
        result['ok'] = data['ok']
    for key in ('due_owned_positions', 'inspected_positions'):
        if type(data.get(key)) is int and 0 <= data[key] <= 100000:
            result[key] = data[key]
    return result


def paused(get):
    values = {}
    for name in ('config', 'top20', 'stock-fast'):
        code, data = get('/api/kadima/'+name)
        require(code == 200, 'control_http_'+str(code)+':'+name)
        values[name] = data
    cfg, top, stock = (values[k] for k in ('config', 'top20', 'stock-fast'))
    require(cfg.get('auto_trade_enabled') is False and cfg.get('kill_latched') is False and
            cfg.get('rollout_enabled') is True, 'global_pause_unverified')
    require(top.get('positions') == [] and top.get('config') == cfg, 'managed_exposure_or_config_changed')
    require(stock.get('controls', {}).get('live_enabled') is False and
            stock.get('global_auto_requested') is False, 'stock_pause_unverified')
    for data in (top, stock):
        stamp = datetime.fromisoformat(data['ts'])
        require(stamp.tzinfo is not None and
                0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 30, 'stale_controls')
    return cfg, stock['controls']


def journal_summary(raw):
    """Emit only traceback frame locations and exception types, never messages."""
    out = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get('MESSAGE', '')
        if not isinstance(message, str):
            continue
        for part in message.splitlines():
            frame = re.fullmatch(r'\s*File "[^"]*/([A-Za-z0-9_.-]+\.py)", line (\d+), in ([A-Za-z0-9_<>]+)\s*', part)
            if frame:
                out.append(dict(file=frame[1], line=int(frame[2]), function=frame[3]))
            match = re.match(r'^([A-Za-z]+Error):', part.strip())
            if match and match[1] in ERRORS:
                out.append(dict(error_type=match[1]))
    return out[-40:]


def probe(report):
    require(os.geteuid() == 0, 'sudo_required')
    with ExitStack() as stack:
        # Existing maintenance lock inodes only; never create or truncate one.
        for name in LOCKS:
            fd = os.open('/run/lock/'+name+'.lock', os.O_RDWR | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
                    info.st_nlink == 1 and not info.st_mode & 0o022, 'unsafe_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = json.loads(read(STATE/'status.json', private=True))
        require(receipt.get('state') == 'priority_trading_incomplete', 'incomplete_installation_required')
        manifest = json.loads((Path(__file__).parent/'manifest.json').read_bytes())
        for name, pins in manifest['runtime'].items():
            raw = read(ROOT/name)
            digest = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
            require(digest == pins['new'], 'installed_source_changed:'+name)
        report['services'] = held_services()
        engine = observe(ENGINE)
        require(engine.get('ActiveState') == 'active' and engine.get('SubState') == 'running'
                and int(engine.get('MainPID', '0')) > 1, 'running_engine_required')
        pid = engine['MainPID']
        env = {}
        for line in read(STATE/'watchdog.env', private=True).decode().splitlines():
            parts = shlex.split(line, comments=False, posix=True)
            require(len(parts) == 1 and '=' in parts[0], 'watchdog_environment_invalid')
            key, value = parts[0].split('=', 1)
            require(key not in env, 'duplicate_environment_variable')
            env[key] = value
        token = env.get('KADIMA_API_TOKEN')
        require(isinstance(token, str) and bool(token), 'watchdog_token_missing')
        process_env = dict(part.split(b'=', 1) for part in
                           Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in part)
        report['watchdog_engine_token_match'] = hmac.compare_digest(
            token.encode(), process_env.get(b'KADIMA_API_TOKEN', b''))
        require(report['watchdog_engine_token_match'], 'watchdog_engine_token_mismatch')
        lease = read(LEASE, protected=True)
        require(json.loads(lease).get('allowed') is False, 'revoked_permit_required')
        get = lambda path: request(token, path)
        policies = paused(get)
        require(observe(ENGINE) == engine and read(LEASE, protected=True) == lease,
                'engine_or_permit_changed')
        held_services()
        report['watchdog_endpoint_called'] = True
        code, data = request(token, '/api/kadima/watchdog', post=True)
        report['watchdog'] = http_summary(code, data)
        report['postcheck_paused'] = paused(get) == policies
        report['postcheck_engine_unchanged'] = observe(ENGINE) == engine
        report['postcheck_permit_revoked'] = read(LEASE, protected=True) == lease
        report['services_after'] = held_services()
        report['state'] = 'watchdog_probe_observed'


def main():
    report = dict(state='watchdog_probe_blocked', activation=False,
        service_changes_requested=False, watchdog_endpoint_called=False)
    try:
        probe(report)
    except Exception as exc:
        report['blocked'] = str(exc) if isinstance(exc, Blocked) else (
            type(exc).__name__ if type(exc).__name__ in ERRORS else 'probe_failed')
    try:
        result = subprocess.run(['/usr/bin/journalctl', '-u', ENGINE,
            '--since', '2026-09-28 16:41:00 UTC', '--no-pager', '-n', '400', '-o', 'json'],
            capture_output=True, timeout=10, check=True)
        report['engine_traceback'] = journal_summary(result.stdout.decode())
    except Exception:
        report['journal_available'] = False
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['state'] == 'watchdog_probe_observed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
