"""
rate_limiter.py — enforces Angel One's actual published SmartAPI rate
limits so auto_executor.py can never accidentally breach them.

Limits below are Angel One's official published per-second ceilings
(SmartAPI rate-limit table, cross-checked against the SEBI-mandated
order-throttling circular effective Aug 2025, which caps order APIs at
10/sec — tighter than SmartAPI's own older 20/sec table, so the lower
number governs). VERIFY these periodically; broker limits do change.

    Endpoint group                                  Per second
    place/modify/cancel order                       10   (SEBI-capped)
    getOrderBook / getPosition / getTradeBook        1    (each, separately)
    getLtpData / quote                               10
    login                                            1

The getOrderBook/getPosition/getTradeBook ceiling of 1/sec is the tight
one — it's why check_exits() in auto_executor.py reads live premium from
the already-subscribed WebSocket feed (push-based, no REST call, no rate
limit) rather than polling position status on every check. REST position
polling is reserved for periodic reconciliation, not the hot loop.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` calls in any rolling
    `period_sec` window. Thread-safe — engine.py runs multiple threads
    (feed, OI loop, scheduler) that may all need to place or check orders."""

    def __init__(self, max_calls: int, period_sec: float = 1.0):
        self.max_calls = max_calls
        self.period_sec = period_sec
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """Non-blocking check: True and records the call if under the
        limit, False (and does NOT record) if it would breach it."""
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > self.period_sec:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            return True

    def wait_and_allow(self, timeout_sec: float = 5.0) -> bool:
        """Blocking version: waits for a free slot, up to timeout_sec.
        Returns False if timeout is reached without getting a slot —
        caller must treat that as 'do not call the API', not retry blindly."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.allow():
                return True
            time.sleep(0.02)
        return False


# ---------------------------------------------------------------- shared instances
# One limiter per endpoint GROUP, not per endpoint — Angel One's own limits
# are per-endpoint, and grouping only same-limit endpoints together keeps
# this simple without being wrong (no two endpoints sharing a limiter here
# have different actual ceilings).
ORDER_WRITE = RateLimiter(max_calls=10, period_sec=1.0)   # place/modify/cancel
ORDER_STATUS = RateLimiter(max_calls=1, period_sec=1.0)   # getOrderBook/getPosition/getTradeBook
QUOTE = RateLimiter(max_calls=10, period_sec=1.0)         # getLtpData/quote
LOGIN = RateLimiter(max_calls=1, period_sec=1.0)

