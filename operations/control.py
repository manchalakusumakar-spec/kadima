"""Root-owned fixed-operation broker, available only on a private Unix socket.

Never accepts commands, unit names, paths or credentials from the browser.
Only the named three KADIMA services may be controlled. No financial order is
submitted, modified or cancelled. Engine stops need an acknowledged entry hold
and a successful local/broker flat-state check.
"""
import fcntl
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
from datetime import datetime,time as wall_time
from zoneinfo import ZoneInfo

UNITS={'engine':'kadima.service','scanner':'kadima-universe-scanner.service','macro':'kadima-macro-watch.service'}
ROOT=Path('/var/lib/kadima-operations')
SCRIPT='/opt/kadima-operations/control.py'
REPO=Path('/home/ubuntu/kadima-py')
ACTION_LOCK=threading.Lock()
PREFLIGHT = '\nimport json\nimport math\nimport re\nimport sys\nstage = \'imports\'\nreason = \'exception\'\nbroker_code = None\ntry:\n    from kadima import config, storage, audit\n    audit.log = lambda *args, **kwargs: None\n    from kadima.session import SESSION\n    import SmartApi\n    import pyotp\n    stage = \'local_state\'\n    c = storage.conn()\n    if c.execute("SELECT 1 FROM positions WHERE status != \'closed\' LIMIT 1").fetchone():\n        reason = \'local_positions_unresolved\'\n        raise RuntimeError()\n    if c.execute("SELECT 1 FROM sqlite_master WHERE name=\'execution_orders\'").fetchone():\n        if c.execute("SELECT 1 FROM execution_orders WHERE state != \'terminal\' LIMIT 1").fetchone():\n            reason = \'local_orders_pending\'\n            raise RuntimeError()\n    stage = \'broker_login\'\n    SESSION.ensure()\n    for name in (\'position\',\'orderBook\'):\n        stage = \'broker_positions\' if name == \'position\' else \'broker_orders\'\n        r = getattr(SESSION.api,name)()\n        if not isinstance(r,dict) or r.get(\'status\') is not True:\n            reason = \'broker_response_unsuccessful\'\n            code = str(r.get(\'errorcode\') or \'\') if isinstance(r,dict) else \'\'\n            if re.fullmatch(r\'[A-Z]{1,4}[0-9]{1,6}\', code):\n                broker_code = code\n            raise RuntimeError()\n        rows = r.get(\'data\')\n        if rows is None:\n            rows = []\n        if not isinstance(rows,list):\n            reason = \'broker_response_invalid\'\n            raise RuntimeError()\n        for row in rows:\n            if not isinstance(row,dict):\n                reason = \'broker_response_invalid\'\n                raise RuntimeError()\n            if name == \'position\':\n                raw = row.get(\'netqty\')\n                if raw is None or isinstance(raw,bool) or not str(raw).strip():\n                    reason = \'broker_response_invalid\'\n                    raise RuntimeError()\n                quantity = float(raw)\n                if not math.isfinite(quantity):\n                    reason = \'broker_response_invalid\'\n                    raise RuntimeError()\n                if quantity != 0:\n                    reason = \'broker_positions_not_flat\'\n                    raise RuntimeError()\n            elif str(row.get(\'orderstatus\') or row.get(\'status\') or \'\').lower() not in {\n                \'complete\',\'completed\',\'filled\',\'cancelled\',\'canceled\',\'rejected\'}:\n                reason = \'broker_orders_pending_or_unknown\'\n                raise RuntimeError()\n    print(\'KADIMA_PREFLIGHT_OK\')\nexcept Exception as exc:\n    error = type(exc).__name__\n    if error not in {\'ModuleNotFoundError\',\'ImportError\',\'PermissionError\',\n                     \'FileNotFoundError\',\'RuntimeError\',\'ValueError\',\'OperationalError\'}:\n        error = \'OtherError\'\n    # Never include exception messages, broker payloads or credentials.\n    print(\'KADIMA_DIAGNOSTIC \' + json.dumps(dict(stage=stage, reason=reason,\n          error=error, broker_code=broker_code)), flush=True)\n    sys.exit(1)\n'


def run(args,timeout=15,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout,**kwargs)


def protected(name,now=None):
    now=now or datetime.now(ZoneInfo('Asia/Kolkata'))
    close=wall_time(23,30) if name=='macro' else wall_time(15,40)
    return now.weekday()<5 and wall_time(9)<=now.time()<close


def api(path,body=None):
    config=json.loads(Path('/etc/kadima-dashboard/config.json').read_text())
    req=urllib.request.Request('http://127.0.0.1:8081/api/operations/'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+config['api_token'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=8) as response:
        raw=response.read(256*1024+1)
    if len(raw)>256*1024:raise ValueError('oversized_backend_reply')
    data=json.loads(raw)
    if not isinstance(data,dict):raise ValueError('invalid_backend_reply')
    return data


def saved():
    try:
        with (ROOT/'state.json').open() as f: data=json.loads(f.read(8193))
        if not isinstance(data,dict):raise ValueError('invalid_state')
        return data
    except FileNotFoundError:
        return {}


def save(data):
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp=ROOT/'state.new'
    with tmp.open('w') as f:
        os.chmod(tmp,0o600);json.dump(data,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,ROOT/'state.json')


def unit_state(unit):
    text=run(['systemctl','show',unit,'--property=LoadState,ActiveState,SubState,MainPID']).stdout
    props=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
    return {k:props.get(k,'unknown') for k in ('LoadState','ActiveState','SubState','MainPID')}


def status():
    data=saved();services=[]
    for name,unit in UNITS.items():
        row=unit_state(unit)
        timer=unit_state('kadima-delay-'+name+'.timer')
        services.append(dict(id=name,state=row['ActiveState'],detail=row['SubState'],
            installed=row['LoadState']=='loaded',scan_protected=protected(name),
            delayed=timer['ActiveState']=='active',delay_until=data.get(name,{}).get('delay_until') if timer['ActiveState']=='active' else None))
    try:engine=api('state')
    except Exception:engine=None
    return dict(ok=True,ts=datetime.now().astimezone().isoformat(),services=services,engine=engine)


def engine_process():
    state=unit_state(UNITS['engine']);pid=state['MainPID']
    if not pid.isdigit() or int(pid)<=0:raise ValueError('engine_process_unavailable')
    proc=Path('/proc')/pid
    argv=[os.fsdecode(x) for x in (proc/'cmdline').read_bytes().split(b'\0') if x]
    args=argv[1:]
    if args and args[0]=='-u':args=args[1:]
    owner=pwd.getpwnam('ubuntu')
    if proc.stat().st_uid!=owner.pw_uid or (proc/'cwd').resolve()!=REPO or not argv or not Path(argv[0]).is_absolute() or 'python' not in Path(argv[0]).name or len(args)!=1 or (REPO/args[0]).resolve()!=REPO/'run.py':
        raise ValueError('unexpected_engine_process')
    env=dict(os.fsdecode(x).split('=',1) for x in (proc/'environ').read_bytes().split(b'\0') if b'=' in x)
    return argv[0],env,owner


def preflight(process):
    python,env,owner=process
    if not Path(env.get('KADIMA_DATA_DIR','/var/lib/kadima')).is_absolute():
        raise ValueError('relative_engine_data_path')
    # SmartAPI creates logs relative to cwd. Keep these private and transient;
    # the helper's ProtectHome=read-only can remain in force.
    with tempfile.TemporaryDirectory(prefix='kadima-ops-check-') as work:
        os.chown(work,owner.pw_uid,owner.pw_gid)
        code='import sys\nsys.path.insert(0, '+repr(str(REPO))+')\n'+PREFLIGHT
        result=run([python,'-c',code],timeout=55,cwd=work,env=env,
            user=owner.pw_uid,group=owner.pw_gid,extra_groups=[])
    if 'KADIMA_PREFLIGHT_OK' not in result.stdout:raise ValueError('flat_state_unverified')


def cancel_delay(name):
    subprocess.run(['systemctl','stop','kadima-delay-'+name+'.timer'],capture_output=True,timeout=15)


def release_hold(data):
    token=data.get('entry_hold')
    if token:
        if api('entries',dict(action='release',token=token)).get('ok') is not True:
            raise ValueError('entry_hold_release_failed')
        data.pop('entry_hold',None);save(data)


def act(request):
    if not isinstance(request,dict) or request.get('action') not in ('status','start','stop','restart','delay','pause_entries'):
        return dict(ok=False,reason='invalid_action')
    action=request['action']
    if action=='status':
        if set(request)!={'action'}:return dict(ok=False,reason='invalid_request')
        return status()
    if set(request)-{'action','service','minutes'} or request.get('service') not in UNITS:
        return dict(ok=False,reason='invalid_service')
    name=request['service']
    minutes=request.get('minutes',15)
    if action=='delay' and (type(minutes) is not int or minutes not in (5,15,30,60)):
        return dict(ok=False,reason='invalid_delay')
    if action=='pause_entries' and name!='engine':return dict(ok=False,reason='invalid_service')
    if not ACTION_LOCK.acquire(blocking=False):return dict(ok=False,reason='operation_in_progress')
    try:
        ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
        with (ROOT/'operation.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return dict(ok=False,reason='operation_in_progress')
            # Persistent timers use this same lock and these same validation gates.
            return perform(name,action,minutes)
    finally:ACTION_LOCK.release()


def perform(name,action,minutes):
    data=saved();unit=UNITS[name]
    before=unit_state(unit)
    if before['LoadState']!='loaded':return dict(ok=False,reason='service_not_installed')
    if action=='delay' and protected(name,datetime.fromtimestamp(time.time()+minutes*60,ZoneInfo('Asia/Kolkata'))):
        return dict(ok=False,reason='delay_overlaps_protected_scan_session')
    if action in ('stop','restart','delay') and protected(name):
        return dict(ok=False,reason='gamma_cas_scan_session_protected')
    if action=='pause_entries' or (name=='engine' and action in ('stop','restart','delay') and before['ActiveState']=='active'):
        process=engine_process()
        token=data.get('entry_hold') or secrets.token_hex(16)
        # Persist ownership before a possibly ambiguous HTTP acknowledgement.
        data['entry_hold']=token;save(data)
        try:
            if api('entries',dict(action='hold',token=token)).get('ok') is not True:
                raise ValueError('entry_hold_not_acknowledged')
        except Exception:
            return dict(ok=False,reason='entry_hold_unverified_no_services_changed')
        if action=='pause_entries':return dict(ok=True,reason='new_entries_paused_scans_and_exits_continue')
        try:preflight(process)
        except Exception:return dict(ok=False,reason='positions_or_orders_unresolved_entries_remain_paused')
    if name=='engine' and action in ('stop','restart','delay') and before['ActiveState'] not in ('active','inactive','failed'):
        return dict(ok=False,reason='engine_state_unverified')
    cancel_delay(name)
    data.setdefault(name,{})['delay_until']=None;save(data)
    if action in ('stop','delay'):
        run(['systemctl','stop',unit],timeout=45)
    elif action=='restart':
        run(['systemctl','restart',unit],timeout=45)
    elif action=='start':
        run(['systemctl','start',unit],timeout=45)
    if action=='delay':
        run(['systemd-run','--quiet','--collect','--unit=kadima-delay-'+name,'--on-active='+str(minutes)+'m',
            '--timer-property=AccuracySec=1s','/usr/bin/python3',SCRIPT,'--scheduled-start',name])
        data[name]['delay_until']=datetime.fromtimestamp(time.time()+minutes*60).astimezone().isoformat();save(data)
    if name=='engine' and action in ('start','restart') and data.get('entry_hold'):
        # Restore entry admission only after the new control API answers. Existing
        # memory, freshness and reconciliation gates still decide every entry.
        deadline=time.monotonic()+35
        while True:
            try:release_hold(data);break
            except Exception:
                if time.monotonic()>=deadline:
                    return dict(ok=False,reason='engine_started_entries_still_paused_check_readiness')
                time.sleep(1)
    after=unit_state(unit)
    expected='inactive' if action in ('stop','delay') else 'active'
    if after['ActiveState']!=expected:return dict(ok=False,reason='service_state_not_confirmed')
    return dict(ok=True,reason='delay_scheduled' if action=='delay' else 'service_'+action+'_confirmed')


def serve():
    allowed=pwd.getpwnam('ubuntu').pw_uid
    server=socket.socket(fileno=3)
    slots=threading.BoundedSemaphore(4)
    def handle(conn):
        try:
            conn.settimeout(100)
            pid,uid,gid=struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid not in (0,allowed):return
            raw=b''
            while b'\n' not in raw and len(raw)<=1024:
                chunk=conn.recv(1025-len(raw))
                if not chunk:break
                raw+=chunk
            if len(raw)>1024 or not raw.endswith(b'\n'):result=dict(ok=False,reason='invalid_request')
            else:
                try:
                    request=json.loads(raw)
                    result=act(request)
                    if isinstance(request,dict) and request.get('action')!='status':
                        print(json.dumps(dict(event='operations_action',service=request.get('service') if request.get('service') in UNITS else 'invalid',
                            action=request.get('action') if request.get('action') in ('start','stop','restart','delay','pause_entries') else 'invalid',
                            ok=result.get('ok',False),reason=result.get('reason'))),flush=True)
                except Exception:result=dict(ok=False,reason='operation_failed_check_service_state')
            conn.sendall(json.dumps(result).encode()+b'\n')
        finally:conn.close();slots.release()
    while True:
        conn,_=server.accept()
        if not slots.acquire(blocking=False):conn.close();continue
        threading.Thread(target=handle,args=(conn,),daemon=True).start()


if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Root service only')
    if sys.argv[1:]==['--serve']:
        serve()
    elif len(sys.argv)==3 and sys.argv[1]=='--scheduled-start' and sys.argv[2] in UNITS:
        result=act(dict(action='start',service=sys.argv[2]))
        print(json.dumps(result));raise SystemExit(0 if result.get('ok') else 1)
    else:raise SystemExit('Unsupported operation')
