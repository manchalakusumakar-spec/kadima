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
spec = importlib.util.spec_from_file_location('priority_sessions', HERE/'priority_sessions.py')
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)
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


def common_ready(gd, sd, now):
    if not fresh(gd.get('ts'), now, 30) or not fresh(sd.get('ts'), now, 30):
        return 'control_snapshot_not_fresh'
    cfg = gd.get('config') or {}
    if cfg.get('kill_latched') is not False or sd.get('kill_latched') is not False:
        return 'kill_latched_or_unknown'
    if any(sd.get(k) is not True for k in ('celestial_rollout', 'broker_order_permission')):
        return 'master_permission_not_enabled'
    for key, ttl in (('reconciliation', 120), ('news', 300)):
        item = (gd.get('system') or {}).get(key) or {}
        if item.get('ok') is not True or not fresh(item.get('ts'), now, ttl):
            return key+'_not_ready'
    return None


def lane_readiness(gd, sd, policy, now):
    common = common_ready(gd, sd, now)
    reasons = dict(
        equity=gate.calendar_reason(policy, now, entries=True) or
               sessions.calendar_reason('NFO', now) or common or ready(gd, sd, now),
        mcx=sessions.calendar_reason('MCX', now) or common)
    if reasons['mcx'] is None:
        worker = (gd.get('system') or {}).get('mcx_live') or {}
        if (worker.get('ok') is not True or worker.get('live_requested') is not True
                or not fresh(worker.get('ts'), now, 45)
                or type(worker.get('fresh_futures')) is not int or worker['fresh_futures'] < 1
                or type(worker.get('fresh_options')) is not int or worker['fresh_options'] < 1):
            reasons['mcx'] = 'mcx_source_quotes_not_ready'
    return reasons


def observed(cfg, scfg):
    return dict(global_revision=cfg['revision'], global_enabled=cfg['auto_trade_enabled'],
                stock_revision=scfg['revision'], stock_enabled=scfg['live_enabled'])


def reconcile_controls(state, current, save):
    pending = state.get('pending')
    if pending:
        if current == pending['after']:
            state['expected'] = current
            state.pop('pending')
            save(state)
        elif current == pending['before']:
            # A request may still finish after a timeout: never repeat it or arm.
            return 'control_write_confirmation_pending'
        else:
            state['phase'] = 'OPERATOR_PAUSED'; save(state)
            return 'operator_control_changed_during_arming'
    if current != state['expected']:
        state['phase'] = 'OPERATOR_PAUSED'; save(state)
        return 'operator_control_changed'
    return None


def write_control(client, state, target, enabled, save):
    before = dict(state['expected'])
    after = dict(before)
    after[target+'_revision'] += 1
    after[target+'_enabled'] = enabled
    state['pending'] = dict(before=before, after=after)
    save(state)  # Durable before the authenticated API call.
    if target == 'global':
        client.call('resume' if enabled else 'pause')
    else:
        client.call('stock_config', dict(patch=dict(live_enabled=enabled),
                                        expected_revision=before['stock_revision']))
    gd, sd, cfg, scfg = observed_flags(client)
    reason = reconcile_controls(state, observed(cfg, scfg), save)
    if reason:
        raise Blocked(reason)
    return gd, sd, cfg, scfg


def iteration(client, policy, state, now, save):
    revoke('priority_readiness_check')
    gd, sd, cfg, scfg = observed_flags(client)
    current = observed(cfg, scfg)
    day = now.astimezone(gate.IST).date().isoformat()
    if state.get('day') != day:
        state.clear()
        state.update(day=day, phase='ACTIVE', expected=current)
        save(state)
    if state.get('phase') == 'OPERATOR_PAUSED':
        return dict(equity='operator_pause_today', mcx='operator_pause_today')
    if state.get('phase') != 'ACTIVE' or not isinstance(state.get('expected'), dict):
        raise Blocked('priority_scheduler_state_invalid')
    reason = reconcile_controls(state, current, save)
    if reason:
        return dict(equity=reason, mcx=reason)
    at = datetime.now(gate.IST)
    reasons = lane_readiness(gd, sd, policy, at)
    equity_window = (gate.calendar_reason(policy, at, entries=True) is None and
                     sessions.calendar_reason('NFO', at) is None)
    mcx_window = sessions.calendar_reason('MCX', at) is None
    # Calendar changes can disable intents. Temporary health failures revoke
    # permits without changing operator revisions or interrupting owned exits.
    want_global = (equity_window or mcx_window) and (cfg['auto_trade_enabled'] or any(v is None for v in reasons.values()))
    want_stock = equity_window and (scfg['live_enabled'] or reasons['equity'] is None)
    for target, desired in (('global', want_global), ('stock', want_stock)):
        if state['expected'][target+'_enabled'] != desired:
            gd, sd, cfg, scfg = write_control(client, state, target, desired, save)
    # Re-evaluate source timestamps and cutoffs after every network call.
    at = datetime.now(gate.IST)
    reasons = lane_readiness(gd, sd, policy, at)
    lanes = {lane: reason is None and cfg['auto_trade_enabled'] and
             (lane != 'equity' or scfg['live_enabled']) for lane, reason in reasons.items()}
    mono = time.monotonic()
    atomic(gate.LEASE, dict(allowed=any(lanes.values()), lanes=lanes,
        day=at.date().isoformat(), boot_id=gate.BOOT.read_text().strip(),
        issued_at=at.isoformat(), issued_mono=mono, expires_mono=mono+gate.MAX_LEASE_SECONDS), 0o644)
    return {lane: 'armed_subject_to_entry_gates' if lanes[lane] else reasons[lane] or 'intent_paused'
            for lane in lanes}


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run through the installed system service.')
    if '--disable' in sys.argv:
        DISABLED.touch(mode=0o600, exist_ok=True)
        revoke('schedule_disabled')
        try: pause_requested(Client())
        except Blocked: pass
        return
    if '--status' in sys.argv:
        print(json.dumps(gate.read_json(STATUS), indent=2)); return
    # Existing explicit operator operations retain their meaning.
    if '--enable' in sys.argv:
        DISABLED.unlink(missing_ok=True); STATE.unlink(missing_ok=True)
    if '--resume-today' in sys.argv:
        STATE.unlink(missing_ok=True)
    try:
        if DISABLED.exists():
            revoke('schedule_disabled')
            result = dict(equity='schedule_disabled', mcx='schedule_disabled')
        else:
            policy = gate.validate_policy(gate.read_json(gate.POLICY))
            try: state = gate.read_json(STATE)
            except FileNotFoundError: state = {}
            result = iteration(Client(), policy, state, datetime.now(gate.IST), lambda v: atomic(STATE, v))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Blocked) else 'schedule_check_unavailable'
        revoke(reason)
        result = dict(equity=reason, mcx=reason)
    report = dict(observed_at=datetime.now(gate.IST).isoformat(), scheduled=True,
                  status='priority_lane_schedule', lanes=result, calendar_through='2026-12-31',
                  orders_submitted_by_scheduler=0)
    atomic(STATUS, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    with open('/run/lock/kadima-session-auto-run.lock', 'w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Another session check is running.')
        main()

