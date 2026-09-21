"""Extract only broker error codes from bounded local logs after a failed reset.

No SDK import, network/broker request, reset, resume, source write or restart.
SDK log tails may contain old events; their presence is evidence, not live status.
Never echo complete lines, headers, tokens, response bodies or exception text.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

ROOT = Path('/home/ubuntu/kadima-py')
IST = timezone(timedelta(hours=5, minutes=30))
LIMIT = 524288
CODES = {
    'AG8001': 'INVALID_TOKEN', 'AG8002': 'TOKEN_EXPIRED', 'AG8003': 'TOKEN_MISSING',
    'AB8050': 'INVALID_REFRESH_TOKEN', 'AB8051': 'REFRESH_TOKEN_EXPIRED',
    'AB1000': 'INVALID_LOGIN', 'AB1004': 'BROKER_ERROR_RETRY_LATER',
    'AB1006': 'CLIENT_BLOCKED_FOR_TRADING', 'AB1007': 'AMX_ERROR',
    'AB1010': 'AMX_SESSION_EXPIRED', 'AB1011': 'CLIENT_NOT_LOGGED_IN',
}
PATTERN = re.compile(r'''["']errorcode["']\s*:\s*["']((?:AG|AB)\d{4})["']''', re.I)
ROUTES = (('/getPosition', 'POSITIONS'), ('/getOrderBook', 'ORDERS'),
          ('/loginByPassword', 'LOGIN'), ('/getProfile', 'PROFILE'))


def codes_from_message(message):
    if not isinstance(message, str) or len(message) > LIMIT:
        return []
    scope = next((label for route, label in ROUTES if route in message), None)
    if scope is None:
        return []
    return [dict(operation=scope, code=code, meaning=CODES[code])
            for code in sorted({m.upper() for m in PATTERN.findall(message)} & CODES.keys())]


def bounded_tail(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path:
        raise ValueError()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError()
        truncated = info.st_size > LIMIT
        if truncated:
            stream.seek(info.st_size-LIMIT)
        raw = stream.read(LIMIT)
    return raw.split(b'\n', 1)[-1] if truncated else raw


def journal_evidence():
    # The root-private temporary capture is unlinked automatically and never printed.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run([
            'journalctl', '-u', 'kadima.service', '--since', '-2 hours',
            '-n', '500', '--no-pager', '-o', 'json',
            '--output-fields=__REALTIME_TIMESTAMP,MESSAGE'], stdout=output,
            stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode:
            return [], 'UNAVAILABLE'
        end = output.tell()
        output.seek(max(0, end-LIMIT))
        raw = output.read(LIMIT)
        if end > LIMIT:
            raw = raw.split(b'\n', 1)[-1]
    rows = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
            message = value.get('MESSAGE')
            at = datetime.fromtimestamp(int(value['__REALTIME_TIMESTAMP'])/1000000, IST).isoformat()
            rows.extend(dict(item, source='ENGINE_JOURNAL', at=at)
                        for item in codes_from_message(message))
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError, OSError):
            continue
    return rows, 'READ_BOUNDED_LAST_2_HOURS'


def collect():
    try:
        rows, journal_status = journal_evidence()
    except (OSError, subprocess.TimeoutExpired):
        rows, journal_status = [], 'UNAVAILABLE'
    dates = set()
    for clock in (datetime.now().astimezone(), datetime.now(IST)):
        dates.update((clock-timedelta(days=n)).date().isoformat() for n in (0, 1))
    sdk_read = 0
    for day in sorted(dates):
        try:
            raw = bounded_tail(ROOT/'logs'/day/'app.log')
        except (OSError, ValueError):
            continue
        sdk_read += 1
        for line in raw.decode('utf-8', errors='replace').splitlines():
            rows.extend(dict(item, source='SDK_LOG_TAIL', log_folder_day=day,
                             at='NOT_VERIFIED_FROM_LOG_LINE')
                        for item in codes_from_message(line))
    # Journal timestamps are precise; SDK folder dates are NOT event timestamps.
    unique = []
    for row in rows:
        if row not in unique:
            unique.append(row)
    journal = [r for r in unique if r['source']=='ENGINE_JOURNAL'][-8:]
    sdk = [r for r in unique if r['source']=='SDK_LOG_TAIL'][-8:]
    return dict(observed_at=datetime.now(IST).isoformat(), journal=journal_status,
                sdk_logs_read=sdk_read, journal_errors=journal, sdk_tail_errors=sdk,
                result='RECORDED_CODES_FOUND' if unique else 'NO_RECOGNIZED_CODE_IN_BOUNDED_LOGS',
                warning='Logs are historical, not a current broker check; SDK tails are not time-filtered.',
                actions='NO LOGIN / NO BROKER REQUEST / NO RESET / NO RESTART / NO INSTALL')


if __name__ == '__main__':
    try:
        print(json.dumps(collect(), indent=2))
    except Exception:
        print('BROKER LOG REVIEW UNAVAILABLE (private details suppressed).')
        raise SystemExit(1)
