"""Consolidated baseline-to-release maintenance deployment. Run with sudo -n python3 install.py.

Stops the engine/scanner/macro services only after source, local-state and broker
checks. Rechecks broker state after stopping. Uses the engine's existing interpreter/environment for a read-only
broker preflight; never sends orders, changes credentials or trading flags.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = Path('/home/ubuntu/kadima-py')
PACKAGE = Path(__file__).resolve().parents[1]
UNITS = ['kadima.service', 'kadima-universe-scanner.service', 'kadima-macro-watch.service']


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def engine_interpreter(process, repo):
    """Confirm the service really executes this checkout before replacing it."""
    argv=[os.fsdecode(v) for v in (process/'cmdline').read_bytes().split(b'\0') if v]
    if not argv or not Path(argv[0]).is_absolute() or 'python' not in Path(argv[0]).name:
        raise SystemExit('Engine interpreter could not be verified. No files changed.')
    if (process/'cwd').resolve()!=repo.resolve():
        raise SystemExit('Engine uses a different checkout. No files changed.')
    args=argv[1:]
    if args and args[0]=='-u':args=args[1:]
    if len(args)!=1 or (repo/args[0]).resolve()!=(repo/'run.py').resolve():
        raise SystemExit('Unexpected engine entrypoint. No files changed.')
    return argv[0]


def validate_dependencies(package, target, manifest):
    """Check local package imports against the resulting release before stopping."""
    todo = list(manifest)
    visited = set()
    while todo:
        name = todo.pop()
        if name in visited:
            continue
        visited.add(name)
        source = (package if name in manifest else target) / name
        for node in ast.walk(ast.parse(source.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    modules = [node.module] if node.module else [a.name for a in node.names]
                elif node.module == 'kadima':
                    modules = [a.name for a in node.names]
                elif node.module and node.module.startswith('kadima.'):
                    modules = [node.module[7:]]
            elif isinstance(node, ast.Import):
                modules = [a.name[7:] for a in node.names if a.name.startswith('kadima.')]
            for module in modules:
                dependency = 'kadima/' + module.replace('.', '/') + '.py'
                # Names imported from a package can also be attributes. Only
                # require actual source modules shipped in this release tree.
                if not (package / dependency).is_file():
                    continue
                if dependency not in manifest and not (target / dependency).is_file():
                    raise SystemExit('Release dependency missing: ' + dependency + '. No files changed.')
                todo.append(dependency)


PREFLIGHT = r'''
import json
import math
import re
import sys
stage = 'imports'
reason = 'exception'
broker_code = None
try:
    from kadima import config, storage
    from kadima.session import SESSION
    import SmartApi
    import pyotp
    stage = 'local_state'
    c = storage.conn()
    if c.execute("SELECT 1 FROM positions WHERE status != 'closed' LIMIT 1").fetchone():
        reason = 'local_positions_unresolved'
        raise RuntimeError()
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='execution_orders'").fetchone():
        if c.execute("SELECT 1 FROM execution_orders WHERE state != 'terminal' LIMIT 1").fetchone():
            reason = 'local_orders_pending'
            raise RuntimeError()
    stage = 'broker_login'
    SESSION.ensure()
    for name in ('position','orderBook'):
        stage = 'broker_positions' if name == 'position' else 'broker_orders'
        r = getattr(SESSION.api,name)()
        if not isinstance(r,dict) or r.get('status') is not True:
            reason = 'broker_response_unsuccessful'
            code = str(r.get('errorcode') or '') if isinstance(r,dict) else ''
            if re.fullmatch(r'[A-Z]{1,4}[0-9]{1,6}', code):
                broker_code = code
            raise RuntimeError()
        rows = r.get('data')
        if rows is None:
            rows = []
        if not isinstance(rows,list):
            reason = 'broker_response_invalid'
            raise RuntimeError()
        for row in rows:
            if not isinstance(row,dict):
                reason = 'broker_response_invalid'
                raise RuntimeError()
            if name == 'position':
                raw = row.get('netqty')
                if raw is None or isinstance(raw,bool) or not str(raw).strip():
                    reason = 'broker_response_invalid'
                    raise RuntimeError()
                quantity = float(raw)
                if not math.isfinite(quantity):
                    reason = 'broker_response_invalid'
                    raise RuntimeError()
                if quantity != 0:
                    reason = 'broker_positions_not_flat'
                    raise RuntimeError()
            elif str(row.get('orderstatus') or row.get('status') or '').lower() not in {
                'complete','completed','filled','cancelled','canceled','rejected'}:
                reason = 'broker_orders_pending_or_unknown'
                raise RuntimeError()
    print('KADIMA_PREFLIGHT_OK')
except Exception as exc:
    error = type(exc).__name__
    if error not in {'ModuleNotFoundError','ImportError','PermissionError',
                     'FileNotFoundError','RuntimeError','ValueError','OperationalError'}:
        error = 'OtherError'
    # Never include exception messages, broker payloads or credentials.
    print('KADIMA_DIAGNOSTIC ' + json.dumps(dict(stage=stage, reason=reason,
          error=error, broker_code=broker_code)), flush=True)
    sys.exit(1)
'''


STAGES = {'imports','local_state','broker_login','broker_positions','broker_orders'}
REASONS = {'exception','local_positions_unresolved','local_orders_pending',
           'broker_response_unsuccessful','broker_response_invalid',
           'broker_positions_not_flat','broker_orders_pending_or_unknown'}
ERRORS = {'ModuleNotFoundError','ImportError','PermissionError','FileNotFoundError',
          'RuntimeError','ValueError','OperationalError','OtherError'}


def failure_summary(exc, operation):
    if isinstance(exc, subprocess.TimeoutExpired):
        return operation + ': timed out'
    if isinstance(exc, subprocess.CalledProcessError):
        # Child output can contain SDK secrets. Render only validated diagnostics.
        for line in (exc.stdout or '').splitlines():
            if not line.startswith('KADIMA_DIAGNOSTIC '):
                continue
            try:
                d = json.loads(line[len('KADIMA_DIAGNOSTIC '):])
                if d.get('stage') in STAGES and d.get('reason') in REASONS and d.get('error') in ERRORS:
                    return d['stage'] + ': ' + d['reason'] + ' (' + d['error'] + ')'
            except (ValueError, TypeError, AttributeError):
                pass
        return operation + ': child command failed (exit ' + str(exc.returncode) + ')'
    error = type(exc).__name__
    return operation + ': ' + (error if error in ERRORS else 'OtherError')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo -n python3 '+str(Path(__file__).resolve()))
    # Updates are allowed at any hour; exposure and source checks still apply.
    manifest = json.loads((PACKAGE/'executor_fix/manifest.json').read_text())
    # Validate every source before stopping any service.
    for name, allowed in manifest.items():
        target = REPO/name
        source = PACKAGE/name
        compile(source.read_text(), str(source), 'exec')
        current = blob(target.read_bytes()) if target.exists() else None
        new = blob(source.read_bytes())
        if current not in allowed and current != new:
            raise SystemExit('Unexpected local version: '+name+'. No files changed.')
    validate_dependencies(PACKAGE, REPO, manifest)
    pid = run(['systemctl','show','kadima.service','-p','MainPID','--value']).stdout.strip()
    if not pid.isdigit() or int(pid) == 0:
        raise SystemExit('Engine must be running so its existing environment can be verified. No files changed.')
    env = {}
    for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            k,v=item.split(b'=',1)
            env[os.fsdecode(k)] = os.fsdecode(v)
    # Check exposure before stopping; repeat after stopping to catch new entries.
    db = Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db'
    if not db.is_file():
        raise SystemExit('Local position database is unavailable. No services stopped.')
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as local:
        if local.execute("SELECT 1 FROM positions WHERE status != 'closed' LIMIT 1").fetchone():
            raise SystemExit('Local positions/orders remain unresolved. No services stopped.')
        if local.execute("SELECT 1 FROM sqlite_master WHERE name='execution_orders'").fetchone():
            if local.execute("SELECT 1 FROM execution_orders WHERE state != 'terminal' LIMIT 1").fetchone():
                raise SystemExit('Pending execution intents remain. No services stopped.')
    python=engine_interpreter(Path('/proc')/pid,REPO)
    user=pwd.getpwnam('ubuntu')
    if Path('/proc/'+pid).stat().st_uid != user.pw_uid:
        raise SystemExit('Unexpected engine owner. No files changed.')
    def as_user():
        os.initgroups(user.pw_name,user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    # An expired contract can still have a non-zero quantity in the broker
    # book. Never infer settlement from its date or stop services just to
    # discover the same outstanding exposure again.
    try:
        check=run([python,'-c',PREFLIGHT],cwd=REPO,env=env,preexec_fn=as_user,timeout=60)
        if 'KADIMA_PREFLIGHT_OK' not in check.stdout:
            raise RuntimeError('Broker preflight did not confirm flat state')
    except Exception as exc:
        print('Failed check:',failure_summary(exc,'broker_preflight_before_stop'))
        raise SystemExit('Install stopped before service shutdown. No source files changed.')
    active=[u for u in UNITS if subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0]
    backup=Path(tempfile.mkdtemp(prefix='kadima-executor-backup-',dir='/home/ubuntu'))
    os.chmod(backup,0o700)
    for name in manifest:
        target=REPO/name
        if target.exists():
            saved=backup/name
            saved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(target,saved)
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
    copied=False
    operation='stop_services'
    try:
        run(['systemctl','stop',*active],timeout=60)
        operation='broker_preflight'
        check=run([python,'-c',PREFLIGHT],cwd=REPO,env=env,preexec_fn=as_user,timeout=60)
        if 'KADIMA_PREFLIGHT_OK' not in check.stdout:
            raise RuntimeError('Read-only broker preflight did not confirm flat state')
        operation='replace_source'
        for name in manifest:
            target=REPO/name
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.executor-new')
            shutil.copy2(PACKAGE/name,temporary)
            os.chown(temporary,user.pw_uid,user.pw_gid)
            os.replace(temporary,target)
            copied=True
        # Check the installed import graph before restarting any trading process.
        # Imports construct no Engine and start no feeds, workers or broker orders.
        operation='installed_import_check'
        run([python, '-c', 'import kadima.engine; import kadima.control_api; '
             'import kadima.stock_universe_scanner'], cwd=REPO, env=env,
            preexec_fn=as_user, timeout=30)
        operation='start_services'
        run(['systemctl','start',*active],timeout=60)
        operation='verify_services'
        for unit in active:
            run(['systemctl','is-active','--quiet',unit])
    except Exception as exc:
        detail = failure_summary(exc, operation)
        (backup/'install-status.json').write_text(json.dumps({'failed_check':detail}))
        print('Failed check:', detail)
        # Once restart is attempted, live entries may already have resumed.
        # Retain the installed source and recover services instead of reverting
        # code under a potentially open position. Never roll back the database.
        if copied and operation in {'start_services', 'verify_services'}:
            subprocess.run(['systemctl','start',*active],capture_output=True,timeout=60)
            print('Installed source retained after restart failure; trading may have resumed.')
            print('Check service health and broker/local positions before a recovery deployment.')
            print('Backup:',backup)
            raise SystemExit(1)
        # Before restart, the engine is stopped and source rollback is safe.
        if copied:
            run(['systemctl','stop',*active],timeout=60)
            for name,allowed in manifest.items():
                target=REPO/name
                old=backup/name
                if old.exists():
                    shutil.copy2(old,target)
                elif target.exists():
                    target.unlink()
        subprocess.run(['systemctl','start',*active],capture_output=True,timeout=60)
        print('Install stopped; original source restored where needed. Check service status.')
        print('Broker/local preflight must confirm no positions or pending orders.')
        print('Backup:',backup)
        raise SystemExit(1)
    print('Installed complete backend release; services active.')
    print('Backup:',backup)
    print('Live fill handling still requires observation in the next session; no test orders sent.')
    print('Forecast trailing remains disabled unless already explicitly configured.')


if __name__=='__main__':
    main()
