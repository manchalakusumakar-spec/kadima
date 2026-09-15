#!/usr/bin/env python3
"""Existing gateway/HTML update with exact source checks, backup and rollback.

Default is check-only. --apply updates these two files and restarts only the
dashboard gateway. Credentials, Nginx, trading flags and engine are preserved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BASE=Path(__file__).resolve().parent
TARGETS={'gateway.py':Path('/opt/kadima-dashboard/gateway.py'),
         'dashboard.html':Path('/var/www/html/dashboard.html')}
UNIT='kadima-dashboard.service'


def blob(path):
    data=path.read_bytes()
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def run(args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=20,**kwargs)


def check_versions(targets, manifest):
    for name,target in targets.items():
        if target.is_symlink() or not target.is_file():
            raise ValueError('Expected a regular installed '+name)
        if blob(target) not in set(manifest[name]) | {blob(BASE/name)}:
            raise ValueError('Unreviewed installed version: '+name)


def atomic_copy(source,target):
    stat=target.stat()
    temporary=target.with_name(target.name+'.celestial-new')
    shutil.copyfile(source,temporary)
    os.chmod(temporary,stat.st_mode & 0o777)
    os.chown(temporary,stat.st_uid,stat.st_gid)
    os.replace(temporary,target)


def wait_for_backend(request):
    """Allow a bounded restart delay; reject auth/schema failures immediately."""
    deadline=time.monotonic()+30
    while True:
        try:
            with urllib.request.urlopen(request,timeout=2) as response:
                backend=json.load(response)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            if time.monotonic()>=deadline:
                raise ValueError('Backend did not become ready') from None
            time.sleep(1)
            continue
        if not isinstance(backend,dict) or backend.get('dashboard_limit')!=20:
            raise ValueError('Updated Celestial backend required first')
        return backend


def main(apply=False):
    if os.geteuid()!=0:raise SystemExit('Run with sudo -n python3 dashboard_live/upgrade.py')
    manifest=json.loads((BASE/'upgrade_manifest.json').read_text())
    check_versions(TARGETS,manifest)
    config_path=Path('/etc/kadima-dashboard/config.json')
    original_config=config_path.read_bytes()
    config=json.loads(original_config)
    request=urllib.request.Request('http://127.0.0.1:8081/api/kadima/config',
        headers={'Authorization':'Bearer '+config['api_token']})
    wait_for_backend(request)
    pid=run(['systemctl','show',UNIT,'-p','MainPID','--value']).stdout.strip()
    if not pid.isdigit() or int(pid)==0:raise ValueError('Existing gateway must be running')
    process=Path('/proc')/pid
    argv=process.joinpath('cmdline').read_bytes().split(b'\0')
    if b'gateway:app' not in argv or process.joinpath('cwd').resolve()!=TARGETS['gateway.py'].parent:
        raise ValueError('Unexpected gateway entrypoint')
    python=os.fsdecode(argv[0])
    if not Path(python).is_absolute() or 'python' not in Path(python).name:
        raise ValueError('Unverified gateway interpreter')
    user=pwd.getpwnam('ubuntu')
    if process.stat().st_uid!=user.pw_uid:raise ValueError('Unexpected gateway owner')
    compile((BASE/'gateway.py').read_text(),'gateway.py','exec')
    if not apply:
        print('Dashboard preflight passed. No files changed. --apply updates gateway and HTML together.')
        return
    backup=Path(tempfile.mkdtemp(prefix='kadima-dashboard-upgrade-',dir='/var/backups'))
    os.chmod(backup,0o700)
    for name,target in TARGETS.items():shutil.copy2(target,backup/name)
    try:
        run(['systemctl','stop',UNIT])
        # Recheck after service stop before replacing a changed live file.
        check_versions(TARGETS,manifest)
        for name,target in TARGETS.items():atomic_copy(BASE/name,target)
        run(['systemctl','start',UNIT])
        healthy=False
        for _ in range(15):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8082/health',timeout=2) as r:
                    healthy=json.load(r).get('ok') is True
                if healthy:break
            except Exception:pass
            time.sleep(1)
        if not healthy:raise ValueError('Gateway health check failed')
        if config_path.read_bytes()!=original_config:raise ValueError('Private configuration changed concurrently')
        for name,target in TARGETS.items():
            if blob(target)!=blob(BASE/name):raise ValueError('Installed source hash mismatch')
    except Exception:
        run(['systemctl','stop',UNIT])
        for name,target in TARGETS.items():atomic_copy(backup/name,target)
        run(['systemctl','start',UNIT])
        raise
    print('Dashboard updated; gateway healthy. Sign in again. Backup: '+str(backup))
    print('Engine and trading flags were not changed. Browser/live-source validation remains required.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    try:main(parser.parse_args().apply)
    except Exception as exc:
        # Do not echo upstream exceptions, private config, process args or headers.
        raise SystemExit('Dashboard update stopped: '+type(exc).__name__+'. Inspect service state before proceeding.')
