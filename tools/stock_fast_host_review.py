#!/usr/bin/env python3
"""Passive host review; no broker calls, live imports, flags or service changes.

Run with python3 -I -B -S. Only fixed loopback GETs, read-only SQLite queries,
fixed systemctl show calls and source/process observations are used. An absent
health record is NOT_PUBLISHED, not a claim that a worker failed. No output of
credentials, complete process environments, command arguments or provider bodies.
"""
import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SOURCE_HASHES = {
    'run.py': {'b27d03215ea7ba7e4fe6e73b1052da208beea8d2': 'BASE_ENTRYPOINT',
               '9036548a589752d26efe2bf952b25cdc590a7cdf': 'STOCK_FAST_ENTRYPOINT'},
    'kadima/rate_limiter.py': {'9886d951be6997e0ddf8fdda640529f5f751a25c': 'FIFO_PRESERVE',
                             '34867007aeb2feba2b8fc15e0f90610302321aa8': 'OLD_NON_FIFO'},
    'kadima/full_universe_scanner.py': {'aca08c5c6af10e7fffbbb316eea3d039aeb41731': 'BASE_SCANNER',
                                       '31b7e19c9a6283831ab8c2dd1057d2e37fb3d100': 'STOCK_FAST_SCANNER'},
    'kadima/stock_fast.py': {'d4c537156dbb080a50b249d5164e9506f1e1b8dd': 'STOCK_FAST_MODULE'},
    'kadima/stock_fast_api.py': {'0d9b50418d6cc12752ebf96114e98504eaca455d': 'STOCK_FAST_API'},
}
UNITS = ('kadima.service', 'kadima-universe-scanner.service', 'kadima-dashboard.service')
URLS = {'health': 'http://127.0.0.1:8080/health',
        'celestial': 'http://127.0.0.1:8081/api/kadima/top20',
        'stock_fast': 'http://127.0.0.1:8081/api/kadima/stock-fast'}
FLAGS = ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_FAST_MOMENTUM_LIVE')
REASONS = frozenset(('checking_broker_books', 'contract_master_unavailable',
    'external_pending_orders', 'external_fill_history_unresolved', 'invalid_broker_position',
    'worker_error', 'broker_reconciliation_required', 'fresh_news_coverage_required',
    'news_coverage_unavailable', 'stock_fast_live_disabled', 'fast_observation_only',
    'rollout_disabled', 'auto_paused', 'kill_latched', 'broker_orders_disabled',
    'reconcile_position_quantity_mismatch', 'startup_reconcile_not_complete',
    'external_position_unresolved', 'external_position_not_adopted'))
ERROR_CATEGORIES = ('announcements', 'board_meetings', 'corporate_actions',
                    'financial_results', 'company_mapping')


def boolean(value):
    return value if type(value) is bool else None


def age(stamp, now):
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            return None
        return (now - parsed).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def safe_reasons(value):
    """Exact allowlist, not a regex that could echo a lowercase secret."""
    found = []
    if isinstance(value, dict):
        if 'reason' in value:
            reason = value['reason']
            found.append(reason if isinstance(reason, str) and reason in REASONS
                         else 'UNRECOGNIZED_REASON_REDACTED')
        positions = value.get('positions')
        if isinstance(positions, list):
            for item in positions[:100]:
                if isinstance(item, dict) and 'reason' in item:
                    reason = item['reason']
                    found.append(reason if isinstance(reason, str) and reason in REASONS
                                 else 'UNRECOGNIZED_REASON_REDACTED')
    return sorted(set(found))


def health_record(value, now, ttl):
    if value is None:
        return dict(state='NOT_PUBLISHED', ok=None, age_seconds=None, fresh=None, reasons=[])
    if not isinstance(value, dict):
        return dict(state='INVALID_RECORD', ok=None, age_seconds=None, fresh=None, reasons=[])
    ok = boolean(value.get('ok'))
    seconds = age(value.get('ts'), now)
    fresh = None if seconds is None else 0 <= seconds <= ttl
    state = 'INVALID_OK' if ok is None else 'UNHEALTHY' if not ok else 'HEALTHY'
    if seconds is None:
        state += '_TIME_UNKNOWN'
    elif seconds < 0:
        state += '_FUTURE_TIME'
    elif not fresh:
        state += '_STALE'
    errors = value.get('errors')
    return dict(state=state, ok=ok, age_seconds=round(seconds, 1) if seconds is not None else None,
                fresh=fresh, reasons=safe_reasons(value),
                error_categories=[k for k in ERROR_CATEGORIES if isinstance(errors, dict) and k in errors])


def source_inventory(root):
    out = {}
    root = root.resolve()
    for relative, known in SOURCE_HASHES.items():
        file = root / relative
        try:
            # Unknown/symlinked layouts need human reconciliation, not an overwrite.
            if file.is_symlink() or not file.resolve().is_relative_to(root):
                out[relative] = dict(state='SYMLINK_REVIEW_REQUIRED', blob=None)
                continue
            with file.open('rb') as handle:
                raw = handle.read(2_000_001)
            if len(raw) > 2_000_000:
                out[relative] = dict(state='OVERSIZE_REVIEW_REQUIRED', blob=None)
                continue
            digest = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
            out[relative] = dict(state=known.get(digest, 'UNRECOGNIZED_KEEP_UNCHANGED'), blob=digest)
        except FileNotFoundError:
            out[relative] = dict(state='MISSING', blob=None)
        except OSError:
            out[relative] = dict(state='UNREADABLE', blob=None)
    return out


def services():
    out = {}
    for unit in UNITS:
        try:
            result = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '-p',
                                     'SubState', '-p', 'MainPID'], capture_output=True, text=True, timeout=5)
            if result.returncode or len(result.stdout) > 4096:
                raise ValueError('unavailable')
            fields = {}
            for line in result.stdout.splitlines():
                key, sep, value = line.partition('=')
                if sep and key in ('ActiveState', 'SubState', 'MainPID'):
                    fields[key] = value if re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', value) else 'UNKNOWN'
            out[unit] = fields
        except (OSError, ValueError, subprocess.TimeoutExpired):
            out[unit] = {'ActiveState': 'UNKNOWN', 'SubState': 'UNKNOWN', 'MainPID': '0'}
    return out


def script_identity(command, cwd, target):
    """Only the interpreter's script operand. Never output arbitrary argv."""
    if not command or not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(command[0]).name):
        return 'NON_PYTHON_OR_UNKNOWN'
    for token in command[1:]:
        if token in ('-B', '-u', '-E', '-s', '-S', '-I', '-O', '-OO', '-P'):
            continue
        if token.startswith('-'):
            return 'MODULE_OR_CUSTOM_ENTRYPOINT'
        candidate = Path(token)
        path = candidate if candidate.is_absolute() else cwd / candidate
        try:
            relative = path.resolve().relative_to(target.resolve()).as_posix()
        except (OSError, ValueError):
            return 'OUTSIDE_TARGET_REVIEW_REQUIRED'
        return relative if relative in SOURCE_HASHES else 'CUSTOM_ENTRYPOINT_REVIEW_REQUIRED'
    return 'UNKNOWN'


def process_review(pid, root, proc=Path('/proc')):
    public = dict(available=False, cwd_matches_target=None, script='UNKNOWN',
                  flags={name: None for name in FLAGS})
    data_dir = None
    if not isinstance(pid, str) or not pid.isdigit() or int(pid) <= 0:
        return public, data_dir
    try:
        directory = proc / pid
        # These stay local and are never serialized. Limits also cover corrupted files.
        env_raw = (directory / 'environ').read_bytes()
        cmd_raw = (directory / 'cmdline').read_bytes()
        if len(env_raw) > 1_000_000 or len(cmd_raw) > 1_000_000:
            raise ValueError('oversize')
        env = dict(x.split(b'=', 1) for x in env_raw.split(b'\0') if b'=' in x)
        for flag in FLAGS:
            v = env.get(flag.encode())
            public['flags'][flag] = v == b'1' if v in (b'0', b'1') else None
        raw_dir = env.get(b'KADIMA_DATA_DIR')
        data_dir = Path(raw_dir.decode()) if raw_dir else Path('/var/lib/kadima')
        if not data_dir.is_absolute():
            data_dir = None
        cwd = (directory / 'cwd').resolve(strict=True)
        command = [x.decode() for x in cmd_raw.split(b'\0') if x]
        public.update(available=True, cwd_matches_target=cwd == root.resolve(),
                      script=script_identity(command, cwd, root))
    except (OSError, ValueError, UnicodeError):
        pass
    return public, data_dir


def local_counts(data_dir):
    out = dict(open_positions=None, pending_intents=None, state='UNKNOWN')
    if data_dir is None:
        return out
    try:
        path = data_dir / 'kadima.db'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'positions' in tables:
                out['open_positions'] = db.execute("SELECT count(*) FROM positions WHERE status != 'closed'").fetchone()[0]
            if 'execution_orders' in tables:
                out['pending_intents'] = db.execute("SELECT count(*) FROM execution_orders WHERE state != 'terminal'").fetchone()[0]
            out['state'] = 'OBSERVED' if all(out[k] is not None for k in ('open_positions', 'pending_intents')) else 'SCHEMA_INCOMPLETE'
    except (OSError, ValueError, sqlite3.Error):
        out['state'] = 'UNAVAILABLE'
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(name, token=None):
    if name not in URLS:
        raise ValueError('unknown_local_endpoint')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    request = urllib.request.Request(URLS[name], headers=headers, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return dict(http=None, error='OVERSIZE'), None
            data = json.loads(raw)
            if not isinstance(data, dict):
                return dict(http=None, error='INVALID_SCHEMA'), None
            return dict(http=response.status, error=None), data
    except urllib.error.HTTPError as exc:
        return dict(http=exc.code, error='HTTP_ERROR'), None
    except (OSError, ValueError):
        return dict(http=None, error='UNAVAILABLE_OR_INVALID_JSON'), None


def api_observation(status, now):
    cfg = status.get('config') if isinstance(status, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    system = status.get('system') if isinstance(status, dict) else None
    system = system if isinstance(system, dict) else {}
    positions = status.get('positions') if isinstance(status, dict) else None
    records = {name: health_record(system.get(name), now, ttl) for name, ttl in
               (('reconciliation', 120), ('news', 300), ('stock_universe', 90))}
    if not isinstance(status, dict):
        for record in records.values():
            record['state'] = 'API_UNAVAILABLE'
    elif not isinstance(status.get('system'), dict):
        for record in records.values():
            record['state'] = 'SYSTEM_SCHEMA_UNAVAILABLE'
    return dict(config={k: boolean(cfg.get(k)) for k in
                        ('rollout_enabled', 'auto_trade_enabled', 'broker_orders_enabled', 'kill_latched')},
                reconciliation=records['reconciliation'],
                news=records['news'], stock_universe=records['stock_universe'],
                api_position_count=len(positions) if isinstance(positions, list) else None,
                api_age_seconds=age(status.get('ts'), now) if isinstance(status, dict) else None,
                broker_flat_verified=False)


def collect(root, config_path=Path('/etc/kadima-dashboard/config.json')):
    started = datetime.now(timezone.utc)
    units = services()
    process, data_dir = process_review(units.get('kadima.service', {}).get('MainPID'), root)
    endpoints = {}
    endpoints['health'], health = read_json('health')
    token = None
    try:
        with config_path.open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError('oversize')
        candidate = json.loads(raw).get('api_token')
        if isinstance(candidate, str) and 0 < len(candidate) < 8192:
            token = candidate
    except (OSError, ValueError, AttributeError):
        pass
    status = fast_status = None
    if token:
        endpoints['celestial'], status = read_json('celestial', token)
        endpoints['stock_fast'], fast_status = read_json('stock_fast', token)
    else:
        for name in ('celestial', 'stock_fast'):
            endpoints[name] = dict(http=None, error='LOCAL_AUTH_UNAVAILABLE')
    now = datetime.now(timezone.utc)
    result = dict(observed_at=now.astimezone(ZoneInfo('Asia/Kolkata')).isoformat(),
                  duration_seconds=round((now-started).total_seconds(), 1),
                  services=units, process=process, sources=source_inventory(root),
                  endpoints=endpoints, local_counts=local_counts(data_dir),
                  api=api_observation(status, now),
                  market_open=boolean(health.get('market_open')) if isinstance(health, dict) else None,
                  stock_fast_live_requested=None, stock_fast_worker_alive=None,
                  verdict='DIAGNOSTIC_ONLY_NOT_RELEASE_APPROVAL',
                  deployment_performed=False, live_settings_changed=False)
    if isinstance(fast_status, dict):
        cfg = fast_status.get('controls')
        result['stock_fast_live_requested'] = boolean(cfg.get('live_enabled')) if isinstance(cfg, dict) else None
        result['stock_fast_worker_alive'] = boolean(fast_status.get('worker_alive'))
    return result


def word(value):
    if value is True:
        return 'YES'
    if value is False:
        return 'NO'
    return 'UNKNOWN' if value is None else str(value)


def summary(result):
    api = result['api']; cfg = api['config']; process = result['process']
    lines = ['OBSERVED: ' + result['observed_at']]
    for label, unit in zip(('ENGINE', 'SCANNER', 'DASHBOARD'), UNITS):
        row = result['services'][unit]
        lines.append(label + ': ' + row.get('ActiveState', 'UNKNOWN') + '/' + row.get('SubState', 'UNKNOWN'))
    lines.extend([
        'ENTRY SCRIPT: ' + process['script'],
        'RUN.PY: ' + result['sources']['run.py']['state'],
        'RUN HASH: ' + (result['sources']['run.py']['blob'] or 'UNKNOWN'),
        'RATE LIMITER: ' + result['sources']['kadima/rate_limiter.py']['state'],
        'SCANNER CODE: ' + result['sources']['kadima/full_universe_scanner.py']['state'],
        'ROLLOUT/AUTO/FAST: ' + '/'.join(word(x) for x in (cfg['rollout_enabled'], cfg['auto_trade_enabled'],
                                 process['flags']['KADIMA_FAST_MOMENTUM_LIVE'])),
        'BROKER/KILL: ' + word(cfg['broker_orders_enabled']) + '/' + word(cfg['kill_latched']),
        'CONTROL HTTP: ' + word(result['endpoints']['celestial']['http']),
        'RECON: ' + api['reconciliation']['state'],
        'RECON REASON: ' + (','.join(api['reconciliation']['reasons']) or 'not supplied'),
        'NEWS: ' + api['news']['state'],
        'NEWS ERRORS: ' + (','.join(api['news'].get('error_categories', [])) or 'not supplied'),
        'COUNTS local/API/pending: ' + '/'.join(word(x) for x in
              (result['local_counts']['open_positions'], api['api_position_count'], result['local_counts']['pending_intents'])),
        'STOCK API HTTP: ' + word(result['endpoints']['stock_fast']['http']),
        'BROKER FLAT: NOT VERIFIED; no broker request made',
        'NO INSTALL / NO RESTART / NO LIVE ACTIVATION',
    ])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('/home/ubuntu/kadima-py'))
    parser.add_argument('--json', action='store_true', help='Full sanitized report instead of phone summary')
    args = parser.parse_args()
    result = collect(args.target)
    print(json.dumps(result, indent=2) if args.json else summary(result))


if __name__ == '__main__':
    main()
