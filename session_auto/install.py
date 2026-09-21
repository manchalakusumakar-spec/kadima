"""One-time operator installation of working-day activation; never a test order.

Requires the already-installed PR #10 stock-fast feature and paused controls.
Checks the reviewed source, current broker/local exposure and service identity,
then adds the calendar entry guard and a persistent systemd activation timer.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import time

ROOT=Path('/home/ubuntu/kadima-py')
DEST=Path('/opt/kadima-session-auto')
# Nonsecret calendar must be readable by the unprivileged engine.
# Keep it outside the existing private /etc/kadima credential directory.
CONF=Path('/etc/kadima-session-auto')
ENV=Path('/etc/kadima/session-auto.env')
SYSTEMD=Path('/etc/systemd/system')
TIMER='kadima-session-auto.timer'
SERVICE='kadima-session-auto.service'
BASE_EXECUTOR=('f830afebc49b764e48b91bd4c61f2260e769d25f','362923e19b763975d1a6cf8c18a23459585e0259','502744a63d0b5a5d5aa9c74c39e7bd7cf5334c88')
INSTALL_HELPER='00ac2c1ba6b5a68a21cd2bf532dd53e1440e6e35'
MARKER='# KADIMA SESSION AUTO ENTRY GUARD v1'
ENV_CONTENT='KADIMA_CELESTIAL_ENABLED=1\nKADIMA_FAST_MOMENTUM_LIVE=1\nKADIMA_SESSION_AUTOTRADE=1\n'


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def patch_executor(raw):
    """Exact two entry-only insertions; never edits check_exits or risk functions."""
    reviewed=BASE_EXECUTOR if isinstance(BASE_EXECUTOR,(tuple,list)) else (BASE_EXECUTOR,)
    if digest(raw) not in reviewed:
        raise ValueError('reviewed_executor_required')
    text=raw.decode()
    first="        if not getattr(chain, '_entry_guard', lambda: True)():\n"
    second='        pos = Position(\n            signal_id=source_signal_id, kind=kind, index=index_name, direction=direction,\n'
    if text.count(first)!=2 or text.count(second)!=1:
        # There are two existing _entry_guard checks; add before the first only.
        raise ValueError('entry_anchors_changed')
    block=("        "+MARKER+"\n"
           "        from . import market_session_gate\n"
           "        session_block = market_session_gate.entry_block(chain.index.opt_exchange)\n"
           "        if session_block:\n"
           "            return self._reject(session_block, index_name, direction)\n")
    text=text.replace(first,block+first,1)
    text=text.replace(second,block+second,1)
    compile(text,'guarded_auto_executor.py','exec')
    return text.encode()


def load(path,name,expected=None):
    raw=path.read_bytes()
    if expected and digest(raw)!=expected:
        raise ValueError('reviewed_helper_hash_mismatch')
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def service_text():
    return '''[Unit]
Description=KADIMA market-day auto activation and entry permit
After=network-online.target kadima.service kadima-universe-scanner.service
Wants=network-online.target
[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 -I -B -S /opt/kadima-session-auto/runner.py
TimeoutStartSec=150
UMask=0077
RuntimeDirectory=kadima-session-auto
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
StateDirectory=kadima-session-auto
StateDirectoryMode=0700
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/run/kadima-session-auto /var/lib/kadima-session-auto /run/lock
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
'''


def timer_text():
    return '''[Unit]
Description=Check KADIMA market-day activation every minute
[Timer]
OnBootSec=30s
OnUnitInactiveSec=60s
AccuracySec=1s
Unit=kadima-session-auto.service
[Install]
WantedBy=timers.target
'''


def ensure_root_directory(path,mode=0o755,owner=0,*,public=True,create=True):
    """Validate by reader; never chmod an existing directory or its parents.

    Public calendar/permit paths require traversal for the engine. Root-only
    systemd configuration and private state may have private ancestors.
    create=False performs the same existing-path checks without making folders.
    """
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path or path.is_symlink():
        raise ValueError('unsafe_install_directory')
    missing=[]
    cursor=path
    while not cursor.exists():
        missing.append(cursor)
        cursor=cursor.parent
    if not stat.S_ISDIR(cursor.stat().st_mode):
        raise ValueError('install_parent_not_directory')
    for parent in path.parents:
        if not parent.exists():
            continue
        info=parent.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('install_parent_not_directory')
        if public and not info.st_mode & 0o001:
            raise ValueError('private_parent_directory_requires_review')
    def validate_target():
        info=path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('install_target_not_directory')
        if info.st_uid!=owner or info.st_mode & 0o022:
            raise ValueError('unsafe_install_directory_owner')
        required=mode if public else 0o700
        if info.st_mode & required != required:
            raise ValueError('existing_directory_permissions_require_review')
    if path.exists():
        validate_target()
    if not create:
        return
    for directory in reversed(missing):
        # Compensate for umask only on directories this call creates. Private
        # root-only ancestors need not expose sibling credential filenames.
        new_mode=mode if directory==path else (0o755 if public else 0o700)
        directory.mkdir(mode=new_mode)
        directory.chmod(new_mode)
    validate_target()


def prepare_directories(targets,*,create):
    """Policy/permit are public data; environment, runner and state are root-only."""
    for parent in sorted({p.parent for p in targets if not p.is_relative_to(ROOT)}):
        ensure_root_directory(parent,public=parent==CONF,create=create)
    ensure_root_directory(Path('/run/kadima-session-auto'),create=create)
    ensure_root_directory(Path('/var/lib/kadima-session-auto'),0o700,public=False,create=create)


def check_engine_policy(child):
    """Real read as the engine owner before restart; not just a root import."""
    child('from kadima import market_session_gate as gate; '
          'gate.validate_policy(gate.read_json(gate.POLICY))')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0: raise ValueError('sudo_required')
    package=Path(__file__).resolve().parents[1]
    helper=load(package/'stock_fast_update/install.py','session_install_helper',INSTALL_HELPER)
    installer=helper.load_helper(package/'executor_fix/install.py',helper.HELPER)
    gate=load(package/'session_auto/gate.py','new_calendar_gate')
    policy=gate.validate_policy(json.loads((package/'session_auto/policy.json').read_text()))
    release_raw=helper.read_file(package/'kadima/auto_executor.py')
    patched=patch_executor(release_raw)
    # Idempotent setup is status-only, not a stealth restart/rearm after a pause.
    if (ROOT/'kadima/auto_executor.py').read_bytes()==patched and (SYSTEMD/TIMER).is_file():
        for relative,target in (('gate.py',ROOT/'kadima/market_session_gate.py'),('runner.py',DEST/'runner.py'),('gate.py',DEST/'gate.py')):
            if helper.read_file(target)!=(package/'session_auto'/relative).read_bytes():
                raise ValueError('existing_schedule_source_changed')
        if helper.read_file(ENV).decode()!=ENV_CONTENT:
            raise ValueError('existing_schedule_environment_changed')
        print('SCHEDULE ALREADY INSTALLED. Use status; no restart or permission reset performed.')
        return
    user=pwd.getpwnam('ubuntu')
    # Validates the known installed stock release and its static dependencies.
    base_changes,fingerprints=helper.backend_plan(package,ROOT,(user.pw_uid,user.pw_gid))
    if base_changes:
        raise ValueError('finish_stock_fast_installation_first')
    pid,argv,env=helper.process(helper.ENGINE,ROOT,user.pw_uid)
    python=installer.engine_interpreter(Path('/proc')/pid,ROOT)
    spid,sargv,senv=helper.process(helper.SCANNER,ROOT,user.pw_uid)
    if [a for a in sargv[1:] if a not in ('-u','-B')]!=['-m','kadima.stock_universe_scanner']:
        raise ValueError('scanner_entrypoint_changed')
    if env.get('KADIMA_DATA_DIR','/var/lib/kadima')!=senv.get('KADIMA_DATA_DIR','/var/lib/kadima'):
        raise ValueError('engine_scanner_database_differs')
    auth=helper.read_file(helper.TOKEN_FILE)
    token=json.loads(auth).get('api_token')
    if not isinstance(token,str) or not token: raise ValueError('local_auth_unavailable')
    fingerprints[helper.TOKEN_FILE]=auth
    def paused():
        data=helper.local_api('/api/kadima/top20',token)
        from datetime import datetime, timezone
        stamp=datetime.fromisoformat(data['ts'])
        if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 30:
            raise ValueError('fresh_control_snapshot_required')
        cfg=data.get('config') or {}
        if cfg.get('auto_trade_enabled') is not False or cfg.get('kill_latched') is not False or cfg.get('broker_orders_enabled') is not True:
            raise ValueError('paused_broker_enabled_no_kill_required')
        stock=helper.local_api('/api/kadima/stock-fast',token)
        stamp=datetime.fromisoformat(stock['ts'])
        if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 30:
            raise ValueError('fresh_stock_snapshot_required')
        if (stock.get('controls') or {}).get('live_enabled') is not False:
            raise ValueError('stock_live_must_be_paused_for_setup')
        return stock
    paused()
    db=Path(env.get('KADIMA_DATA_DIR','/var/lib/kadima'))/'kadima.db'
    helper.local_flat(db)
    # Source and environment changes only; global and stock intents stay OFF
    # during setup. The installed scheduler owns their future, guarded arming.
    targets={
        ROOT/'kadima/auto_executor.py':patched,
        ROOT/'kadima/market_session_gate.py':(package/'session_auto/gate.py').read_bytes(),
        DEST/'gate.py':(package/'session_auto/gate.py').read_bytes(),
        DEST/'runner.py':(package/'session_auto/runner.py').read_bytes(),
        CONF/'policy.json':(package/'session_auto/policy.json').read_bytes(),
        ENV:ENV_CONTENT.encode(),
        SYSTEMD/SERVICE:service_text().encode(),
        SYSTEMD/TIMER:timer_text().encode(),
    }
    for unit in (helper.ENGINE,helper.SCANNER):
        targets[SYSTEMD/(unit+'.d')/'zz-kadima-session-auto.conf']=('[Service]\nEnvironmentFile='+str(ENV)+'\n').encode()
    changes=[]
    for target,incoming in targets.items():
        old=helper.read_file(target,missing=True)
        if target!=ROOT/'kadima/auto_executor.py' and old is not None and old!=incoming:
            raise ValueError('schedule_target_already_differs:'+target.name)
        if old!=incoming:
            owner=(user.pw_uid,user.pw_gid) if target.is_relative_to(ROOT) else (0,0)
            changes.append(helper.change_for(target,incoming,owner))
    print('PLAN: preserve strategies/risk/FIFO; add entry-only session guard, master permissions and one-minute timer.')
    print('CALENDAR: normal NSE/BSE equity-derivative sessions, through '+policy['valid_through']+'; special sessions excluded.')
    if not args.apply:
        print('PLAN ONLY: no broker login, stop, restart, writes or scheduled activation.'); return
    # Directory failures are reported before another broker login or service stop.
    prepare_directories(targets,create=False)
    child_env=dict(env,PYTHONDONTWRITEBYTECODE='1',ORDER_PLACEMENT_ENABLED='0',KADIMA_FAST_MOMENTUM_LIVE='0',KADIMA_CAS_LIVE='0')
    def child(code):
        return helper.command([python,'-B','-c',code],cwd=ROOT,env=child_env,
            user=user.pw_uid,group=user.pw_gid,extra_groups=[user.pw_gid],timeout=60)
    def flat():
        helper.local_flat(db)
        try:
            result=child(helper.broker_preflight_code(installer.PREFLIGHT))
            if 'KADIMA_PREFLIGHT_OK' not in result.stdout.splitlines():
                raise ValueError('broker_flat_not_confirmed')
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
            raise helper.Blocked(installer.failure_summary(exc,'broker_preflight')) from None
        helper.local_flat(db)
    flat()
    print('BROKER: flat books checked. No place/modify/cancel order calls.')
    paused(); helper.check_fingerprints(fingerprints)
    if helper.process(helper.ENGINE,ROOT,user.pw_uid)[0]!=pid or helper.process(helper.SCANNER,ROOT,user.pw_uid)[0]!=spid:
        raise ValueError('process_changed_during_setup')
    prepare_directories(targets,create=True)
    # Nothing may grant entry before all setup checks complete and the scheduler runs.
    lease=Path('/run/kadima-session-auto/lease.json')
    if lease.exists(): raise ValueError('existing_entry_permit_requires_review')
    backup=Path(tempfile.mkdtemp(prefix='kadima-session-auto-',dir='/var/backups'))
    backup.chmod(0o700)
    for i,item in enumerate(changes):
        item.unchanged()
        if item.original is not None: (backup/str(i)).write_bytes(item.original)
    (backup/'files.json').write_text(json.dumps([dict(path=str(c.path),old=digest(c.original) if c.original else None,
        new=digest(c.replacement),uid=c.uid,gid=c.gid,mode=c.mode) for c in changes],indent=2))
    units=[helper.SCANNER,helper.ENGINE]
    written=[]; started=False
    try:
        helper.command(['systemctl','stop',*units],timeout=60)
        for unit in units:
            info=helper.unit_state(unit)
            if info.get('ActiveState')!='inactive' or info.get('MainPID')!='0':
                raise ValueError('service_not_fully_stopped')
        flat(); helper.check_fingerprints(fingerprints)
        for item in changes:
            item.unchanged(); written.append(item); helper.write_atomic(item,item.replacement)
        child('import kadima.auto_executor; import kadima.market_session_gate; import kadima.stock_fast_api')
        check_engine_policy(child)
        helper.command(['systemctl','daemon-reload'])
        started=True
        helper.command(['systemctl','start',*reversed(units)],timeout=60)
        for attempt in range(20):
            try:
                data=paused()
                if data.get('celestial_rollout') is True and data.get('fast_master_permission') is True:
                    break
            except Exception:
                pass
            time.sleep(1)
        else: raise ValueError('master_permission_postcheck_failed')
        for unit in units:
            info=helper.unit_state(unit)
            if info.get('ActiveState')!='active': raise ValueError('restarted_service_not_active')
            proc=Path('/proc')/info['MainPID']
            penv=dict(x.split(b'=',1) for x in (proc/'environ').read_bytes().split(b'\0') if b'=' in x)
            for flag in (b'KADIMA_CELESTIAL_ENABLED',b'KADIMA_FAST_MOMENTUM_LIVE',b'KADIMA_SESSION_AUTOTRADE'):
                if penv.get(flag)!=b'1': raise ValueError('schedule_environment_overridden')
        helper.command(['systemctl','enable','--now',TIMER])
        helper.command(['systemctl','is-active','--quiet',TIMER])
    except BaseException:
        # Never restore a code version lacking the guard after runtime restart.
        if started:
            print('SETUP INCOMPLETE: guarded source retained; inspect services before recovery.')
        else:
            for item in reversed(written):
                if item.original is None: item.path.unlink(missing_ok=True)
                else: helper.write_atomic(item,item.original)
            helper.command(['systemctl','daemon-reload'])
            helper.command(['systemctl','start',*reversed(units)],timeout=60)
            print('SETUP ABORTED: previous source restored before restart.')
        print('BACKUP:',backup)
        raise
    print('SCHEDULE INSTALLED: automatic global + stock activation on calendar working days.')
    print('RETRIES: every 60 seconds after each check; no daily command or stock button required.')
    print('START: 09:00 IST readiness checks. ENTRIES: 09:15 until existing strategy cutoff (outer ceiling 15:35).')
    print('PAUSE/KILL: respected; an observed intraday pause is not automatically undone that day.')
    print('CALENDAR UPDATE REQUIRED AFTER: '+policy['valid_through'])
    print('BACKUP:',backup)
    print('STATUS: sudo -n python3 -I -B -S /opt/kadima-session-auto/runner.py --status')


if __name__=='__main__':
    try:
        handles=[]
        for name in ('kadima-stock-fast-install','kadima-activation','kadima-session-auto-install'):
            fd=os.open('/run/lock/'+name+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            handle=os.fdopen(fd,'w'); handles.append(handle)
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        main()
    except Exception as exc:
        # Never echo child stdout/stderr or provider errors. Only authored reasons.
        import re
        reason=str(exc) if type(exc) is ValueError and re.fullmatch(r'[a-z_]+(?::[a-zA-Z0-9._-]+)?',str(exc)) else type(exc).__name__
        print('BLOCKED:',reason)
        raise SystemExit(1)
