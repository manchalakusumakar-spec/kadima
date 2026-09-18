"""Read-only, lane-specific KADIMA diagnosis. No SDK imports or broker calls.

Reads the running process's allowlisted flags, not the login shell's flags.
No setting is written and no service is started, stopped or enabled.
"""
import argparse
import ast
import http.client
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

INDICES = ('NIFTY', 'BANKNIFTY', 'SENSEX', 'MIDCPNIFTY')
FLAGS = ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_DATA_DIR')
IST = timezone(timedelta(hours=5, minutes=30))
EVENTS = {'CONFIRMED_EVENT', 'gamma_signal_emitted', 'SHIELD_DISPATCH_BLOCKED',
          'SHIELD_DISPATCH_RESULT', 'FAST_CANDIDATE', 'FAST_DISPATCH_RESULT',
          'auto_executor_entry_rejected', 'execution_order_submitted',
          'execution_fill_confirmed', 'startup_reconcile_complete',
          'startup_reconcile_broker_unreadable', 'reconcile_external_pending_orders',
          'reconcile_position_quantity_mismatch', 'oi_poll_failed', 'oi_window_degraded',
          'CONFIRMATION_GATE'}
RECONCILE_EVENTS = {'startup_reconcile_complete', 'startup_reconcile_broker_unreadable',
                    'startup_reconcile_order_book_unreadable', 'reconcile_external_pending_orders',
                    'reconcile_position_quantity_mismatch'}
EVENTS |= RECONCILE_EVENTS


def api_token(pid):
    """Keep the existing local credential in memory; never print or pass in argv."""
    try:
        for item in Path('/proc', str(pid), 'environ').read_bytes().split(b'\0'):
            key, _, value = item.partition(b'=')
            if key == b'KADIMA_API_TOKEN' and value:
                return value.decode('utf-8')
    except (OSError, UnicodeError):
        pass
    try:
        value = json.loads(Path('/etc/kadima-dashboard/config.json').read_text()).get('api_token')
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, AttributeError):
        return None


def local_get(port, path, token=None):
    """Fixed GET-only destinations; no proxies, redirects, SDK login or order calls."""
    if (port, path) not in {(8080, '/health'), (8081, '/api/kadima/config')}:
        return {'status': 'unsupported_endpoint'}, None
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        conn.request('GET', path, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            return {'status': 'http_error', 'http_status': response.status}, None
        raw = response.read(65537)
        if len(raw) > 65536:
            return {'status': 'oversized_response'}, None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {'status': 'invalid_response'}, None
        return {'status': 'ok'}, data
    except (OSError, ValueError, http.client.HTTPException):
        return {'status': 'unavailable'}, None
    finally:
        conn.close()


def runtime_state(pid):
    health_status, health = local_get(8080, '/health')
    result = {'health': health_status, 'controls': {'status': 'credential_unavailable'}}
    if health is not None:
        result['health'].update({key: health[key] if type(health.get(key)) is bool else None
                                for key in ('ok', 'market_open', 'engine_attached')})
    token = api_token(pid)
    if token:
        status, config = local_get(8081, '/api/kadima/config', token)
        result['controls'] = status
        if config is not None:
            result['controls'].update({key: config[key] if type(config.get(key)) is bool else None
                for key in ('broker_orders_enabled', 'rollout_enabled', 'auto_trade_enabled', 'kill_latched')})
            score = config.get('score_threshold')
            result['controls']['score_threshold'] = score if type(score) is int and score in (65,70,75,80,85,90) else None
    return result


def age(value, now):
    try:
        stamp = datetime.fromisoformat(value)
        seconds = (now - stamp).total_seconds() if stamp.tzinfo else -1
        return round(seconds, 1) if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


def process_flags(raw):
    values = {}
    for item in raw.split(b'\0'):
        key, _, value = item.partition(b'=')
        name = key.decode('utf-8', errors='replace')
        if name in FLAGS:
            values[name] = value.decode('utf-8', errors='replace')
    return values


def service_state(name, now):
    try:
        result = subprocess.run(['systemctl', 'show', name, '--no-pager',
            '-p', 'ActiveState', '-p', 'MainPID', '-p', 'ExecMainStartTimestampMonotonic'],
            check=True, capture_output=True, text=True, timeout=3)
        info = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        pid = int(info.get('MainPID', '0'))
        out = dict(active=info.get('ActiveState') == 'active', pid=pid, flags=None, since=None)
        started = int(info.get('ExecMainStartTimestampMonotonic', '0')) / 1_000_000
        elapsed = time.monotonic() - started
        if started > 0 and elapsed >= 0:
            out['since'] = (now - timedelta(seconds=elapsed)).isoformat()
        if pid > 0:
            try:
                out['flags'] = process_flags(Path('/proc', str(pid), 'environ').read_bytes())
            except OSError:
                out['flags_status'] = 'unreadable_use_host_read_permissions'
        return out
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(active=None, pid=None, flags=None, since=None, status='unavailable')


def source_order_setting(root):
    """On-disk value only: never execute config.py or claim the loaded value."""
    try:
        tree = ast.parse((root / 'kadima' / 'config.py').read_text())
        for item in tree.body:
            if isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'ORDER_PLACEMENT_ENABLED' for t in item.targets):
                return item.value.value if isinstance(item.value, ast.Constant) and type(item.value.value) is bool else None
    except (OSError, SyntaxError):
        pass
    return None


def database_state(path, now):
    out = dict(controls_status='unavailable', controls={}, oi_age_seconds={}, news={}, reconciliation={})
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            try:
                row = db.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
                cfg = json.loads(row[0]) if row else dict(auto_trade_enabled=False, kill_latched=False, score_threshold=65)
                if (not isinstance(cfg, dict) or any(type(cfg.get(k)) is not bool
                        for k in ('auto_trade_enabled', 'kill_latched'))
                        or type(cfg.get('score_threshold')) is not int
                        or cfg['score_threshold'] not in (65, 70, 75, 80, 85, 90)):
                    raise ValueError('invalid settings')
                out['controls_status'] = 'saved' if row else 'defaults_unsaved'
                out['controls'] = {k:cfg[k] for k in ('auto_trade_enabled', 'kill_latched', 'score_threshold')}
            except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
                pass
            for name in INDICES:
                try:
                    row = db.execute('SELECT ts FROM oi_snapshots WHERE index_name=? ORDER BY ts DESC LIMIT 1', (name,)).fetchone()
                    out['oi_age_seconds'][name] = age(row[0], now) if row else None
                except sqlite3.DatabaseError:
                    out['oi_age_seconds'][name] = None
            for name in ('news', 'reconciliation'):
                try:
                    row = db.execute('SELECT ts,payload FROM celestial_health WHERE name=?', (name,)).fetchone()
                    if not row:
                        continue
                    h = json.loads(row[1])
                    if not isinstance(h, dict):
                        continue
                    stamp = h.get('ts', row[0])
                    out[name] = dict(recorded_ok=h.get('ok') is True, age_seconds=age(stamp, now))
                    if name == 'news':
                        covered = h.get('covered_symbols', [])
                        fresh = out[name]['age_seconds'] is not None and out[name]['age_seconds'] <= 300
                        out[name]['covered_indices'] = [n for n in INDICES if isinstance(covered, list) and n in covered]
                        out[name]['verified_indices'] = out[name]['covered_indices'] if fresh and h.get('ok') is True else []
                except (sqlite3.DatabaseError, ValueError, TypeError):
                    continue
    except sqlite3.DatabaseError:
        out['database_status'] = 'unavailable'
    return out


def lane_controls(service, database):
    flags, controls = service.get('flags'), database['controls']
    if flags is None:
        return dict(mode='unknown_runtime_flags', fast_lane=['runtime_flags_unverified'])
    celestial = flags.get('KADIMA_CELESTIAL_ENABLED', '0') == '1'
    blocked = []
    if flags.get('KADIMA_FAST_MOMENTUM_LIVE', '0') != '1': blocked.append('fast_observation_only')
    if not celestial: blocked.append('rollout_disabled')
    if not controls: blocked.append('saved_controls_unverified')
    elif not controls['auto_trade_enabled']: blocked.append('auto_paused')
    if controls.get('kill_latched'): blocked.append('kill_latched')
    missing = [n for n in INDICES if n not in database['news'].get('verified_indices', [])]
    if missing: blocked.append('index_news_coverage_missing:' + ','.join(missing))
    return dict(mode='celestial' if celestial else 'legacy_shield_gamma', fast_lane=blocked,
                note='Fast-lane blockers do not disable legacy Shield/Gamma. Candidate, kill-file, risk and broker checks still apply.')


def audit_state(path, since, now):
    counts, reasons, checks = Counter(), Counter(), Counter()
    last_reconcile = None
    parsed = 0
    try:
        with path.open() as source:
            for line in source:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict): continue
                    stamp = datetime.fromisoformat(row.get('ts', ''))
                    if stamp.tzinfo is None or not since <= stamp <= now: continue
                except (ValueError, TypeError):
                    continue
                parsed += 1
                event = row.get('event')
                if not isinstance(event, str) or event not in EVENTS: continue
                counts[event] += 1
                if event in RECONCILE_EVENTS and (last_reconcile is None or stamp >= last_reconcile[0]):
                    last_reconcile = (stamp, event)
                # Output only bounded code-like reason labels, never raw exceptions.
                values = row.get('blocked_reasons', [])
                values = list(values) if isinstance(values, list) else []
                values.append(row.get('reason'))
                for value in values:
                    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value): reasons[value] += 1
                if event == 'oi_poll_failed' and 'shared quote budget' in str(row.get('error', '')).lower():
                    reasons['local_quote_budget_timeout'] += 1
                if event == 'CONFIRMATION_GATE' and isinstance(row.get('checks'), dict):
                    for key in ('score','oi','extra_5m_hold','no_invalidation','vwap','ema','rsi','bank_confirmation','spot_fresh','technical_fresh'):
                        if row['checks'].get(key) is False: checks[key] += 1
        latest = ({'event': last_reconcile[1], 'at': last_reconcile[0].isoformat(),
                   'age_seconds': age(last_reconcile[0].isoformat(), now)} if last_reconcile else {})
        return dict(status='retained_records_only', since=since.isoformat(), records=parsed,
                    latest_reconciliation=latest,
                    events=dict(counts), reasons=dict(reasons), failed_checks=dict(checks))
    except OSError:
        return dict(status='unavailable', since=since.isoformat())


def compact(report):
    db, lanes, audit = report['database'], report['lanes'], report['audit']
    encoded = lambda value: json.dumps(value, separators=(',', ':'))
    lines = [
        'READ ONLY ' + report['at'],
        'ENGINE active=' + str(report['engine'].get('active')) + ' mode=' + lanes['mode'],
        'ORDER switch on disk=' + str(report['order_switch_on_disk']) +
            (' (compare RUNNING API controls)' if 'runtime' in report else ' (loaded value unverified)'),
        'KILL file=' + str(report['kill_file_present']),
        'SAVED controls=' + db['controls_status'] + ' ' + encoded(db['controls']),
        'OI sample ages (seconds)=' + encoded(db['oi_age_seconds']),
        'NEWS=' + encoded(db['news']),
        'RECONCILIATION recorded=' + encoded(db['reconciliation']),
        'LATEST broker reconciliation audit=' + encoded(audit.get('latest_reconciliation', {})),
        'FAST blockers=' + encoded(lanes['fast_lane']),
        'AUDIT since=' + audit['since'] + ' status=' + audit['status'],
        'EVENTS=' + encoded(audit.get('events', {})),
        'ENTRY blockers=' + encoded(audit.get('reasons', {})),
        'SHIELD failed checks=' + encoded(audit.get('failed_checks', {})),
        report['verdict'],
    ]
    if 'runtime' in report:
        lines[-1:-1] = ['RUNNING API health=' + encoded(report['runtime']['health']),
                        'RUNNING API controls=' + encoded(report['runtime']['controls'])]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/kadima-py'))
    parser.add_argument('--json', action='store_true', help='Show the complete structured report')
    parser.add_argument('--runtime', action='store_true',
                        help='Also GET local health and loaded controls; no order or broker-login calls')
    args = parser.parse_args()
    now = datetime.now(IST)
    service = service_state('kadima.service', now)
    flags = service.get('flags') or {}
    folder = Path(flags.get('KADIMA_DATA_DIR', '/var/lib/kadima'))
    if not folder.is_absolute():
        parser.exit(1, 'Relative KADIMA_DATA_DIR requires explicit host path review.\n')
    database = database_state(folder / 'kadima.db', now)
    try:
        kill_file_present = (folder / 'KILL').exists()
    except OSError:
        kill_file_present = None
    since = datetime.fromisoformat(service['since']) if service.get('since') else now - timedelta(minutes=15)
    since = max(since, now.replace(hour=0, minute=0, second=0, microsecond=0))
    report = dict(at=now.isoformat(), read_only=True,
        engine={k:v for k,v in service.items() if k != 'flags'},
        order_switch_on_disk=source_order_setting(args.root),
        kill_file_present=kill_file_present,
        lanes=lane_controls(service, database), database=database,
        audit=audit_state(folder / 'audit' / (now.date().isoformat() + '.jsonl'), since, now),
        verdict='NOT_CERTIFIED: running order switch, complete entry conditions and broker books are not verified by this report.')
    if args.runtime:
        report['runtime'] = runtime_state(service.get('pid'))
        report['verdict'] = ('NOT_CERTIFIED: API controls and the latest recorded broker check are diagnostic evidence; '
                             'current broker connectivity, complete entry eligibility and live fills remain unverified.')
    if service_state('kadima.service', now).get('pid') != service.get('pid'):
        report['verdict'] = 'RUNTIME_CHANGED: engine restarted during this read; rerun.'
    print(json.dumps(report, indent=2) if args.json else compact(report))


if __name__ == '__main__':
    main()
