"""Additive stock-fast deployment. Defaults to plan-only; never arms trading.

--broker-check logs in through the installed session and reads positions/orders.
--apply additionally stops verified services, repeats the flat checks, installs
only four backend files (plus optional additive UI), and restarts those services.
All credentials, global settings, PM2, the FIFO limiter and other code survive.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
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

BASE_RUN = 'b27d03215ea7ba7e4fe6e73b1052da208beea8d2'
BASE_SCANNER = 'aca08c5c6af10e7fffbbb316eea3d039aeb41731'
FIFO = '9886d951be6997e0ddf8fdda640529f5f751a25c'
HELPER = 'e3f8970bcaeedfc14fd8f4ee295df53f23ec7e61'
UI_HELPER = '3e72af124de85020e873614128f78d6dc080b185'
FILES = {'run.py': BASE_RUN, 'kadima/full_universe_scanner.py': BASE_SCANNER,
         'kadima/stock_fast.py': None, 'kadima/stock_fast_api.py': None}
ENGINE = 'kadima.service'
SCANNER = 'kadima-universe-scanner.service'
DASHBOARD = 'kadima-dashboard.service'
TOKEN_FILE = Path('/etc/kadima-dashboard/config.json')
GATEWAY = Path('/opt/kadima-dashboard/gateway.py')
PAGE = Path('/var/www/html/dashboard.html')


class Blocked(RuntimeError):
    """Only installer-authored nonsecret diagnostics may use this exception."""


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def read_file(path, missing=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise Blocked('symlink_or_noncanonical_path')
    try:
        with path.open('rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Blocked('not_a_regular_file')
            raw = handle.read(2_000_001)
    except FileNotFoundError:
        if missing:
            return None
        raise Blocked('required_file_missing') from None
    if len(raw) > 2_000_000:
        raise Blocked('oversize_source')
    return raw


def load_helper(path, expected):
    raw = read_file(path)
    if digest(raw) != expected:
        raise Blocked('reviewed_helper_hash_mismatch')
    spec = importlib.util.spec_from_file_location('_sf_'+path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class Change:
    path: Path
    original: bytes | None
    replacement: bytes
    uid: int
    gid: int
    mode: int

    def unchanged(self):
        if read_file(self.path, missing=True) != self.original:
            raise Blocked('source_changed_since_plan:'+self.path.name)


def change_for(path, new, default_owner):
    old = read_file(path, missing=True)
    metadata = path.stat() if old is not None else None
    return Change(path, old, new,
                  metadata.st_uid if metadata else default_owner[0],
                  metadata.st_gid if metadata else default_owner[1],
                  stat.S_IMODE(metadata.st_mode) if metadata else 0o644)


def broker_preflight_code(code):
    # Pinning the helper protects its call surface. Missing data is not flat.
    anchor = "rows = r.get('data')"
    if code.count(anchor) != 1:
        raise Blocked('broker_preflight_shape_changed')
    replacement = ("if 'data' not in r:\n            reason = 'broker_response_invalid'"
                   "\n            raise RuntimeError()\n        rows = r.get('data')")
    result = code.replace(anchor, replacement)
    compile(result, 'broker_preflight', 'exec')
    return result


def verify_gateway(current, reviewed):
    # Preserve newer display functions but require unchanged auth/proxy handlers.
    required = {'authenticated', 'origin_check', 'read_data', 'celestial_write'}
    def functions(source):
        out = {}
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in required:
                if node.name in out:
                    raise Blocked('duplicate_gateway_auth_handler')
                out[node.name] = ast.dump(node, include_attributes=False)
        return out
    observed, expected = functions(current), functions(reviewed)
    if set(observed) != required or set(expected) != required or observed != expected:
        raise Blocked('gateway_auth_handlers_require_review')


def local_modules(raw, package):
    found = set()
    for node in ast.walk(ast.parse(raw)):
        names = []
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                names = [node.module] if node.module else [a.name for a in node.names]
            elif node.module == 'kadima':
                names = [a.name for a in node.names]
            elif node.module and node.module.startswith('kadima.'):
                names = [node.module[7:]]
        elif isinstance(node, ast.Import):
            names = [a.name[7:] for a in node.names if a.name.startswith('kadima.')]
        for name in names:
            relative = 'kadima/'+name.replace('.', '/')+'.py'
            if (package/relative).is_file():
                found.add(relative)
    return found


def backend_plan(package, target, owner):
    """Unknown dependencies are refused, not overwritten or allowlisted."""
    if package == target:
        raise Blocked('separate_release_directory_required')
    changes, fingerprints = [], {}
    for name, base in FILES.items():
        incoming = read_file(package/name)
        compile(incoming, name, 'exec')
        item = change_for(target/name, incoming, owner)
        current = digest(item.original) if item.original is not None else None
        if current not in (base, digest(incoming)):
            raise Blocked('unreviewed_target:'+name)
        fingerprints[item.path] = item.original
        if item.original != incoming:
            changes.append(item)
    todo, visited = list(FILES), set()
    if (package/'kadima/__init__.py').exists():
        todo.append('kadima/__init__.py')
    while todo:
        name = todo.pop()
        if name in visited:
            continue
        visited.add(name)
        release = read_file(package/name)
        current = read_file(target/name, missing=name in FILES)
        fingerprints[target/name] = current
        # config.py is deployment-specific; preserve it and validate syntax.
        if name not in FILES and name != 'kadima/config.py' and current != release:
            raise Blocked('unreviewed_dependency:'+name)
        source = release if name in FILES else current
        if source is None:
            raise Blocked('dependency_missing:'+name)
        compile(source, name, 'exec')
        todo.extend(local_modules(source, package))
    limiter = read_file(target/'kadima/rate_limiter.py')
    if digest(limiter) != FIFO or digest(read_file(package/'kadima/rate_limiter.py')) != FIFO:
        raise Blocked('fifo_limiter_must_be_preserved')
    fingerprints[target/'kadima/rate_limiter.py'] = limiter
    return changes, fingerprints


def check_fingerprints(fingerprints):
    for path, original in fingerprints.items():
        if read_file(path, missing=True) != original:
            raise Blocked('dependency_changed_since_plan:'+path.name)


def command(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True,
                          timeout=kwargs.pop('timeout', 30), **kwargs)


def unit_state(unit):
    result = command(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID'])
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def process(unit, cwd, uid):
    info = unit_state(unit)
    pid = info.get('MainPID', '')
    if info.get('ActiveState') != 'active' or not pid.isdigit() or int(pid) <= 0:
        raise Blocked('required_service_not_active:'+unit)
    proc = Path('/proc')/pid
    if proc.stat().st_uid != uid or (proc/'cwd').resolve() != cwd:
        raise Blocked('service_owner_or_directory_mismatch:'+unit)
    argv = [os.fsdecode(v) for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
    env = {os.fsdecode(kv.split(b'=', 1)[0]): os.fsdecode(kv.split(b'=', 1)[1])
           for kv in (proc/'environ').read_bytes().split(b'\0') if b'=' in kv}
    return pid, argv, env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_api(path, token):
    if path not in ('/api/kadima/top20', '/api/kadima/stock-fast'):
        raise Blocked('unknown_local_endpoint')
    request = urllib.request.Request('http://127.0.0.1:8081'+path,
                                    headers={'Authorization':'Bearer '+token}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=8) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise Blocked('oversize_api_response')
    return json.loads(raw)


def paused_status(status):
    if not isinstance(status, dict) or not isinstance(status.get('config'), dict):
        raise Blocked('control_schema_unknown')
    cfg = status['config']
    # This installer supports the observed paused rollout, never an armed migration.
    if cfg.get('rollout_enabled') is not False or cfg.get('auto_trade_enabled') is not False:
        raise Blocked('paused_celestial_rollout_required')
    if cfg.get('kill_latched') is not False:
        raise Blocked('kill_state_requires_operator_review')
    if not isinstance(status.get('positions'), list) or status['positions']:
        raise Blocked('local_api_exposure_or_unknown')
    try:
        timestamp = datetime.fromisoformat(status['ts'])
        age = (datetime.now(timezone.utc)-timestamp).total_seconds()
        if not 0 <= age <= 30:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise Blocked('stale_or_unknown_control_timestamp') from None


def local_flat(db):
    if not db.is_file():
        raise Blocked('local_database_missing')
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=2)) as connection:
        connection.execute('PRAGMA query_only=ON')
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'positions', 'execution_orders'}.issubset(tables):
            raise Blocked('local_exposure_schema_unknown')
        if connection.execute("SELECT 1 FROM positions WHERE status IS NULL OR status != 'closed' LIMIT 1").fetchone():
            raise Blocked('local_position_unresolved')
        if connection.execute("SELECT 1 FROM execution_orders WHERE state IS NULL OR state != 'terminal' LIMIT 1").fetchone():
            raise Blocked('local_intent_unresolved')
        for table, field in (('celestial_config', 'auto_trade_enabled'), ('stock_fast_controls', 'live_enabled')):
            if table in tables:
                row = connection.execute('SELECT payload FROM '+table+' WHERE id=1').fetchone()
                if row is not None and json.loads(row[0]).get(field) is not False:
                    raise Blocked('persistent_live_intent_requires_operator_pause')


def write_atomic(item, raw):
    # The existing owner/mode are kept; new files use the verified engine owner.
    handle, temporary = tempfile.mkstemp(prefix='.stock-fast-', dir=item.path.parent)
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), item.mode)
            os.fchown(output.fileno(), item.uid, item.gid)
        os.replace(temporary, item.path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_transaction(changes, units, backup, flat_check, source_check, import_check,
                        post_check, runner=command):
    """Rollback before restart only; never roll back a DB or potentially active code."""
    for index, item in enumerate(changes):
        item.unchanged()
        if item.original is not None:
            (backup/str(index)).write_bytes(item.original)
    (backup/'files.json').write_text(json.dumps([
        dict(path=str(c.path), existed=c.original is not None, uid=c.uid, gid=c.gid,
             mode=c.mode, old_hash=digest(c.original) if c.original is not None else None,
             new_hash=digest(c.replacement)) for c in changes], indent=2))
    replaced, restart_attempted = [], False
    try:
        runner(['systemctl', 'stop', *units], timeout=60)
        for unit in units:
            state = runner(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID']).stdout
            fields = dict(line.split('=', 1) for line in state.splitlines() if '=' in line)
            if fields.get('ActiveState') != 'inactive' or fields.get('MainPID') != '0':
                raise Blocked('service_not_fully_stopped:'+unit)
        source_check()
        flat_check()  # A new/manual position appearing during shutdown aborts installation.
        source_check()
        for item in changes:
            item.unchanged()
            replaced.append(item)
            write_atomic(item, item.replacement)
        import_check()
        restart_attempted = True
        runner(['systemctl', 'start', *reversed(units)], timeout=60)
        post_check()
    except BaseException:
        if restart_attempted:
            print('INSTALLED SOURCE RETAINED: restart was attempted; review services/exposure before recovery.')
        else:
            for item in reversed(replaced):
                if item.original is None:
                    item.path.unlink(missing_ok=True)
                else:
                    write_atomic(item, item.original)
            try:
                runner(['systemctl', 'start', *reversed(units)], timeout=60)
            except Exception:
                print('SERVICE RESTORE FAILED: operator recovery is required.')
            print('INSTALL ABORTED: original source retained/restored; no trading flags changed.')
        print('BACKUP:', backup)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('/home/ubuntu/kadima-py'))
    parser.add_argument('--broker-check', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--with-dashboard', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Blocked('sudo_required_for_process_inventory')
    if args.apply and not args.broker_check:
        raise Blocked('apply_requires_fresh_broker_check')
    sys.dont_write_bytecode = True
    package, target = Path(__file__).resolve().parents[1], args.target.absolute()
    user = pwd.getpwnam('ubuntu')
    helper = load_helper(package/'executor_fix/install.py', HELPER)
    changes, fingerprints = backend_plan(package, target, (user.pw_uid, user.pw_gid))
    pid, argv, env = process(ENGINE, target, user.pw_uid)
    python = helper.engine_interpreter(Path('/proc')/pid, target)
    scanner_pid, scanner_argv, _ = process(SCANNER, target, user.pw_uid)
    scanner_tail = [v for v in scanner_argv[1:] if v not in ('-u', '-B')]
    if scanner_tail != ['-m', 'kadima.stock_universe_scanner']:
        raise Blocked('unreviewed_scanner_launch_operand')
    token_bytes = read_file(TOKEN_FILE)
    token = json.loads(token_bytes).get('api_token')
    if not isinstance(token, str) or not 0 < len(token) < 8192:
        raise Blocked('local_control_credentials_unavailable')
    fingerprints[TOKEN_FILE] = token_bytes
    paused_status(local_api('/api/kadima/top20', token))
    db = Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db'
    if not db.is_absolute():
        raise Blocked('absolute_data_directory_required')
    local_flat(db)
    units = [SCANNER, ENGINE]
    if args.with_dashboard:
        _, gateway_argv, _ = process(DASHBOARD, GATEWAY.parent, user.pw_uid)
        if gateway_argv[1:4] != ['-m', 'uvicorn', 'gateway:app']:
            raise Blocked('unreviewed_dashboard_launch_operand')
        ui = load_helper(package/'stock_fast_update/upgrade_dashboard.py', UI_HELPER)
        script = read_file(package/'stock_fast_update/panel.js').decode()
        page = read_file(PAGE)
        gateway = read_file(GATEWAY)
        verify_gateway(gateway, read_file(package/'dashboard_live/gateway.py'))
        # Additive anchors preserve all other frontend work; auth code is untouched.
        for path, old, new in (
                (GATEWAY, gateway, ui.patch_gateway(gateway.decode()).encode()),
                (PAGE, page, ui.patch_dashboard(page.decode(), script).encode())):
            fingerprints[path] = old
            if old != new:
                changes.append(change_for(path, new, (user.pw_uid, user.pw_gid)))
        units.insert(0, DASHBOARD)
    print('PLAN: '+str(len(changes))+' additive file changes; FIFO, PM2, config and credentials preserved.')
    if not args.broker_check:
        print('PLAN ONLY: no broker login, source replacement, service stop or live activation.')
        return
    # Separate child permissions are disabled only in this subprocess; no env file write.
    child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0',
                     KADIMA_FAST_MOMENTUM_LIVE='0', KADIMA_CAS_LIVE='0')
    def as_user():
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    def child(code):
        return command([python, '-B', '-c', code], cwd=target, env=child_env,
                       preexec_fn=as_user, timeout=60)
    def flat_check():
        local_flat(db)
        try:
            response = child(broker_preflight_code(helper.PREFLIGHT))
            if 'KADIMA_PREFLIGHT_OK' not in response.stdout.splitlines():
                raise Blocked('broker_flat_not_confirmed')
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise Blocked(helper.failure_summary(exc, 'broker_preflight')) from None
        local_flat(db)
    flat_check()
    print('BROKER CHECK: flat position/order books observed; no place/modify/cancel order calls.')
    if not args.apply:
        print('CHECK ONLY: no source replacement, service stop or live activation.')
        return
    # Reject a service respawn or operator flag/source change during preparation.
    if process(ENGINE, target, user.pw_uid)[0] != pid or process(SCANNER, target, user.pw_uid)[0] != scanner_pid:
        raise Blocked('service_changed_during_preparation')
    paused_status(local_api('/api/kadima/top20', token))
    check_fingerprints(fingerprints)
    if not changes:
        print('ALREADY INSTALLED: no services restarted; activation remains separate.')
        return
    backup = Path(tempfile.mkdtemp(prefix='kadima-stock-fast-', dir='/var/backups'))
    backup.chmod(0o700)
    def import_check():
        child('import kadima.engine; import kadima.control_api; import kadima.stock_fast; '
              'import kadima.stock_fast_api; import kadima.full_universe_scanner')
    def post_check():
        for unit in units:
            if unit_state(unit).get('ActiveState') != 'active':
                raise Blocked('post_install_service_not_active:'+unit)
        for _ in range(15):
            try:
                data = local_api('/api/kadima/stock-fast', token)
                if data.get('controls', {}).get('live_enabled') is not False:
                    raise Blocked('stock_live_intent_not_off_after_install')
                paused_status(local_api('/api/kadima/top20', token))
                print('STOCK API: HTTP 200; stock live intent remains OFF.')
                return
            except Blocked:
                raise
            except Exception:
                time.sleep(1)
        raise Blocked('post_install_stock_api_unavailable')
    install_transaction(changes, units, backup, flat_check,
                        lambda: check_fingerprints(fingerprints), import_check, post_check)
    print('INSTALLED; backup:', backup)
    print('NO LIVE ACTIVATION. Worker/feed readiness and operator-controlled arming remain separate.')


if __name__ == '__main__':
    try:
        # Prevent two copies of this installer running concurrently. No global env changes.
        fd = os.open('/run/lock/kadima-stock-fast-install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            main()
    except Blocked as exc:
        print('BLOCKED:', str(exc))
        raise SystemExit(1)
    except Exception as exc:
        # Never print raw argv/environment, HTTP payloads or SDK exception strings.
        print('BLOCKED:', type(exc).__name__, '(details suppressed; no credential output)')
        raise SystemExit(1)
