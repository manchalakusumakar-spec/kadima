"""Add the read-only /api/forecast/live route (control_api.py) and allow it through the dashboard
gateway (/opt/kadima-dashboard/gateway.py). Anchored, idempotent, backups kept, compile-checked.

    python3 tools/install_live_forecast_api_20261028.py --check
    sudo python3 tools/install_live_forecast_api_20261028.py
    sudo python3 tools/install_live_forecast_api_20261028.py --rollback
"""
import os
import shutil
import subprocess
import sys

ROOT = os.environ.get('KADIMA_ROOT', '/home/ubuntu/kadima-py')
GW = os.environ.get('KADIMA_GATEWAY', '/opt/kadima-dashboard/gateway.py')
CA = os.path.join(ROOT, 'kadima', 'control_api.py')
SUF = '.bak-live-20261028'
MARK = '# LIVE-FORECAST-20261028'
CA_ANCHOR = "@app.get('/api/forecast/days')\n"
CA_INSERT = ("@app.get('/api/forecast/live')   " + MARK + "\n"
             "def forecast_live(index: str = Query('NIFTY'), authorization: Optional[str] = Header(None)):\n"
             "    \"\"\"Live forecast line + sharp-move meter (display only, read-only).\"\"\"\n"
             "    _check_auth(authorization)\n"
             "    from . import live_forecast\n"
             "    return live_forecast.snapshot(_forecast_index(index))\n\n\n")
GW_OLD = "if sub not in ('hourly', 'days'):"
GW_NEW = "if sub not in ('hourly', 'days', 'live'):   " + MARK


def py():
    v = os.path.join(ROOT, 'venv', 'bin', 'python')
    return v if os.path.exists(v) else sys.executable


def plan():
    ca, gw = open(CA).read(), open(GW).read()
    out = []
    if MARK not in ca:
        if ca.count(CA_ANCHOR) != 1:
            sys.exit('ABORT: /api/forecast/days route not found exactly once in control_api.py; nothing changed')
        out.append((CA, ca.replace(CA_ANCHOR, CA_INSERT + CA_ANCHOR, 1)))
    if MARK not in gw:
        if gw.count(GW_OLD) != 1:
            sys.exit('ABORT: forecast allow-list line not found exactly once in gateway.py; nothing changed')
        out.append((GW, gw.replace(GW_OLD, GW_NEW, 1)))
    return out


def main():
    a = sys.argv[1:]
    if '--rollback' in a:
        for f in (CA, GW):
            if os.path.exists(f + SUF):
                shutil.copy2(f + SUF, f)
                print('restored', f)
        return
    todo = plan()
    if not todo:
        print('already installed')
        return
    if '--check' in a:
        print('check ok:', ', '.join(f for f, _ in todo))
        return
    for f, new in todo:
        tmp = f + '.new-live'
        open(tmp, 'w').write(new)
        if subprocess.call([py(), '-m', 'py_compile', tmp]) != 0:
            os.remove(tmp)
            sys.exit('ABORT: ' + f + ' would not compile; nothing changed for it')
        shutil.copy2(f, f + SUF)
        st = os.stat(f)
        os.replace(tmp, f)
        os.chown(f, st.st_uid, st.st_gid) if os.geteuid() == 0 else None
        os.chmod(f, st.st_mode)
        print('patched', f, '(backup', f + SUF + ')')


if __name__ == '__main__':
    main()
