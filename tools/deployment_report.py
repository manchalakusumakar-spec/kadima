#!/usr/bin/env python3
"""Read-only EC2 inventory. No broker login, orders, service stops or writes.

Run from a reviewed checkout, or copy this file alone and run it with sudo -n.
No package imports or adjacent manifest are needed for live diagnostics.
Only selected status fields and source hashes are printed; never environments,
tokens, passwords, raw PM2 config, broker responses or database contents.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PACKAGE = Path(__file__).resolve().parents[1]
UNITS = ('kadima.service', 'kadima-universe-scanner.service',
         'kadima-macro-watch.service', 'kadima-dashboard.service')
SOURCE_FILES = ('kadima/engine.py', 'kadima/auto_executor.py', 'kadima/control_api.py',
                'kadima/angel_orders.py', 'kadima/shield_confirmation.py',
                'kadima/fast_momentum.py', 'kadima/cas_engine.py', 'kadima/cas_orders.py')
STOP_FILES = ('kadima/stop_runtime.py','kadima/regime_stops.py','kadima/structure_guard.py',
              'kadima/structure_levels.py','kadima/execution_ledger.py','kadima/config.py')
FLAGS = {'KADIMA_REGIME_STOPS_ENABLED': 'regime_stops_requested','KADIMA_CELESTIAL_ENABLED': 'celestial_rollout_env',
         'KADIMA_FAST_MOMENTUM_LIVE': 'fast_momentum_live_requested',
         'KADIMA_CAS_LIVE': 'cas_live_requested'}


def blob(path):
    try:
        if not path.is_file():return None
        data=path.read_bytes()
    except OSError:return None
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def run(args):
    try:
        r=subprocess.run(args,capture_output=True,text=True,timeout=10)
        return r.stdout if r.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):return ''


def age_seconds(value, now=None):
    try:
        ts=datetime.fromisoformat(value)
        if ts.tzinfo is None:return None
        return ((now or datetime.now(timezone.utc))-ts).total_seconds()
    except (ValueError, TypeError):return None


def api_checks(status):
    """API reachability and worker health are separate, including before startup."""
    if not isinstance(status,dict) or not isinstance(status.get('config'),dict):
        raise ValueError('Invalid API snapshot')
    checks={'celestial_api':True}
    for key in ('rollout_enabled','auto_trade_enabled','kill_latched','broker_orders_enabled','score_threshold'):
        value=status['config'].get(key)
        valid = type(value) is int if key == 'score_threshold' else type(value) is bool
        checks[key]=value if valid else None
    checks['snapshot_at']=status.get('ts') if age_seconds(status.get('ts')) is not None else None
    system=status.get('system')
    for name,key in (('reconciliation','broker_reconciliation_ok'),('news','news_ok')):
        health=system.get(name) if isinstance(system,dict) else None
        checks[key]=isinstance(health,dict) and health.get('ok') is True
        age=age_seconds(health.get('ts')) if isinstance(health,dict) else None
        checks[name+'_age_seconds']=round(age,1) if age is not None else None
        # Diagnostic age bounds, not replacements for executor entry gates.
        checks[name+'_fresh']=age is not None and 0 <= age <= (300 if name=='news' else 120)
    news=system.get('news') if isinstance(system,dict) else None
    news=news if isinstance(news,dict) else {}
    covered=news.get('covered_symbols')
    covered=covered if isinstance(covered,list) else []
    checks['news_covered_indices']=[name for name in ('NIFTY','BANKNIFTY','MIDCPNIFTY','SENSEX') if name in covered]
    errors=news.get('errors')
    checks['news_error_categories']=[name for name in ('announcements','board_meetings','corporate_actions',
        'financial_results','company_mapping') if isinstance(errors,dict) and name in errors]
    checks['news_scope']=news.get('scope') if news.get('scope')=='NSE_stock_corporate_RSS_only' else None
    rows=status.get('positions')
    checks['api_open_positions']=len(rows) if isinstance(rows,list) else None
    return checks


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_json(url, token=None):
    headers={'Authorization':'Bearer '+token} if token else {}
    req=urllib.request.Request(url,headers=headers,method='GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(req,timeout=10) as response:
        raw=response.read(2_000_001)
        if len(raw)>2_000_000:raise ValueError('oversize')
        return json.loads(raw)


def live_checks():
    checks={}
    try:
        health=local_json('http://127.0.0.1:8080/health')
        if not isinstance(health,dict):raise ValueError('schema')
        checks['health']={key:health.get(key) if type(health.get(key)) is bool else None
                          for key in ('ok','engine_attached','market_open')}
    except Exception:checks['health']={'ok':None,'error':'local_health_unavailable'}
    try:
        config=json.loads(Path('/etc/kadima-dashboard/config.json').read_text())
        token=config.get('api_token')
        if not isinstance(token,str) or not token:raise ValueError('token')
    except Exception:
        checks.update(celestial_api=False,control_api_error='dashboard_credentials_unavailable')
        return checks
    try:
        checks.update(api_checks(local_json('http://127.0.0.1:8081/api/kadima/top20',token)))
    except urllib.error.HTTPError as exc:
        checks.update(celestial_api=False,control_api_http_status=exc.code)
    except Exception:
        checks.update(celestial_api=False,control_api_error='unreachable_or_invalid_snapshot')
    return checks


def celestial_blockers(checks):
    """Only Celestial controls; disabled rollout does not describe legacy trading."""
    if checks.get('celestial_api') is not True:return ['authenticated_control_status_unavailable']
    blockers=[]
    for key in ('rollout_enabled','auto_trade_enabled','broker_orders_enabled'):
        if checks.get(key) is not True:
            blockers.append(key+('_off' if checks.get(key) is False else '_unknown'))
    if checks.get('kill_latched') is not False:
        blockers.append('kill_latched' if checks.get('kill_latched') is True else 'kill_status_unknown')
    for name,key in (('reconciliation','broker_reconciliation_ok'),('news','news_ok')):
        if checks.get(key) is not True:blockers.append(name+'_not_healthy')
        if checks.get(name+'_fresh') is not True:blockers.append(name+'_stale_or_unknown')
    return blockers


def report(target):
    target=target.resolve()
    result=dict(timestamp=datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),
                read_only=True,target=str(target),source={},services={},checks={})
    try:
        manifest=json.loads((PACKAGE/'executor_fix/manifest.json').read_text())
        if not isinstance(manifest,dict) or not all(isinstance(k,str) and isinstance(v,list)
                                                    for k,v in manifest.items()):manifest={}
    except (OSError,ValueError):manifest={}
    independent=PACKAGE.resolve()!=target and bool(manifest)
    result['source_comparison']='separate_release_checkout' if independent else 'unavailable_inventory_only'
    if not manifest:manifest={name:[] for name in SOURCE_FILES}
    for name in STOP_FILES:
        manifest.setdefault(name,[])
    for name,allowed in manifest.items():
        actual,expected=blob(target/name),blob(PACKAGE/name) if independent else None
        result['source'][name]=dict(installed=actual,release=expected,
            matches_release=actual==expected if expected is not None else None,
            known_base=actual in allowed if independent else None)
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
            for name,key in FLAGS.items():
                result['checks'][key]=env.get(name.encode(),b'0')==b'1'
            cwd=Path('/proc',pid,'cwd').resolve(strict=True)
            result['checks']['engine_uses_target_directory']=cwd==target
        except (OSError,UnicodeError):result['checks']['engine_environment_readable']=False
    db=data_dir/'kadima.db'
    try:
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
            result['checks']['local_open_positions']=c.execute("SELECT count(*) FROM positions WHERE status != 'closed'").fetchone()[0]
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['checks']['pending_execution_intents']=c.execute("SELECT count(*) FROM execution_orders WHERE state != 'terminal'").fetchone()[0] if 'execution_orders' in tables else 0
    except (sqlite3.Error,ValueError):result['checks']['local_database_readable']=False
    result['checks'].update(live_checks())
    result['celestial_control_blockers']=celestial_blockers(result['checks'])
    result['dashboard']={name:blob(Path(name)) for name in
        ('/opt/kadima-dashboard/gateway.py','/var/www/html/dashboard.html')}
    result['conclusion']='INVENTORY_ONLY_NOT_LIVE_RELEASE_APPROVAL'
    result['note']='No broker login or orders. Empty control blockers do not verify market data, entry eligibility or fills.'
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--target',type=Path,default=Path('/home/ubuntu/kadima-py'))
    print(json.dumps(report(parser.parse_args().target),indent=2))
