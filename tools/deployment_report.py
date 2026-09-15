#!/usr/bin/env python3
"""Read-only EC2 inventory. No broker login, orders, service stops or writes.

Run from the reviewed checkout with sudo -n python3 tools/deployment_report.py.
Only selected status fields and source hashes are printed; never environments,
tokens, passwords, raw PM2 config, broker responses or database contents.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

PACKAGE = Path(__file__).resolve().parents[1]
UNITS = ('kadima.service', 'kadima-universe-scanner.service',
         'kadima-macro-watch.service', 'kadima-dashboard.service')


def blob(path):
    if not path.is_file():return None
    data=path.read_bytes()
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def run(args):
    try:
        r=subprocess.run(args,capture_output=True,text=True,timeout=10)
        return r.stdout if r.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):return ''


def report(target):
    result=dict(timestamp=datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),
                read_only=True,target=str(target),source={},services={},checks={})
    manifest=json.loads((PACKAGE/'executor_fix/manifest.json').read_text())
    for name,allowed in manifest.items():
        actual,expected=blob(target/name),blob(PACKAGE/name)
        result['source'][name]=dict(installed=actual,release=expected,
            matches_release=actual==expected,known_base=actual in allowed)
    for unit in UNITS:
        fields={}
        raw=run(['systemctl','show',unit,'-p','ActiveState','-p','SubState','-p','MainPID'])
        for line in raw.splitlines():
            if '=' in line:
                k,v=line.split('=',1)
                if k in ('ActiveState','SubState','MainPID'):fields[k]=v
        result['services'][unit]=fields or {'available':False}
    # PM2 output is parsed locally and never echoed, even on parse failure.
    raw=run(['sudo','-n','-u','ubuntu','pm2','jlist']) if Path('/home/ubuntu/.pm2/rpc.sock').exists() else ''
    try:
        entries=json.loads(raw)
        result['pm2']=[dict(name=p['name'],pid=p.get('pid'),status=p.get('pm2_env',{}).get('status'))
                       for p in entries if p.get('name') in ('kadima-bridge','kadima','kadima-engine')]
    except (ValueError,TypeError):result['pm2']=[]
    pid=result['services']['kadima.service'].get('MainPID','0')
    data_dir=Path('/var/lib/kadima')
    if pid.isdigit() and int(pid)>0:
        try:
            env=dict(item.split(b'=',1) for item in Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in item)
            if b'KADIMA_DATA_DIR' in env:data_dir=Path(env[b'KADIMA_DATA_DIR'].decode())
            result['checks']['celestial_rollout_env']=env.get(b'KADIMA_CELESTIAL_ENABLED',b'0')==b'1'
        except (OSError,UnicodeError):result['checks']['engine_environment_readable']=False
    db=data_dir/'kadima.db'
    try:
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
            result['checks']['local_open_positions']=c.execute("SELECT count(*) FROM positions WHERE status != 'closed'").fetchone()[0]
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['checks']['pending_execution_intents']=c.execute("SELECT count(*) FROM execution_orders WHERE state != 'terminal'").fetchone()[0] if 'execution_orders' in tables else 0
    except (sqlite3.Error,ValueError):result['checks']['local_database_readable']=False
    try:
        config=json.loads(Path('/etc/kadima-dashboard/config.json').read_text())
        req=urllib.request.Request('http://127.0.0.1:8081/api/kadima/top20',headers={'Authorization':'Bearer '+config['api_token']})
        with urllib.request.urlopen(req,timeout=10) as r:status=json.load(r)
        result['checks']['celestial_api']=True
        for key in ('rollout_enabled','auto_trade_enabled','kill_latched','broker_orders_enabled','score_threshold'):
            value=status.get('config',{}).get(key)
            if isinstance(value,(bool,int)) or value is None:result['checks'][key]=value
        result['checks']['broker_reconciliation_ok']=status.get('system',{}).get('reconciliation',{}).get('ok') is True
        result['checks']['news_ok']=status.get('system',{}).get('news',{}).get('ok') is True
    except Exception:result['checks']['celestial_api']=False
    result['dashboard']={name:blob(Path(name)) for name in
        ('/opt/kadima-dashboard/gateway.py','/var/www/html/dashboard.html')}
    result['conclusion']='INVENTORY_ONLY_NOT_LIVE_RELEASE_APPROVAL'
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--target',type=Path,default=Path('/home/ubuntu/kadima-py'))
    print(json.dumps(report(parser.parse_args().target),indent=2))
