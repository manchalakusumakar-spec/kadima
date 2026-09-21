"""Create a 1 GiB emergency swap file with source/space checks; never swapoff.

Plan-only by default, including no lock-file writes. Uses a systemd swap unit,
preserving /etc/fstab, sysctl settings and existing swap. No trading restarts.
Run with sudo -n python3 tools/install_swap_buffer.py --apply.
"""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess

PATH=Path('/var/lib/kadima-swapfile')
SIZE=1024**3
LOCK_PATH=Path('/run/kadima-swap-setup.lock')
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


def own_swap_active():
    return any(line.split()[0] == str(PATH)
               for line in Path('/proc/swaps').read_text().splitlines()[1:]
               if line.split())


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
    if not apply:
        state=check()
        print('Existing swap is configured; no additional buffer needed.' if state=='existing_swap'
              else 'Swap preflight passed; --apply creates and enables a 1 GiB buffer. No changes made.')
        return
    if os.geteuid()!=0:raise SystemExit('Run with sudo -n python3 tools/install_swap_buffer.py --apply')
    fd=os.open(LOCK_PATH,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=check()
        if state=='existing_swap':
            print('Existing swap is configured; no additional buffer created.');return
        try:
            fd=os.open(PATH,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as f:
                remaining=SIZE
                block=b'\0'*min(1024*1024,SIZE)
                while remaining:
                    chunk=block[:min(len(block),remaining)]
                    f.write(chunk)
                    remaining-=len(chunk)
                f.flush();os.fsync(f.fileno())
            run(['mkswap',str(PATH)])
            # Create without overwriting any concurrently added unit.
            with UNIT_PATH.open('x') as f:f.write(UNIT)
            os.chmod(UNIT_PATH,0o644)
            run(['systemctl','daemon-reload'])
            run(['systemctl','start',UNIT_PATH.name])
            run(['systemctl','enable',UNIT_PATH.name])
            if not own_swap_active():raise ValueError('swap_not_confirmed')
        except Exception:
            # Activation can succeed even if the command times out. Retain all
            # partial files for inspection; never guess that deleting is safe.
            raise SystemExit('Swap setup incomplete. Partial files retained. Inspect swapon --show and the swap unit; no swapoff or deletion was attempted.') from None
        print('1 GiB swap buffer active and enabled at boot. No trading services restarted.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    try:main(parser.parse_args().apply)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        raise SystemExit('Swap preflight stopped: '+str(exc) if isinstance(exc,ValueError) else 'Swap preflight unavailable; no trading services changed.') from None
