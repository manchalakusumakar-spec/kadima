#!/usr/bin/env python3
"""User-run AWS activation for the reviewed installed engine; never a test order.

Loads the existing guarded installer's broker preflight from the extracted
release. Checks exposure before and after stopping, then uses the authenticated
resume endpoint. Never edits broker credentials, scores, stops or kill latches.
"""
import argparse
from datetime import datetime, timezone
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


def carryforward_preflight_code(code):
    """Permit only verified long CARRYFORWARD options; all other exposure blocks."""
    contract_anchor="    SESSION.ensure()\n    for name in ('position','orderBook'):\n"
    position_anchor="""                if quantity != 0:
                    reason = 'broker_positions_not_flat'
                    raise RuntimeError()
"""
    if code.count(contract_anchor)!=1 or code.count(position_anchor)!=1:
        raise Blocked('broker_preflight_shape_changed')
    contract_block="""    SESSION.ensure()
    from kadima.scrip_master import ScripMaster
    from kadima.celestial_universe import active_contracts
    from kadima.utils import now_ist
    contracts = active_contracts(ScripMaster.load().rows, now_ist().date())
    for name in ('position','orderBook'):
"""
    position_block="""                if quantity != 0:
                    product = str(row.get('producttype') or '')
                    exchange = str(row.get('exchange') or '')
                    token = str(row.get('symboltoken') or '')
                    symbol = str(row.get('tradingsymbol') or '')
                    contract = contracts.get((exchange, token))
                    carry_ok = (
                        product == 'CARRYFORWARD' and quantity > 0
                        and quantity == int(quantity) and contract is not None
                        and int(quantity) % int(contract['lot_size']) == 0
                        and symbol == str(contract['symbol']))
                    if not carry_ok:
                        reason = 'broker_positions_not_flat'
                        raise RuntimeError()
"""
    result=code.replace(contract_anchor,contract_block,1).replace(position_anchor,position_block,1)
    compile(result,'activation_carryforward_preflight','exec')
    return result


def preflight(installer,context):
    python,env,owner=context
    # Read-only broker preflight permits verified long carry-forward exposure,
    # but still refuses intraday positions, shorts, invalid contracts/lots and
    # pending/unknown broker orders.
    try:
        result=command([python,'-c',carryforward_preflight_code(installer.PREFLIGHT)],cwd=REPO,env=env,
            user=owner.pw_uid,group=owner.pw_gid,extra_groups=[])
    except Exception as exc:
        raise Blocked(installer.failure_summary(exc,'flat_account_preflight')) from None
    if 'KADIMA_PREFLIGHT_OK' not in result.stdout:
        raise Blocked('flat_account_preflight_unconfirmed')


def planned_files(fast,gap,mcx=False,mcx_capital=None,move=False):
    existing=set()
    existing_cap=None
    if ENVFILE.exists():
        if ENVFILE.is_symlink():
            raise Blocked('existing_activation_file_differs:'+ENVFILE.name)
        for raw in ENVFILE.read_text().splitlines():
            line=raw.strip()
            if not line:continue
            if line.startswith('KADIMA_MCX_CAPITAL_INR='):
                try:
                    existing_cap=float(line.split('=',1)[1])
                except ValueError:
                    raise Blocked('existing_activation_file_differs:'+ENVFILE.name) from None
                if not existing_cap>0:raise Blocked('existing_activation_file_differs:'+ENVFILE.name)
                continue
            if line not in {'KADIMA_CELESTIAL_ENABLED=1','KADIMA_FAST_MOMENTUM_LIVE=1',
                            'KADIMA_GAP_LIVE=1','KADIMA_MCX_LIVE=1','KADIMA_MOVE_CAPTURE_LIVE=1'}:
                raise Blocked('existing_activation_file_differs:'+ENVFILE.name)
            existing.add(line)
        if 'KADIMA_CELESTIAL_ENABLED=1' not in existing:
            raise Blocked('existing_activation_file_differs:'+ENVFILE.name)
    if mcx:
        try:
            mcx_capital=float(mcx_capital)
        except (TypeError,ValueError):
            raise Blocked('mcx_capital_required') from None
        if not 0 < mcx_capital <= 100000:
            raise Blocked('mcx_capital_out_of_bounds')
    elif existing_cap is not None:
        mcx_capital=existing_cap
    lines=['KADIMA_CELESTIAL_ENABLED=1']
    if fast or move or 'KADIMA_FAST_MOMENTUM_LIVE=1' in existing:
        lines.append('KADIMA_FAST_MOMENTUM_LIVE=1')
    if move or 'KADIMA_MOVE_CAPTURE_LIVE=1' in existing:
        lines.append('KADIMA_MOVE_CAPTURE_LIVE=1')
    if gap or 'KADIMA_GAP_LIVE=1' in existing:
        lines.append('KADIMA_GAP_LIVE=1')
    if mcx or 'KADIMA_MCX_LIVE=1' in existing:
        lines.append('KADIMA_MCX_LIVE=1')
        lines.append('KADIMA_MCX_CAPITAL_INR='+str(int(mcx_capital) if float(mcx_capital).is_integer() else mcx_capital))
    files={ENVFILE:'\n'.join(lines)+'\n'}
    files.update({SYSTEMD/(u+'.d')/DROPIN:'[Service]\nEnvironmentFile='+str(ENVFILE)+'\n' for u in UNITS})
    for path,content in files.items():
        if path.is_symlink():
            raise Blocked('existing_activation_file_differs:'+path.name)
        if path.exists() and path != ENVFILE and path.read_text()!=content:
            raise Blocked('existing_activation_file_differs:'+path.name)
    return files


def activate_services(installer,context,files):
    preflight(installer,context)
    created=[]
    replaced={}
    restart_attempted=False
    try:
        command(['systemctl','stop',*UNITS])
        # Source and exposure can change between the initial check and shutdown.
        source_check(installer)
        preflight(installer,context)
        for path,content in files.items():
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists():
                if path.read_text()==content:continue
                if path != ENVFILE:raise Blocked('existing_activation_file_differs:'+path.name)
                current=path.read_text()
                current_lines={line.strip() for line in current.splitlines() if line.strip()}
                desired_lines={line.strip() for line in content.splitlines() if line.strip()}
                if (not current_lines or 'KADIMA_CELESTIAL_ENABLED=1' not in current_lines
                        or not current_lines.issubset(desired_lines)):
                    raise Blocked('activation_environment_changed_during_preparation')
                replaced[path]=current
            else:
                created.append(path)
            temporary=path.with_name('.'+path.name+'.gap-live.tmp')
            with temporary.open('w') as out:
                out.write(content);out.flush();os.fsync(out.fileno())
            temporary.chmod(0o644)
            os.replace(temporary,path)
        command(['systemctl','daemon-reload'])
        restart_attempted=True
        command(['systemctl','start',*UNITS])
        for unit in UNITS:command(['systemctl','is-active','--quiet',unit])
    except Exception:
        if not restart_attempted:
            for path,content in replaced.items():path.write_text(content)
            for path in reversed(created):path.unlink(missing_ok=True)
            command(['systemctl','daemon-reload'])
        # After restart, preserve installed mode; do not roll back under exposure.
        try:command(['systemctl','start',*UNITS])
        except Exception:pass
        raise


def check_runtime_flags(fast,gap,mcx=False,mcx_capital=None,move=False):
    for unit in UNITS:
        pid=command(['systemctl','show',unit,'-p','MainPID','--value']).stdout.strip()
        if not pid.isdigit() or int(pid)<=0:raise Blocked('restarted_service_unavailable')
        env=dict(x.split(b'=',1) for x in (Path('/proc')/pid/'environ').read_bytes().split(b'\0') if b'=' in x)
        if env.get(b'KADIMA_CELESTIAL_ENABLED')!=b'1':raise Blocked('rollout_environment_overridden')
        if fast and unit==UNITS[0] and env.get(b'KADIMA_FAST_MOMENTUM_LIVE')!=b'1':
            raise Blocked('fast_environment_overridden')
        if move and unit==UNITS[0] and (env.get(b'KADIMA_MOVE_CAPTURE_LIVE')!=b'1'
                or env.get(b'KADIMA_FAST_MOMENTUM_LIVE')!=b'1'):
            raise Blocked('move_capture_environment_overridden')
        if gap and unit==UNITS[0] and env.get(b'KADIMA_GAP_LIVE')!=b'1':
            raise Blocked('gap_environment_overridden')
        if mcx and unit==UNITS[0]:
            if env.get(b'KADIMA_MCX_LIVE')!=b'1':
                raise Blocked('mcx_environment_overridden')
            try:
                observed=float(env.get(b'KADIMA_MCX_CAPITAL_INR',b'0'))
            except ValueError:
                observed=0
            if observed != float(mcx_capital):
                raise Blocked('mcx_capital_environment_overridden')


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


def arm_stock_intent(token, reporter):
    state = reporter.local_json('http://127.0.0.1:8081/api/kadima/stock-fast', token)
    for key in ('move_capture_feed', 'move_capture_worker'):
        health = state.get(key) or {}
        try:
            stamp = datetime.fromisoformat(health['ts'])
            age = (datetime.now(timezone.utc)-stamp).total_seconds()
            ready = health.get('ok') is True and stamp.tzinfo is not None and 0 <= age <= 15
        except (KeyError, ValueError, TypeError):
            ready = False
        if not ready:
            raise Blocked(key+'_not_ready_auto_remains_paused')
    controls = state.get('controls') or {}
    if controls.get('live_enabled') is True:
        return
    revision = controls.get('revision')
    if type(revision) is not int:
        raise Blocked('stock_control_revision_unknown')
    req = urllib.request.Request('http://127.0.0.1:8081/api/kadima/stock-fast/config',
        data=json.dumps({'patch': {'live_enabled': True}, 'expected_revision': revision}).encode(),
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), reporter.NoRedirect())
    with opener.open(req, timeout=8) as response:
        result = json.loads(response.read(10000))
    if result.get('live_enabled') is not True:
        raise Blocked('stock_live_intent_unconfirmed')


def run_activation(fast,gap,mcx=False,mcx_capital=None,move=False):
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
            check_runtime_flags(fast,gap,mcx,mcx_capital,move)
            already_loaded=True
        except Blocked:pass
    if already_loaded:
        # Retrying a news warm-up failure must not restart the collector again.
        preflight(installer,context)
    else:
        files=planned_files(fast,gap,mcx,mcx_capital,move)
        activate_services(installer,context,files)
        check_runtime_flags(fast,gap,mcx,mcx_capital,move)
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
    if move:
        # Intent is set while global auto remains paused. Resume still enforces
        # fresh broker/news health. The worker rechecks all controls per entry.
        arm_stock_intent(token,reporter)
    resume(token,reporter)
    result=reporter.report(REPO)
    if result.get('checks',{}).get('auto_trade_enabled') is not True:
        raise Blocked('post_resume_auto_status_not_confirmed')
    snapshot=reporter.local_json(url+'top20',token)
    news=(snapshot.get('system') or {}).get('news') or {}
    covered=news.get('covered_symbols') or []
    result['index_news_coverage_missing']=[name for name in INDICES if name not in covered]
    result['activation']='AUTO_ENABLED_SUBJECT_TO_EXISTING_ENTRY_GATES'
    if move:
        state=reporter.local_json(url+'stock-fast',token)
        result['move_capture']={key:state.get(key) for key in
            ('controls','move_capture_feed','move_capture_worker')}
        result['move_capture_note']='Live requested; feed warmup, option confirmation and existing risk gates still apply.'
    result['warning']='Index entries remain blocked wherever index news coverage is missing. CAS is not installed by this helper.'
    print(json.dumps(result,indent=2))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--enable-fast',action='store_true')
    parser.add_argument('--enable-gap',action='store_true')
    parser.add_argument('--enable-mcx',action='store_true')
    parser.add_argument('--enable-move-capture',action='store_true')
    parser.add_argument('--mcx-capital',type=float)
    args=parser.parse_args()
    if not args.apply:raise SystemExit('Use --apply to change service rollout flags and request guarded auto resume.')
    if os.geteuid()!=0:raise SystemExit('Run with sudo -n python3.')
    with open('/run/lock/kadima-activation.lock','w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another activation is running.')
        try:run_activation(args.enable_fast,args.enable_gap,args.enable_mcx,args.mcx_capital,args.enable_move_capture)
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
