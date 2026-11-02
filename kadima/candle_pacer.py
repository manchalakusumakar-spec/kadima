"""Shared pacing for Angel One historical candles (getCandleData), 2026-10-27.

Three modules (stock_fast, stock_execution, gap_live) called getCandleData with no shared limit.
Angel's own log on 26-Oct shows "getCandleData ... Too many requests"; the older stock lane failed
its history step ~830 times that day. All historical calls now pass through one process-wide
pacer: at most one call every MIN_GAP_SEC and at most PER_MINUTE calls in any rolling minute,
with one retry after a rate-limit reply. Order placement and quotes are not affected.
"""
from __future__ import annotations

import collections
import threading
import time

MIN_GAP_SEC = 0.4          # <= 2.5 calls/second (Angel allows 3/second)
PER_MINUTE = 150           # conservative rolling-minute ceiling
RETRY_WAIT_SEC = 1.5
_lock = threading.Lock()
_calls: collections.deque = collections.deque()
_stats = {'calls': 0, 'waits': 0, 'rate_limited': 0, 'retried_ok': 0}


def _reserve():
    """Block until a slot is free, then record it. Returns seconds waited."""
    waited = 0.0
    while True:
        with _lock:
            now = time.monotonic()
            while _calls and now - _calls[0] > 60:
                _calls.popleft()
            gap = (_calls[-1] + MIN_GAP_SEC - now) if _calls else 0.0
            full = (_calls[0] + 60 - now) if len(_calls) >= PER_MINUTE else 0.0
            wait = max(gap, full, 0.0)
            if wait <= 0:
                _calls.append(now)
                _stats['calls'] += 1
                if waited:
                    _stats['waits'] += 1
                return waited
        time.sleep(min(wait, 2.0))
        waited += min(wait, 2.0)


def _rate_limited(value):
    text = str(value).lower()
    return 'too many requests' in text or 'exceed' in text or 'access rate' in text


def get(api, params):
    """api.getCandleData(params) through the shared pacer, one retry on a rate-limit reply."""
    for attempt in (0, 1):
        _reserve()
        try:
            response = api.getCandleData(params)
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a rate limit
            if attempt == 0 and _rate_limited(exc):
                _stats['rate_limited'] += 1
                time.sleep(RETRY_WAIT_SEC)
                continue
            raise
        if (attempt == 0 and isinstance(response, dict) and response.get('status') is not True
                and _rate_limited(response.get('message', ''))):
            _stats['rate_limited'] += 1
            time.sleep(RETRY_WAIT_SEC)
            continue
        if attempt == 1:
            _stats['retried_ok'] += 1
        return response
    return response


def stats():
    with _lock:
        return dict(_stats, in_last_minute=len(_calls))
