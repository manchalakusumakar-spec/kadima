"""Bounded host-memory admission guard; never stops feeds or position exits.

No import-time threads, broker requests, service controls or host writes.
Adapted from codex/fetch-universe-mcx-check with a 15% entry threshold.
"""
import threading
import time
from datetime import datetime, timezone

WARNING_PERCENT = 20
ENTRY_PERCENT = 15
CRITICAL_PERCENT = 10
RECOVERY_PERCENT = 25
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
            if len(fields) != 2 or fields[1] != 'kB' or not fields[0].isdigit():
                raise ValueError('invalid_memory_metric')
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
        self.recovery_at = self.last_at = self.cached = None
        self.minimum_available = None

    def sample(self, force=False):
        with self.lock:
            now = self.clock()
            if not force and self.cached and 0 <= now-self.last_at < 1:
                return dict(self.cached)
            if self.last_at is not None and not 0 <= now-self.last_at <= 10:
                self.state, self.recovery_at = 'starting', None
            self.last_at = now
            total = available = pct = None
            try:
                total, available = self.reader()
                if not 0 <= available <= total or total <= 0:
                    raise ValueError('invalid_memory_range')
                pct = 100*available/total
                self.minimum_available = min(available, self.minimum_available
                    if self.minimum_available is not None else available)
                if pct < ENTRY_PERCENT:
                    self.state = 'critical' if pct < CRITICAL_PERCENT else 'restricted'
                    self.recovery_at = None
                elif self.state in ('starting', 'critical', 'restricted', 'unavailable', 'recovering'):
                    self.state = 'recovering'
                    if pct >= RECOVERY_PERCENT:
                        if self.recovery_at is None:
                            self.recovery_at = now
                        if now-self.recovery_at >= RECOVERY_SECONDS:
                            self.state, self.recovery_at = 'normal', None
                    else:
                        self.recovery_at = None
                elif pct < WARNING_PERCENT:
                    self.state, self.recovery_at = 'warning', None
                elif self.state == 'warning':
                    if pct >= RECOVERY_PERCENT:
                        if self.recovery_at is None:
                            self.recovery_at = now
                        if now-self.recovery_at >= RECOVERY_SECONDS:
                            self.state, self.recovery_at = 'normal', None
                    else:
                        self.recovery_at = None
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                self.state, self.recovery_at = 'unavailable', None
                total = available = pct = None
            self.cached = dict(ts=datetime.now(timezone.utc).isoformat(), state=self.state,
                available_percent=round(pct, 1) if pct is not None else None,
                available_bytes=available, total_bytes=total,
                minimum_available_bytes=self.minimum_available,
                entries_blocked=self.state not in ('normal', 'warning'),
                optional_work_delayed=self.state != 'normal',
                recovery_seconds=max(0, round(RECOVERY_SECONDS-(now-self.recovery_at)))
                    if self.recovery_at is not None else None,
                protected_work=['exits', 'reconciliation', 'feeds', 'gamma_cas_detection'])
            return dict(self.cached)


GUARD = MemoryGuard()
_worker = None
_worker_lock = threading.Lock()


def entry_block_reason():
    result = GUARD.sample(force=True)
    return 'memory_guard_'+result['state'] if result['entries_blocked'] else None


def optional_work_allowed():
    return not GUARD.sample()['optional_work_delayed']


def status():
    return dict(GUARD.sample(), monitor_running=bool(_worker and _worker.is_alive()))


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
                    try:
                        from . import audit
                        audit.log('memory_guard_transition', **result)
                    except Exception:
                        pass
                    previous = result['state']
                time.sleep(2)
        _worker = threading.Thread(target=watch, daemon=True, name='memory-guard')
        _worker.start()
