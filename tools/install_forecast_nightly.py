"""Install the nightly forecast maintenance as a systemd timer (16:05 IST, Mon-Fri).

    sudo python3 tools/install_forecast_nightly.py            # install / update
    sudo python3 tools/install_forecast_nightly.py --remove   # uninstall
    sudo python3 tools/install_forecast_nightly.py --run-now  # install and run once now

Reuses kadima.service's own user, working directory, Python and environment
(credentials stay where they already are; nothing is copied or printed), so the
nightly job logs in to Angel One exactly as the engine does. It runs after the
engine has stopped and logged out at 15:40.
"""
import os
import re
import subprocess
import sys

UNIT = 'kadima-forecast-nightly'
ETC = '/etc/systemd/system'


def sh(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def main():
    if os.geteuid() != 0:
        print('Run with sudo.')
        return 1
    if '--remove' in sys.argv:
        sh('systemctl', 'disable', '--now', UNIT + '.timer', check=False)
        for ext in ('.service', '.timer'):
            path = os.path.join(ETC, UNIT + ext)
            if os.path.exists(path):
                os.remove(path)
        sh('systemctl', 'daemon-reload')
        print('REMOVED')
        return 0
    src = sh('systemctl', 'cat', 'kadima').stdout
    keep, python, workdir = [], None, None
    section = None
    for line in src.splitlines():
        s = line.strip()
        if s.startswith('['):
            section = s
            continue
        if section != '[Service]' or not s or s.startswith('#'):
            continue
        key = s.split('=', 1)[0]
        if key in ('User', 'Group', 'Environment', 'EnvironmentFile'):
            keep.append(s)
        elif key == 'WorkingDirectory':
            workdir = s.split('=', 1)[1]
        elif key == 'ExecStart' and python is None:
            m = re.search(r'(\S*python[0-9.]*)', s.split('=', 1)[1])
            if m:
                python = m.group(1).lstrip('-@+!:')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workdir = workdir or root
    if not python or not os.path.exists(python):
        print('STOPPED - could not find the Python used by kadima.service:', python)
        return 1
    service = '\n'.join([
        '[Unit]', 'Description=KADIMA hourly forecast nightly maintenance (history, replay, retune)',
        'After=network-online.target', '',
        '[Service]', 'Type=oneshot', 'Nice=10', 'TimeoutStartSec=1800',
        f'WorkingDirectory={workdir}', *keep,
        f'ExecStart={python} {os.path.join(root, "tools", "forecast_nightly.py")}', ''])
    timer = '\n'.join([
        '[Unit]', 'Description=Run KADIMA forecast nightly maintenance at 16:05 IST on weekdays', '',
        '[Timer]', 'OnCalendar=Mon..Fri *-*-* 10:35:00 UTC', 'Persistent=false', '',
        '[Install]', 'WantedBy=timers.target', ''])
    with open(os.path.join(ETC, UNIT + '.service'), 'w') as f:
        f.write(service)
    with open(os.path.join(ETC, UNIT + '.timer'), 'w') as f:
        f.write(timer)
    sh('systemctl', 'daemon-reload')
    sh('systemctl', 'enable', '--now', UNIT + '.timer')
    nxt = sh('systemctl', 'list-timers', UNIT + '.timer', '--no-pager', check=False).stdout.splitlines()
    print('INSTALL_OK -', nxt[1].strip() if len(nxt) > 1 else 'timer enabled')
    print('Logs: journalctl -u', UNIT, '--since today')
    if '--run-now' in sys.argv:
        print('Running once now (about a minute)...')
        sh('systemctl', 'start', UNIT + '.service', check=False)
        print(sh('journalctl', '-u', UNIT, '-n', '20', '--no-pager', '-o', 'cat', check=False).stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
