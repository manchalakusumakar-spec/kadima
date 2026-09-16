"""Host memory admission guard. Never stops scans, feeds or position management.

One tiny monitor thread in the engine; entry paths additionally read fresh
kernel counters before sending a BUY. No broker calls or unbounded history.
"""
import json
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone

from . import config

RECOVERY_SECONDS = 60


def available_memory():
    with open('/proc/meminfo') as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError('oversized_memory_data')
    values = {}
    for line in raw.splitlines():
        key, _, value = line.partition(':')
        if key in ('MemTotal', 'MemAvailable'):
            fields = value.split()
            if len(fields) != 2 or fields[1] != 'kB':
                raise ValueError('invalid_memory_unit')
            values[key] = int(fields[0]) * 1024
    total, available = values['MemTotal'], values['MemAvailable']
    if not 0 <= available <= total or total <= 0:
        raise ValueError('invalid_memory_range')
    return total, available


class MemoryGuard:
    def __init__(self, reader=available_memory, clock=time.monotonic):
        self.reader, self.clock = reader, clock
        self.lock = threading.Lock()
        self.state = 'starting'
        self.recovery_at = None
        self.last_at = None
        self.cached = None
        self.minimum_available = None

    def sample(self, force=False):
        with self.lock:
            now = self.clock()
            if not force and self.cached and 0 <= now-self.last_at < 1:
                return dict(self.cached)
            # A long unsampled interval cannot establish continuous recovery.
            if self.last_at is not None and not 0 <= now-self.last_at <= 10:
                self.state, self.recovery_at = 'starting', None
            self.last_at = now
            total = available = pct = None
            try:
                total, available = self.reader()
                if not 0 <= available <= total or total <= 0:
                    raise ValueError('invalid_memory_range')
                pct = 100*available/total
                self.minimum_available = min(self.minimum_available if self.minimum_available is not None else available, available)
                if pct < 10:
                    self.state, self.recovery_at = 'critical', None
                elif self.state in ('starting','critical','unavailable','recovering'):
                    if pct >= 25:
                        self.recovery_at = now if self.recovery_at is None else self.recovery_at
                        self.state = 'normal' if now-self.recovery_at >= RECOVERY_SECONDS else 'recovering'
                    else:
                        self.state, self.recovery_at = 'recovering', None
                elif pct < 20:
                    self.state, self.recovery_at = 'warning', None
                elif self.state == 'warning':
                    if pct >= 25:
                        self.recovery_at = now if self.recovery_at is None else self.recovery_at
                        if now-self.recovery_at >= RECOVERY_SECONDS:
                            self.state = 'normal'
                    else:
                        self.recovery_at = None
            except (OSError, ValueError, KeyError, TypeError):
                self.state, self.recovery_at = 'unavailable', None
            self.cached = dict(ts=datetime.now(timezone.utc).isoformat(), state=self.state,
                available_percent=round(pct,1) if pct is not None else None,
                available_bytes=available, total_bytes=total,
                minimum_available_bytes=self.minimum_available,
                entries_blocked=self.state not in ('normal','warning'),
                optional_work_delayed=self.state != 'normal',
                recovery_seconds=max(0,round(RECOVERY_SECONDS-(now-self.recovery_at)))
                    if self.recovery_at is not None and self.state != 'normal' else None,
                gamma_cas_protected=True)
            return dict(self.cached)


GUARD = MemoryGuard()
_worker_lock = threading.Lock()
_worker = None


def _hold_path():
    return Path(config.DATA_DIR)/'operations-entry-hold.json'


def hold_status():
    try:
        path = _hold_path()
        with path.open() as f:
            data = json.loads(f.read(1025))
        if set(data) != {'token'} or not re.fullmatch('[a-f0-9]{32}', data['token']):
            raise ValueError('invalid_hold')
        return data['token']
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        return 'unavailable'


def hold_entries(token):
    if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}', token):
        raise ValueError('invalid_hold_token')
    path = _hold_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        os.chmod(temp,0o600)
        json.dump({'token':token},f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def release_entries(token):
    current = hold_status()
    if current is None:
        return True
    if not isinstance(token,str) or current != token or current == 'unavailable':
        return False
    _hold_path().unlink()
    return True


def entry_block_reason():
    if hold_status() is not None:
        return 'operations_entries_paused'
    result = GUARD.sample(force=True)
    if result['entries_blocked']:
        return 'memory_guard_'+result['state']
    return None


def optional_work_allowed():
    return not GUARD.sample()['optional_work_delayed']


def status():
    result = GUARD.sample()
    result['manual_entry_hold'] = hold_status() is not None
    result['monitor_running'] = bool(_worker and _worker.is_alive())
    return result


def start():
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        def watch():
            previous = None
            while True:
                result = GUARD.sample(force=True)
                if result['state'] != previous:
                    # Local audit only. Logging failure must not kill the guard.
                    try:
                        from . import audit
                        audit.log('memory_guard_transition', **result)
                    except Exception:
                        pass
                    previous = result['state']
                time.sleep(2)
        _worker = threading.Thread(target=watch,daemon=True,name='memory-guard')
        _worker.start()
