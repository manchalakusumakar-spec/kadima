"""Passive evidence for a refused verified-flat kill reset.

GETs only. No reset/resume, broker SDK, live-package imports or service changes.
Reads cached reconciliation, a read-only local ledger and bounded audit tails.
This report is not a fresh broker-book check or permission to clear a kill.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
import urllib.request

ROOT = Path('/home/ubuntu/kadima-py')
AUTH = Path('/etc/kadima-dashboard/config.json')
IST = timezone(timedelta(hours=5, minutes=30))
ENDPOINTS = ('top20', 'stock-fast')
REASONS = {
    'checking_broker_books', 'contract_master_unavailable',
    'invalid_broker_position', 'reconciled', 'adoption_verified',
    'managed_quantity_changed_externally',
    'adoption_disabled_or_external_order_pending',
    'unsupported_position_type_or_contract', 'contract_or_lot_mismatch',
    'entry_basis_unavailable', 'managed_position_missing_at_broker',
    'external_pending_orders', 'external_fill_history_unresolved',
}
EVENTS = {
    'login_failed', 'login_ok', 'login_retry_deferred', 'logout_ok', 'logout_failed',
    'broker_position_fetch_rate_limited', 'broker_position_fetch_failed',
    'startup_reconcile_broker_unreadable', 'order_book_unavailable',
    'execution_order_unresolved', 'execution_fill_inconsistent',
    'reconcile_external_pending_orders', 'reconcile_position_quantity_mismatch',
    'startup_reconcile_complete', 'position_monitor_loop_error',
    'execution_order_submitted', 'execution_fill_confirmed',
}
STATES = {'open', 'entry_pending', 'exit_pending', 'closed', 'pending',
          'submitting', 'unknown', 'terminal'}


def read_regular(path, limit=65536, tail=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError('noncanonical_input')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('regular_input_required')
        if tail and info.st_size > limit:
            stream.seek(info.st_size-limit)
            return stream.read(limit).split(b'\n', 1)[-1]
        data = stream.read(limit+1)
        if len(data) > limit:
            raise ValueError('input_too_large')
        return data


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def age(value, now):
    at = timestamp(value)
    return round((now-at).total_seconds(), 1) if at else None


def flag(value):
    return 'YES' if value is True else 'NO' if value is False else 'UNKNOWN'


def named(value, allowed):
    return value if isinstance(value, str) and value in allowed else 'UNKNOWN_OR_OMITTED'


def counted_rows(value, key, allowed):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        return {'count': None, 'detail': 'NOT_PUBLISHED_OR_INVALID'}
    return {'count': len(value), 'by_'+key: dict(Counter(named(r.get(key), allowed) for r in value))}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        data = json.loads(read_regular(AUTH))
        token = data.get('api_token')
        if not isinstance(token, str) or not 0 < len(token) < 8192:
            raise ValueError('local_auth_unavailable')
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(self, endpoint):
        if endpoint not in ENDPOINTS:
            raise ValueError('read_endpoint_not_allowed')
        req = urllib.request.Request('http://127.0.0.1:8081/api/kadima/'+endpoint,
            headers={'Authorization': 'Bearer '+self.token}, method='GET')
        with self.opener.open(req, timeout=12) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError('control_response_too_large')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('invalid_control_document')
        return result


def summarize(global_data, stock_data, now):
    cfg = global_data.get('config')
    scfg = stock_data.get('controls')
    if not isinstance(cfg, dict) or not isinstance(scfg, dict):
        raise ValueError('invalid_control_schema')
    for document in (global_data, stock_data):
        seconds = age(document.get('ts'), now)
        if seconds is None or not 0 <= seconds <= 30:
            raise ValueError('control_response_stale_or_future')
    system = global_data.get('system')
    record = system.get('reconciliation') if isinstance(system, dict) else None
    if not isinstance(record, dict):
        health = {'state': 'NOT_PUBLISHED'}
    else:
        seconds = age(record.get('ts'), now)
        health = {
            'ok': flag(record.get('ok')),
            'age_seconds': seconds,
            'fresh_within_120s': seconds is not None and 0 <= seconds <= 120,
            'reason': named(record.get('reason'), REASONS) if record.get('reason') is not None else 'NOT_SUPPLIED',
            'observations': counted_rows(record.get('positions'), 'reason', REASONS),
        }
    return dict(observed_at=now.isoformat(),
        global_auto=flag(cfg.get('auto_trade_enabled')),
        stock_live=flag(scfg.get('live_enabled')),
        kill_latched=flag(cfg.get('kill_latched')),
        reconciliation=health,
        engine_positions=counted_rows(global_data.get('positions'), 'status', STATES))


def data_directory():
    # No source imports and no raw process environment emitted.
    result = subprocess.run(['systemctl', 'show', 'kadima.service', '-p', 'MainPID', '--value'],
        capture_output=True, text=True, check=True, timeout=5)
    pid = result.stdout.strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise ValueError('engine_pid_unavailable')
    proc = Path('/proc')/pid
    if (proc/'cwd').resolve() != ROOT.resolve():
        raise ValueError('engine_directory_mismatch')
    with (proc/'environ').open('rb') as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('environment_too_large')
    selected = [v.split(b'=', 1)[1] for v in raw.split(b'\0') if v.startswith(b'KADIMA_DATA_DIR=')]
    if len(selected) > 1:
        raise ValueError('ambiguous_data_directory')
    directory = Path(os.fsdecode(selected[0])) if selected else Path('/var/lib/kadima')
    if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir():
        raise ValueError('data_directory_unavailable')
    return directory


def pending_summary(directory):
    path = directory/'kadima.db'
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        return {'count': None, 'detail': 'DATABASE_UNAVAILABLE'}
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        deadline = time.monotonic()+2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute('BEGIN')
        found = conn.execute("SELECT type FROM sqlite_master WHERE name='execution_orders'").fetchone()
        if found != ('table',):
            return {'count': None, 'detail': 'LEDGER_NOT_PUBLISHED'}
        columns = {r[1] for r in conn.execute('PRAGMA table_info(execution_orders)')}
        if not {'state', 'side'} <= columns:
            return {'count': None, 'detail': 'LEDGER_SCHEMA_UNKNOWN'}
        rows = conn.execute("SELECT state,side,COUNT(*) FROM execution_orders "
            "WHERE state IS NULL OR state!='terminal' GROUP BY state,side").fetchmany(101)
        if len(rows) > 100:
            return {'count': None, 'detail': 'LEDGER_SCHEMA_UNEXPECTED'}
        return {'count': sum(r[2] for r in rows), 'groups': [
            {'state': named(s, STATES), 'side': named(side, {'BUY', 'SELL'}), 'count': n}
            for s, side, n in rows]}
    finally:
        conn.close()


def recent_events(directory, now):
    output, missing = [], 0
    for offset in (1, 0):
        day = (now-timedelta(days=offset)).astimezone(IST).date().isoformat()
        try:
            raw = read_regular(directory/'audit'/(day+'.jsonl'), 262144, tail=True)
        except (OSError, ValueError):
            missing += 1
            continue
        for line in raw.splitlines():
            if len(line) > 16384:
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('event') not in EVENTS:
                    continue
                seconds = age(event.get('ts'), now)
                if seconds is not None and 0 <= seconds <= 1800:
                    output.append({'at': timestamp(event['ts']).isoformat(), 'event': event['event']})
            except (ValueError, TypeError):
                continue
    output.sort(key=lambda x: timestamp(x['at']))
    return {'scope': 'Last 30 minutes; bounded tails; shared process audit; no raw errors',
            'last_events': output[-10:], 'unavailable_daily_files': missing}


def main():
    client = Client()
    gd, sd = client.read('top20'), client.read('stock-fast')
    now = datetime.now(IST)
    report = summarize(gd, sd, now)
    try:
        directory = data_directory()
    except Exception as exc:
        report['local_evidence'] = 'UNAVAILABLE:'+type(exc).__name__
    else:
        try:
            report['local_pending_intents'] = pending_summary(directory)
        except Exception as exc:
            report['local_pending_intents'] = {'count': None, 'detail': 'UNAVAILABLE:'+type(exc).__name__}
        report['recent_audit_events'] = recent_events(directory, now)
    report['notice'] = 'PASSIVE EVIDENCE ONLY: no new broker check, reset, resume, order or service action.'
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('DIAGNOSTIC UNAVAILABLE:', type(exc).__name__, '(private details suppressed)')
        raise SystemExit(1)
