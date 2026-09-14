"""Guarded maintenance deployment. Run with sudo -n python3 install.py.

Stops the engine/scanner/macro services only after source and local-state
checks. Uses the engine's existing interpreter/environment for a read-only
broker preflight; never sends orders, changes credentials or trading flags.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

REPO = Path('/home/ubuntu/kadima-py')
PACKAGE = Path(__file__).resolve().parents[1]
UNITS = ['kadima.service', 'kadima-universe-scanner.service', 'kadima-macro-watch.service']


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


PREFLIGHT = '''
import json
from kadima import config, storage
from kadima.session import SESSION
c = storage.conn()
if c.execute("SELECT 1 FROM positions WHERE status != 'closed' LIMIT 1").fetchone():
    raise RuntimeError('Local positions or orders remain unresolved')
if c.execute("SELECT 1 FROM sqlite_master WHERE name='execution_orders'").fetchone():
    if c.execute("SELECT 1 FROM execution_orders WHERE state != 'terminal' LIMIT 1").fetchone():
        raise RuntimeError('Pending execution intents remain')
SESSION.ensure()
for name in ('position','orderBook'):
    r = getattr(SESSION.api,name)()
    if not isinstance(r,dict) or r.get('status') is not True:
        raise RuntimeError('Broker preflight unavailable')
    rows=r.get('data') or []
    if not isinstance(rows,list):
        raise RuntimeError('Broker preflight invalid')
    for row in rows:
        if name=='position':
            if float(row.get('netqty') or 0) != 0:
                raise RuntimeError('Broker positions are not flat')
        elif str(row.get('orderstatus') or row.get('status') or '').lower() not in {
            'complete','completed','filled','cancelled','canceled','rejected'}:
            raise RuntimeError('Broker has pending or unknown orders')
print('KADIMA_PREFLIGHT_OK')
'''


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo -n python3 '+str(Path(__file__).resolve()))
    hour = datetime.now(ZoneInfo('Asia/Kolkata')).hour
    if 8 <= hour < 19:
        raise SystemExit('Maintenance installer runs only from 19:00 to 08:00 IST. No files changed.')
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
    pid = run(['systemctl','show','kadima.service','-p','MainPID','--value']).stdout.strip()
    if not pid.isdigit() or int(pid) == 0:
        raise SystemExit('Engine must be running so its existing environment can be verified. No files changed.')
    env = {}
    for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            k,v=item.split(b'=',1)
            env[os.fsdecode(k)] = os.fsdecode(v)
    argv=Path('/proc/'+pid+'/cmdline').read_bytes().split(b'\0')
    python=os.fsdecode(argv[0])
    if not Path(python).is_absolute() or 'python' not in Path(python).name:
        raise SystemExit('Engine interpreter could not be verified. No files changed.')
    user=pwd.getpwnam('ubuntu')
    if Path('/proc/'+pid).stat().st_uid != user.pw_uid:
        raise SystemExit('Unexpected engine owner. No files changed.')
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
    try:
        run(['systemctl','stop',*active],timeout=60)
        def as_user():
            os.initgroups(user.pw_name,user.pw_gid)
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)
        check=run([python,'-c',PREFLIGHT],cwd=REPO,env=env,preexec_fn=as_user,timeout=60)
        if 'KADIMA_PREFLIGHT_OK' not in check.stdout:
            raise RuntimeError('Read-only broker preflight did not confirm flat state')
        for name in manifest:
            target=REPO/name
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.executor-new')
            shutil.copy2(PACKAGE/name,temporary)
            os.chown(temporary,user.pw_uid,user.pw_gid)
            os.replace(temporary,target)
            copied=True
        run(['systemctl','start',*active],timeout=60)
        for unit in active:
            run(['systemctl','is-active','--quiet',unit])
    except Exception:
        # Maintenance window + flat broker preflight ensure this rollback is
        # before a market entry cycle. Never roll back or replace the database.
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
    print('Installed executor safeguards; services active.')
    print('Backup:',backup)
    print('Live fill handling still requires observation in the next session; no test orders sent.')
    print('Forecast trailing remains disabled unless already explicitly configured.')


if __name__=='__main__':
    main()
