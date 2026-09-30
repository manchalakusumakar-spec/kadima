"""Calendar + expiring entry permit. Does not affect exits or contact a broker."""
from datetime import datetime, date, time as dtime
import json
import math
import os
from pathlib import Path
import stat
import time
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
# Public calendar only; credentials remain under private /etc/kadima.
POLICY = Path('/etc/kadima-session-auto/policy.json')
LEASE = Path('/run/kadima-session-auto/lease.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')
MAX_LEASE_SECONDS = 150


def read_json(path, limit=65536, require_root=True):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('noncanonical_schedule_file')
    with path.open('rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (require_root and (info.st_uid != 0 or info.st_mode & 0o022)):
            raise ValueError('unsafe_schedule_file')
        data = handle.read(limit+1)
    if len(data) > limit:
        raise ValueError('schedule_file_too_large')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('invalid_schedule_document')
    return value


def validate_policy(policy):
    if set(policy) != {'schema', 'valid_from', 'valid_through', 'holidays', 'sources', 'arm_at', 'entries_at', 'stop_entries_at'} or type(policy['schema']) is not int or policy['schema'] != 1:
        raise ValueError('invalid_calendar_schema')
    start, end = date.fromisoformat(policy['valid_from']), date.fromisoformat(policy['valid_through'])
    if start > end or (end-start).days > 366:
        raise ValueError('invalid_calendar_coverage')
    if not isinstance(policy['holidays'], list) or len(policy['holidays']) > 64:
        raise ValueError('invalid_calendar_holidays')
    days = [date.fromisoformat(x) for x in policy['holidays']]
    if len(set(days)) != len(days) or any(x.year != end.year for x in days):
        raise ValueError('invalid_calendar_dates')
    clocks = [dtime.fromisoformat(policy[x]) for x in ('arm_at', 'entries_at', 'stop_entries_at')]
    if not dtime(9) <= clocks[0] <= clocks[1] < clocks[2] <= dtime(15,35):
        raise ValueError('invalid_session_times')
    if clocks[1] < dtime(9,15) or any(t.tzinfo is not None for t in clocks):
        raise ValueError('invalid_session_times')
    if not isinstance(policy['sources'], list) or not policy['sources']:
        raise ValueError('calendar_provenance_missing')
    return policy


def calendar_reason(policy, now, *, entries=False):
    validate_policy(policy)
    if now.tzinfo is None:
        return 'schedule_clock_timezone_missing'
    local = now.astimezone(IST)
    day = local.date()
    if not date.fromisoformat(policy['valid_from']) <= day <= date.fromisoformat(policy['valid_through']):
        return 'calendar_update_required'
    if day.weekday() >= 5 or day.isoformat() in policy['holidays']:
        return 'non_trading_day'
    start = policy['entries_at'] if entries else policy['arm_at']
    if not dtime.fromisoformat(start) <= local.time().replace(tzinfo=None) < dtime.fromisoformat(policy['stop_entries_at']):
        return 'outside_scheduled_window'
    return None


def permit_reason(permit, now, mono, boot_id):
    if permit.get('allowed') is not True or permit.get('day') != now.astimezone(IST).date().isoformat():
        return 'session_permit_not_granted'
    if permit.get('boot_id') != boot_id:
        return 'session_permit_wrong_boot'
    issued, expires = permit.get('issued_mono'), permit.get('expires_mono')
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in (issued,expires)):
        return 'session_permit_invalid'
    if not 0 <= mono-issued <= MAX_LEASE_SECONDS or not issued < expires <= issued+MAX_LEASE_SECONDS or mono >= expires:
        return 'session_permit_expired'
    stamp = datetime.fromisoformat(permit['issued_at'])
    if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= MAX_LEASE_SECONDS:
        return 'session_permit_clock_invalid'
    return None


def entry_block(exchange, now=None, product='INTRADAY'):
    """Called twice on the shared entry path, never from position exit logic.
    product='CARRYFORWARD' (CAS only) skips just the broker's 15:15 INTRADAY
    entry window; the calendar, policy schedule and lane permit still apply."""
    priority = os.environ.get('KADIMA_PRIORITY_TRADING') == '1'
    if priority:
        from . import priority_sessions
        now = now or datetime.now(IST)
        try:
            reason = priority_sessions.calendar_reason(exchange, now)
            if reason == 'outside_broker_intraday_entry_window' and product == 'CARRYFORWARD':
                reason = None
            if reason: return reason
            if exchange in ('NFO', 'BFO'):
                reason = calendar_reason(read_json(POLICY), now, entries=True)
                if reason: return reason
            permit = read_json(LEASE)
            if (permit.get('lanes') or {}).get('mcx' if exchange == 'MCX' else 'equity') is not True:
                return 'lane_permit_not_granted'
            return permit_reason(permit, now, time.monotonic(), BOOT.read_text().strip())
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return 'session_schedule_unavailable'
    if os.environ.get('KADIMA_SESSION_AUTOTRADE') != '1' or exchange not in ('NFO','BFO'):
        return None
    now = now or datetime.now(IST)
    try:
        reason = calendar_reason(read_json(POLICY), now, entries=True)
        if reason:
            return reason
        return permit_reason(read_json(LEASE), now, time.monotonic(), BOOT.read_text().strip())
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return 'session_schedule_unavailable'
