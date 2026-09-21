"""Local order/status limits and a shared conservative quote budget.

Broker reference: https://smartapi.angelone.in/docs (Rate Limit).
Quote calls also use the cross-process limiter attached to SESSION.api.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
import uuid
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
ORDER_WRITE = RateLimiter(max_calls=9, period_sec=1.0)   # place/modify/cancel
ORDER_STATUS = RateLimiter(max_calls=1, period_sec=1.0)   # getOrderBook/getPosition/getTradeBook
QUOTE = RateLimiter(max_calls=10, period_sec=1.0)         # getLtpData/quote
LOGIN = RateLimiter(max_calls=1, period_sec=1.0)




SHARED_QUOTE_WAIT_SEC = 12.0


class SharedQuoteLimiter:
    """Serialize quote requests across processes sharing KADIMA_DATA_DIR.

    Conservative spacing is intentional: this deployment has recorded repeated
    access-denied responses. This is a local budget, not a claim about the
    broker's maximum allowance. Calls on other hosts remain outside this budget.

    Tickets are FIFO across threads and processes. A busy scanner must rejoin
    the tail after each call instead of racing an OI poll for every free slot.
    Expiring tickets bound the wait when a worker exits while queued. The
    existing budget row remains authoritative, including during a rolling
    upgrade; fairness requires all quote clients to run this version.
    """
    def __init__(self, path=None, interval=1.1):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('quote interval must be finite and positive')
        self.path = path
        self.interval = interval

    def wait_and_allow(self, timeout_sec=SHARED_QUOTE_WAIT_SEC):
        if not math.isfinite(timeout_sec) or timeout_sec < 0:
            raise ValueError('quote timeout must be finite and nonnegative')
        if timeout_sec == 0:
            return False
        path = self.path
        if path is None:
            from .config import DATA_DIR
            path = os.path.join(DATA_DIR, 'quote_budget.sqlite3')
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        deadline = time.monotonic() + timeout_sec
        expires = time.time() + timeout_sec
        ticket = uuid.uuid4().hex
        queued = False
        try:
            while time.monotonic() < deadline:
                c = None
                try:
                    c = sqlite3.connect(path, timeout=min(.05, max(0, deadline-time.monotonic())))
                    c.execute('CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, next REAL)')
                    c.execute('CREATE TABLE IF NOT EXISTS quote_waiters ('
                              'seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                              'ticket TEXT NOT NULL UNIQUE, expires REAL NOT NULL)')
                    c.execute('BEGIN IMMEDIATE')
                    now = time.time()
                    if time.monotonic() >= deadline or now >= expires:
                        return False
                    c.execute('DELETE FROM quote_waiters WHERE expires <= ?', (now,))
                    if not queued:
                        c.execute('INSERT INTO quote_waiters(ticket, expires) VALUES (?, ?)',
                                  (ticket, expires))
                    first = c.execute('SELECT ticket FROM quote_waiters ORDER BY seq LIMIT 1').fetchone()
                    row = c.execute('SELECT next FROM budget WHERE id=1').fetchone()
                    if first and first[0] == ticket and (row is None or now >= row[0]):
                        # Space actual grants, not future reservations: a delayed
                        # queue head must not release a burst of overdue slots.
                        c.execute('INSERT OR REPLACE INTO budget VALUES (1,?)', (now+self.interval,))
                        c.execute('DELETE FROM quote_waiters WHERE ticket=?', (ticket,))
                        c.commit()
                        queued = False
                        return True
                    c.commit()
                    queued = True
                except sqlite3.OperationalError:
                    # Busy/failed storage never grants permission to call the API.
                    pass
                finally:
                    if c is not None:
                        c.close()
                time.sleep(min(.05, max(0, deadline-time.monotonic())))
            return False
        finally:
            if queued:
                # Best effort; an unremovable or crashed ticket also expires.
                c = None
                try:
                    c = sqlite3.connect(path, timeout=.05)
                    c.execute('DELETE FROM quote_waiters WHERE ticket=?', (ticket,))
                    c.commit()
                except sqlite3.OperationalError:
                    pass
                finally:
                    if c is not None:
                        c.close()


SHARED_QUOTE = SharedQuoteLimiter()


def protect_quote_api(api):
    """Apply once to each SDK instance; all SESSION quote callers share budget."""
    method = api.getMarketData
    if getattr(method, '_kadima_budgeted', False):
        return
    def quote(*args, **kwargs):
        if not SHARED_QUOTE.wait_and_allow(timeout_sec=SHARED_QUOTE_WAIT_SEC):
            raise TimeoutError('KADIMA shared quote budget unavailable after bounded FIFO wait')
        return method(*args, **kwargs)
    quote._kadima_budgeted = True
    api.getMarketData = quote
