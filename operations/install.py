"""Install only root-owned, fixed-operation socket helper. No trading changes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE=Path(__file__).resolve().parent
TARGETS={'control.py':Path('/opt/kadima-operations/control.py'),
 'kadima-operations.socket':Path('/etc/systemd/system/kadima-operations.socket'),
 'kadima-operations.service':Path('/etc/systemd/system/kadima-operations.service')}


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30)


def main():
    if os.geteuid()!=0:raise SystemExit('Run with sudo -n python3 operations/install.py')
    manifest=json.loads((BASE/'manifest.json').read_text())
    for name,target in TARGETS.items():
        if target.is_symlink():raise SystemExit('Refusing symlink: '+name)
        if target.exists() and digest(target) not in manifest[name]+[digest(BASE/name)]:
            raise SystemExit('Unreviewed operations installation: '+name)
    compile((BASE/'control.py').read_text(),'control.py','exec')
    root=Path('/var/lib/kadima-operations');root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or root.stat().st_uid!=0 or root.stat().st_mode&0o022:
        raise SystemExit('Untrusted operations state directory')
    target_dir=Path('/opt/kadima-operations');target_dir.mkdir(mode=0o755,exist_ok=True)
    if target_dir.is_symlink() or target_dir.stat().st_uid!=0 or target_dir.stat().st_mode&0o022:
        raise SystemExit('Untrusted operations code directory')
    with (root/'operation.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('An operations action is active; retry after completion')
        backup=Path(tempfile.mkdtemp(prefix='kadima-operations-',dir='/var/backups'))
        os.chmod(backup,0o700)
        for name,target in TARGETS.items():
            if target.exists():shutil.copy2(target,backup/name)
        # No gateway privilege changes and no engine/scanner/macro restarts.
        subprocess.run(['systemctl','stop','kadima-operations.socket','kadima-operations.service'],capture_output=True,timeout=30)
        try:
            for name,target in TARGETS.items():
                temp=target.with_suffix(target.suffix+'.new')
                shutil.copyfile(BASE/name,temp);os.chown(temp,0,0);os.chmod(temp,0o644);os.replace(temp,target)
            run(['systemctl','daemon-reload'])
            run(['systemctl','enable','--now','kadima-operations.socket'])
            run(['systemctl','start','kadima-operations.service'])
            run(['systemctl','is-active','--quiet','kadima-operations.socket','kadima-operations.service'])
        except Exception:
            subprocess.run(['systemctl','stop','kadima-operations.socket','kadima-operations.service'],capture_output=True,timeout=30)
            for name,target in TARGETS.items():
                if (backup/name).exists():shutil.copy2(backup/name,target)
                elif target.exists():target.unlink()
            subprocess.run(['systemctl','daemon-reload'],capture_output=True,timeout=30)
            if (backup/'control.py').exists():
                subprocess.run(['systemctl','start','kadima-operations.socket'],capture_output=True,timeout=30)
            raise SystemExit('Operations setup failed; source restored. Backup: '+str(backup)) from None
        print('Guarded website operations installed. Backup: '+str(backup))
        print('No engine/scanner/macro restart; no broker requests; gateway privilege settings preserved.')


if __name__=='__main__':main()
