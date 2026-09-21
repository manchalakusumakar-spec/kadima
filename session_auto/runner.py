"""One bounded iteration of market-day arming. No direct order API calls.

Installed as a root-owned systemd oneshot. Repeats once per minute. Uses only
fixed loopback controls; imports no live trading package. Never resets a kill.
"""
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('session_gate', HERE/'gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
STATE = Path('/var/lib/kadima-session-auto/state.json')
DISABLED = Path('/var/lib/kadima-session-auto/disabled')
STATUS = Path('/var/lib/kadima-session-auto/status.json')
URLS = {
    'global': '/api/kadima/top20', 'stock': '/api/kadima/stock-fast',
    'resume': '/api/kadima/resume', 'pause': '/api/kadima/pause',
    'stock_config': '/api/kadima/stock-fast/config',
}


class Blocked(Exception):
    pass


def atomic(path, value, mode=0o600):
    """All parents are root-owned directories created by the installer/systemd."""
    path = Path(path)
    if path.is_symlink() or path.resolve() != path:
        raise Blocked('unsafe_state_path')
    fd, tmp = tempfile.mkstemp(prefix='.session-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, allow_nan=False)
            output.flush(); os.fsync(output.fileno()); os.fchmod(output.fileno(), mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def revoke(reason):
    atomic(gate.LEASE, {'allowed':False, 'reason':reason}, 0o644)


def fresh(value, now, seconds):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.tzinfo is not None and 0 <= (now-stamp).total_seconds() <= seconds
    except (ValueError,TypeError):
        return False


def ready(global_data, stock, now):
    if not fresh(global_data.get('ts'),now,30) or not fresh(stock.get('ts'),now,30):
        return 'control_snapshot_not_fresh'
    cfg = global_data.get('config') or {}
    required = ('celestial_rollout','fast_master_permission','broker_order_permission')
    if any(stock.get(k) is not True for k in required):
        return 'master_permission_not_enabled'
    if cfg.get('kill_latched') is not False or stock.get('kill_latched') is not False:
        return 'kill_latched_or_unknown'
    if stock.get('worker_alive') is not True:
        return 'worker_not_started_yet'
    health = stock.get('worker') or {}
    if health.get('ok') is not True or not fresh(health.get('ts'),now,120):
        return 'stock_worker_health_not_ready'
    for key, ttl in (('reconciliation',120),('news',300)):
        record = (global_data.get('system') or {}).get(key) or {}
        if record.get('ok') is not True or not fresh(record.get('ts'),now,ttl):
            return key+'_not_ready'
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        settings = gate.read_json(Path('/etc/kadima-dashboard/config.json'))
        token = settings.get('api_token')
        if not isinstance(token,str) or not 0 < len(token) < 8192:
            raise Blocked('local_auth_unavailable')
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def call(self, action, payload=None):
        if action not in URLS:
            raise Blocked('unsupported_control_action')
        write = action in ('resume','pause','stock_config')
        request = urllib.request.Request('http://127.0.0.1:8081'+URLS[action],
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},
            data=json.dumps(payload or {}).encode() if write else None,
            method='POST' if write else 'GET')
        try:
            with self.opener.open(request,timeout=40 if action=='resume' else 8) as response:
                raw = response.read(2_000_001)
            if len(raw)>2_000_000: raise Blocked('control_response_too_large')
            value = json.loads(raw)
            if not isinstance(value,dict): raise Blocked('invalid_control_response')
            return value
        except urllib.error.HTTPError as exc:
            # No broker bodies, arbitrary provider text or auth material is printed.
            raise Blocked(action+'_http_'+str(exc.code)) from None
        except (OSError,ValueError):
            raise Blocked(action+'_temporarily_unavailable') from None


def controls(global_data, stock):
    cfg, scfg = global_data.get('config'), stock.get('controls')
    if not isinstance(cfg,dict) or not isinstance(scfg,dict):
        raise Blocked('invalid_control_schema')
    if any(type(d.get('revision')) is not int or d['revision'] < 0 for d in (cfg,scfg)):
        raise Blocked('invalid_control_revision')
    if type(cfg.get('auto_trade_enabled')) is not bool or type(scfg.get('live_enabled')) is not bool:
        raise Blocked('invalid_control_flag')
    return cfg,scfg


def observed_flags(client):
    gd, sd = client.call('global'),client.call('stock')
    cfg, scfg = controls(gd,sd)
    return gd,sd,cfg,scfg


def pause_requested(client):
    gd,sd,cfg,scfg = observed_flags(client)
    if cfg['auto_trade_enabled']:
        client.call('pause')
    if scfg['live_enabled']:
        client.call('stock_config', {'patch':{'live_enabled':False},'expected_revision':scfg['revision']})


def iteration(client, policy, state, now, save):
    """Injected I/O makes restart, manual-pause and ambiguous response cases testable."""
    reason = gate.calendar_reason(policy,now)
    if reason:
        revoke(reason)
        # Disable intent as well, but never depend on these calls for holiday safety.
        # The executor's calendar check denies entry even if the API is unavailable.
        try: pause_requested(client)
        except Blocked: pass
        return reason
    gd,sd,cfg,scfg = observed_flags(client)
    # Responses are timestamped during the GETs, after the iteration began.
    # Compare freshness against receipt time, not the earlier request time.
    now = datetime.now(gate.IST)
    reason = gate.calendar_reason(policy, now)
    if reason:
        revoke(reason)
        return reason
    day = now.astimezone(gate.IST).date().isoformat()
    if state.get('day') != day:
        state.clear()
        state.update(day=day,phase='WAITING',g0=cfg['revision'],s0=scfg['revision'])
        save(state)
    if cfg.get('kill_latched') is not False or sd.get('kill_latched') is not False:
        revoke('kill_latched_or_unknown')
        return 'kill_latched_or_unknown'
    if state['phase']=='OPERATOR_PAUSED':
        revoke('operator_pause_today')
        return 'operator_pause_today'
    if state['phase']=='ARMED':
        # A user/risk process pause is binding for the rest of this session.
        if not cfg['auto_trade_enabled'] or not scfg['live_enabled']:
            state['phase']='OPERATOR_PAUSED'; save(state)
            revoke('operator_pause_today')
            return 'operator_pause_today'
        reason=ready(gd,sd,now)
        if reason:
            revoke(reason)
            return reason
    else:
        revoke('waiting_for_session_readiness')
        if state['phase']=='WAITING':
            if (cfg['revision'],scfg['revision']) != (state['g0'],state['s0']):
                state['phase']='OPERATOR_PAUSED'; save(state)
                return 'operator_control_changed_while_waiting'
            reason=ready(gd,sd,now)
            if reason: return reason
            # Persist expected post-write revisions BEFORE any write, so a lost
            # response can be reconciled instead of blindly repeating arming.
            state.update(phase='ARMING',g1=cfg['revision']+1,s1=scfg['revision']+1)
            save(state)
        if state['phase']=='ARMING':
            reason=ready(gd,sd,now)
            if reason: return reason
            g,s=cfg['revision'],scfg['revision']
            expected=((state['g0'],state['s0']),(state['g1'],state['s0']),(state['g1'],state['s1']))
            if (g,s) not in expected:
                state['phase']='OPERATOR_PAUSED'; save(state)
                return 'operator_control_changed_during_arming'
            if g==state['g0']:
                result=client.call('resume')  # Existing guarded reconciliation + news path.
                if result.get('auto_trade_enabled') is not True or result.get('revision') != state['g1']:
                    state['phase']='OPERATOR_PAUSED'; save(state)
                    return 'resume_revision_or_confirmation_changed'
            elif not cfg['auto_trade_enabled']:
                state['phase']='OPERATOR_PAUSED'; save(state)
                return 'operator_pause_during_arming'
            if s==state['s0']:
                result=client.call('stock_config', {'patch':{'live_enabled':True},'expected_revision':s})
                if result.get('live_enabled') is not True or result.get('revision') != state['s1']:
                    raise Blocked('stock_arming_not_confirmed')
            elif not scfg['live_enabled']:
                state['phase']='OPERATOR_PAUSED'; save(state)
                return 'operator_stock_pause_during_arming'
            gd,sd,cfg,scfg=observed_flags(client)
            if (cfg['revision'],scfg['revision']) != (state['g1'],state['s1']) or not cfg['auto_trade_enabled'] or not scfg['live_enabled']:
                state['phase']='OPERATOR_PAUSED'; save(state)
                return 'post_arming_controls_changed'
            reason=ready(gd,sd,datetime.now(gate.IST))
            if reason: return reason
            state['phase']='ARMED'; save(state)
    # Re-evaluate the clock AFTER network calls; no lease can cross the cutoff.
    current=datetime.now(gate.IST)
    reason=gate.calendar_reason(policy,current)
    if reason: revoke(reason); return reason
    mono=time.monotonic()
    atomic(gate.LEASE, dict(allowed=True,day=current.date().isoformat(),boot_id=gate.BOOT.read_text().strip(),
        issued_at=current.isoformat(),issued_mono=mono,expires_mono=mono+gate.MAX_LEASE_SECONDS), 0o644)
    return 'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES'


def main():
    if os.geteuid()!=0: raise SystemExit('Run through the installed system service.')
    if '--disable' in sys.argv:
        DISABLED.touch(mode=0o600,exist_ok=True); revoke('schedule_disabled')
        try: pause_requested(Client())
        except Blocked: pass
        print('SCHEDULE DISABLED: new entries blocked; exits remain managed.'); return
    if '--enable' in sys.argv:
        DISABLED.unlink(missing_ok=True)
        STATE.unlink(missing_ok=True)
    if '--status' in sys.argv:
        try: print(json.dumps(gate.read_json(STATUS),indent=2))
        except (OSError,ValueError): print('Status not yet published.')
        return
    if '--resume-today' in sys.argv:
        STATE.unlink(missing_ok=True)
    now=datetime.now(gate.IST)
    try:
        if DISABLED.exists():
            revoke('schedule_disabled'); reason='schedule_disabled'
        else:
            policy=gate.validate_policy(gate.read_json(gate.POLICY))
            try: state=gate.read_json(STATE)
            except FileNotFoundError: state={}
            if state and (state.get('phase') not in ('WAITING','ARMING','ARMED','OPERATOR_PAUSED') or not isinstance(state.get('day'),str)):
                raise Blocked('invalid_schedule_state')
            reason=iteration(Client(),policy,state,now,lambda value:atomic(STATE,value))
    except Exception as exc:
        reason=str(exc) if isinstance(exc,Blocked) else 'schedule_check_unavailable'
        revoke(reason)
    atomic(STATUS,dict(observed_at=datetime.now(gate.IST).isoformat(),status=reason,
        scheduled=True,calendar_through='2026-12-31',orders_submitted_by_scheduler=0))
    print('SESSION AUTO:',reason)


if __name__=='__main__':
    with open('/run/lock/kadima-session-auto-run.lock','w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Another session check is running.')
        main()
