"""Create a 1 GiB emergency swap file with source/space checks; never swapoff.

Uses a systemd swap unit, preserving /etc/fstab and existing swap settings.
Run with sudo -n python3 tools/install_swap_buffer.py --apply.
"""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess

PATH=Path('/var/lib/kadima-swapfile')
SIZE=1024**3
UNIT_PATH=Path('/etc/systemd/system/var-lib-kadima\\x2dswapfile.swap')
UNIT='''[Unit]
Description=KADIMA emergency swap buffer

[Swap]
What=/var/lib/kadima-swapfile

[Install]
WantedBy=swap.target
'''


def run(args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=45)


def memory():
    with open('/proc/meminfo') as f:
        values={line.split(':',1)[0]:int(line.split()[1])*1024 for line in f if line.startswith(('MemTotal:','MemAvailable:','SwapTotal:'))}
    return values


def check():
    values=memory()
    if values['SwapTotal']>0:
        return 'existing_swap'
    if PATH.exists() or PATH.is_symlink() or UNIT_PATH.exists() or UNIT_PATH.is_symlink():
        raise ValueError('existing_unverified_swap_file_or_unit')
    if values['MemAvailable']<values['MemTotal']*.25:raise ValueError('defer_setup_until_memory_recovers')
    disk=shutil.disk_usage(PATH.parent)
    if disk.free-SIZE<max(3*SIZE,disk.total*.20):raise ValueError('insufficient_disk_headroom')
    fs=run(['findmnt','-n','-o','FSTYPE','--target',str(PATH.parent)]).stdout.strip()
    if fs not in ('ext4','xfs'):raise ValueError('filesystem_requires_separate_swap_setup')
    if run(['systemd-escape','--path','--suffix=swap',str(PATH)]).stdout.strip()!=UNIT_PATH.name:
        raise ValueError('unexpected_swap_unit_name')
    return 'ready'


def main(apply=False):
    if os.geteuid()!=0:raise SystemExit('Run with sudo -n python3 tools/install_swap_buffer.py --apply')
    with open('/run/kadima-swap-setup.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=check()
        if state=='existing_swap':
            print('Existing swap is configured; no additional buffer created.');return
        if not apply:
            print('Swap preflight passed; --apply creates and enables a 1 GiB buffer.');return
        created=False;active=False
        try:
            fd=os.open(PATH,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            created=True
            with os.fdopen(fd,'wb') as f:
                block=b'\0'*(1024*1024)
                for _ in range(1024):f.write(block)
                f.flush();os.fsync(f.fileno())
            run(['mkswap',str(PATH)])
            # Create without overwriting any concurrently added unit.
            with UNIT_PATH.open('x') as f:f.write(UNIT)
            os.chmod(UNIT_PATH,0o644)
            run(['systemctl','daemon-reload'])
            run(['systemctl','start',UNIT_PATH.name]);active=True
            run(['systemctl','enable',UNIT_PATH.name])
            if memory()['SwapTotal']<SIZE-1024*1024:raise ValueError('swap_not_confirmed')
        except Exception:
            # A successfully activated buffer must never be removed or swapoff'd
            # as automatic rollback under memory pressure.
            if created and not active:
                try:
                    active=str(PATH) in Path('/proc/swaps').read_text()
                except OSError:active=True
                if not active:
                    PATH.unlink(missing_ok=True)
                    if UNIT_PATH.is_file() and UNIT_PATH.read_text()==UNIT:UNIT_PATH.unlink()
                    subprocess.run(['systemctl','daemon-reload'],capture_output=True,timeout=30)
            raise SystemExit('Swap setup incomplete. Inspect swapon --show and the swap unit; no swapoff was attempted.') from None
        print('1 GiB swap buffer active and enabled at boot. No trading services restarted.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    try:main(parser.parse_args().apply)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        raise SystemExit('Swap preflight stopped: '+str(exc) if isinstance(exc,ValueError) else 'Swap preflight unavailable; no trading services changed.') from None
