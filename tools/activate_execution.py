#!/usr/bin/env python3
"""User-run AWS activation for the reviewed installed engine; never a test order.

Loads the existing guarded installer's broker preflight from the extracted
release. Checks exposure before and after stopping, then uses the authenticated
resume endpoint. Never edits broker credentials, scores, stops or kill latches.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
import urllib.error
import urllib.request

PACKAGE=Path(__file__).resolve().parents[1]
REPO=Path('/home/ubuntu/kadima-py')
UNITS=('kadima.service','kadima-universe-scanner.service')
SYSTEMD=Path('/etc/systemd/system')
ENVFILE=Path('/etc/kadima/auto-execution.env')
DROPIN='99-kadima-auto-execution.conf'
INDICES=('NIFTY','BANKNIFTY','MIDCPNIFTY','SENSEX')


class Blocked(Exception):
    pass


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,PACKAGE/path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def command(args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=60,**kwargs)


def check_config(cfg):
    if not isinstance(cfg,dict):raise Blocked('invalid_control_config')
    if cfg.get('kill_latched') is not False:raise Blocked('kill_latched_or_unknown')
    if cfg.get('broker_orders_enabled') is not True:raise Blocked('broker_orders_disabled_or_unknown')
    if cfg.get('auto_trade_enabled') is not False:raise Blocked('auto_not_paused_no_restart_performed')
    if type(cfg.get('score_threshold')) is not int or cfg['score_threshold'] != 65:
        raise Blocked('threshold_changed_no_override_performed')


def source_check(installer):
    expected=json.loads((PACKAGE/'tools/activation_source.json').read_text())
    for name,sha in expected.items():
        p=REPO/name
        if not p.is_file() or installer.blob(p.read_bytes()) != sha:
            raise Blocked('source_mismatch:'+name)


def process_context(installer):
    pid=command(['systemctl','show','kadima.service','-p','MainPID','--value']).stdout.strip()
    if not pid.isdigit() or int(pid)<=0:raise Blocked('engine_not_running')
    process=Path('/proc')/pid
    try:python=installer.engine_interpreter(process,REPO)
    except SystemExit:raise Blocked('engine_process_mismatch') from None
    owner=pwd.getpwuid(process.stat().st_uid)
    if owner.pw_name!='ubuntu':raise Blocked('unexpected_engine_owner')
    env=dict(os.fsdecode(x).split('=',1) for x in
             (process/'environ').read_bytes().split(b'\0') if b'=' in x)
    scanner_pid=command(['systemctl','show',UNITS[1],'-p','MainPID','--value']).stdout.strip()
    if not scanner_pid.isdigit() or int(scanner_pid)<=0:raise Blocked('scanner_not_running')
    scanner=Path('/proc')/scanner_pid
    if scanner.stat().st_uid!=owner.pw_uid or (scanner/'cwd').resolve()!=REPO.resolve():
        raise Blocked('scanner_process_mismatch')
    argv=[os.fsdecode(x) for x in (scanner/'cmdline').read_bytes().split(b'\0') if x]
    args=argv[1:]
    if args and args[0]=='-u':args=args[1:]
    if not argv or 'python' not in Path(argv[0]).name or args!=['-m','kadima.stock_universe_scanner']:
        raise Blocked('scanner_entrypoint_mismatch')
    scanner_env=dict(os.fsdecode(x).split('=',1) for x in
                     (scanner/'environ').read_bytes().split(b'\0') if b'=' in x)
    if scanner_env.get('KADIMA_DATA_DIR','/var/lib/kadima') != env.get('KADIMA_DATA_DIR','/var/lib/kadima'):
        raise Blocked('scanner_engine_data_directory_mismatch')
    return python,env,owner


def preflight(installer,context):
    python,env,owner=context
    # The reviewed preflight reads broker positions/orders and local state.
    # SDK stdout/stderr remain captured; only its success marker is accepted.
    try:
        result=command([python,'-c',installer.PREFLIGHT],cwd=REPO,env=env,
            user=owner.pw_uid,group=owner.pw_gid,extra_groups=[])
    except Exception as exc:
        raise Blocked(installer.failure_summary(exc,'flat_account_preflight')) from None
    if 'KADIMA_PREFLIGHT_OK' not in result.stdout:
        raise Blocked('flat_account_preflight_unconfirmed')


def planned_files(fast):
    env='KADIMA_CELESTIAL_ENABLED=1\n'
    if fast:env+='KADIMA_FAST_MOMENTUM_LIVE=1\n'
    files={ENVFILE:env}
    files.update({SYSTEMD/(u+'.d')/DROPIN:'[Service]\nEnvironmentFile='+str(ENVFILE)+'\n' for u in UNITS})
    for path,content in files.items():
        if path.is_symlink() or path.exists() and path.read_text()!=content:
            raise Blocked('existing_activation_file_differs:'+path.name)
    return files


def activate_services(installer,context,files):
    preflight(installer,context)
    created=[]
    restart_attempted=False
    try:
        command(['systemctl','stop',*UNITS])
        # Source and exposure can change between the initial check and shutdown.
        source_check(installer)
        preflight(installer,context)
        for path,content in files.items():
            if path.exists():continue
            path.parent.mkdir(parents=True,exist_ok=True)
            created.append(path)
            with path.open('x') as out:out.write(content)
            path.chmod(0o644)
        command(['systemctl','daemon-reload'])
        restart_attempted=True
        command(['systemctl','start',*UNITS])
        for unit in UNITS:command(['systemctl','is-active','--quiet',unit])
    except Exception:
        if not restart_attempted:
            for path in reversed(created):path.unlink(missing_ok=True)
            command(['systemctl','daemon-reload'])
        # After restart, preserve installed mode; do not roll back under exposure.
        try:command(['systemctl','start',*UNITS])
        except Exception:pass
        raise


def check_runtime_flags(fast):
    for unit in UNITS:
        pid=command(['systemctl','show',unit,'-p','MainPID','--value']).stdout.strip()
        if not pid.isdigit() or int(pid)<=0:raise Blocked('restarted_service_unavailable')
        env=dict(x.split(b'=',1) for x in (Path('/proc')/pid/'environ').read_bytes().split(b'\0') if b'=' in x)
        if env.get(b'KADIMA_CELESTIAL_ENABLED')!=b'1':raise Blocked('rollout_environment_overridden')
        if fast and unit==UNITS[0] and env.get(b'KADIMA_FAST_MOMENTUM_LIVE')!=b'1':
            raise Blocked('fast_environment_overridden')


def resume(token,reporter):
    req=urllib.request.Request('http://127.0.0.1:8081/api/kadima/resume',
        data=b'{}',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),reporter.NoRedirect())
    try:
        with opener.open(req,timeout=45) as response:cfg=json.loads(response.read(10000))
    except urllib.error.HTTPError as exc:
        try:detail=json.loads(exc.read(10000)).get('detail')
        except Exception:detail=None
        permitted={'broker_reconciliation_required','fresh_news_coverage_required',
                   'celestial_rollout_not_enabled','executor_not_registered',
                   'Kill is latched; flat-account reset required'}
        raise Blocked(detail if isinstance(detail,str) and detail in permitted else 'resume_rejected_http_'+str(exc.code)) from None
    if not isinstance(cfg,dict) or cfg.get('auto_trade_enabled') is not True:
        raise Blocked('resume_not_confirmed')


def run_activation(fast):
    installer=load_module('activation_installer','executor_fix/install.py')
    reporter=load_module('activation_reporter','tools/deployment_report.py')
    source_check(installer)
    settings=json.loads(Path('/etc/kadima-dashboard/config.json').read_text())
    token=settings.get('api_token')
    if not isinstance(token,str) or not token:raise Blocked('dashboard_token_unavailable')
    url='http://127.0.0.1:8081/api/kadima/'
    cfg=reporter.local_json(url+'config',token)
    check_config(cfg)
    context=process_context(installer)
    already_loaded=False
    if cfg.get('rollout_enabled') is True:
        try:
            check_runtime_flags(fast)
            already_loaded=True
        except Blocked:pass
    if already_loaded:
        # Retrying a news warm-up failure must not restart the collector again.
        preflight(installer,context)
    else:
        files=planned_files(fast)
        activate_services(installer,context,files)
        check_runtime_flags(fast)
    # Startup/news collection may take several requests. Never invent success.
    deadline=time.monotonic()+60
    while True:
        try:
            snapshot=reporter.local_json(url+'top20',token)
            checks=reporter.api_checks(snapshot)
            if checks.get('rollout_enabled') is not True:raise Blocked('rollout_not_loaded')
            check_config(snapshot['config'])
            if (checks.get('broker_reconciliation_ok') is True and checks.get('reconciliation_fresh') is True
                    and checks.get('news_ok') is True and checks.get('news_fresh') is True):break
        except (urllib.error.URLError,TimeoutError):pass
        if time.monotonic()>=deadline:raise Blocked('startup_health_not_ready_auto_remains_paused')
        time.sleep(2)
    resume(token,reporter)
    result=reporter.report(REPO)
    if result.get('checks',{}).get('auto_trade_enabled') is not True:
        raise Blocked('post_resume_auto_status_not_confirmed')
    snapshot=reporter.local_json(url+'top20',token)
    news=(snapshot.get('system') or {}).get('news') or {}
    covered=news.get('covered_symbols') or []
    result['index_news_coverage_missing']=[name for name in INDICES if name not in covered]
    result['activation']='AUTO_ENABLED_SUBJECT_TO_EXISTING_ENTRY_GATES'
    result['warning']='Index entries remain blocked wherever index news coverage is missing. CAS is not installed by this helper.'
    print(json.dumps(result,indent=2))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--enable-fast',action='store_true')
    args=parser.parse_args()
    if not args.apply:raise SystemExit('Use --apply to change service rollout flags and request guarded auto resume.')
    if os.geteuid()!=0:raise SystemExit('Run with sudo -n python3.')
    with open('/run/lock/kadima-activation.lock','w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another activation is running.')
        try:run_activation(args.enable_fast)
        except Exception as exc:
            detail=str(exc) if isinstance(exc,Blocked) else type(exc).__name__
            print(json.dumps({'activation':'BLOCKED_OR_UNCONFIRMED','reason':detail,
                'note':'No test order submitted. Review live status; do not clear risk or reconciliation gates.'}))
            try:
                reporter=load_module('failure_reporter','tools/deployment_report.py')
                print(json.dumps(reporter.report(REPO),indent=2))
            except Exception:pass
            raise SystemExit(1)


if __name__=='__main__':main()
