"""Bounded observation of selected contracts after they leave the ATM ladder.

Records sampled LTP/bid/ask, not a claim of exchange day-high coverage or fills.
No order transport. The shared quote limiter remains authoritative.
"""
import math
import time

from . import audit, storage
from .utils import now_ist

MAX_CONTRACTS = 32
POLL_SECONDS = 10


class QuoteWatch:
    def __init__(self):
        self.last_poll = 0.0

    def connection(self):
        c = storage.conn()
        c.execute('''CREATE TABLE IF NOT EXISTS momentum_quote_watch (
            day TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT NOT NULL,
            underlying TEXT NOT NULL, strike REAL NOT NULL, opt_type TEXT NOT NULL,
            PRIMARY KEY(day,token))''')
        return c

    def pin(self, underlying, q):
        day = now_ist().date().isoformat()
        c = self.connection()
        with c:
            c.execute('DELETE FROM momentum_quote_watch WHERE day != ?', (day,))
            exists = c.execute('SELECT 1 FROM momentum_quote_watch WHERE day=? AND token=?',
                               (day, str(q.token))).fetchone()
            if exists:
                return True
            count = c.execute('SELECT count(*) FROM momentum_quote_watch WHERE day=?', (day,)).fetchone()[0]
            if count >= MAX_CONTRACTS:
                audit.log('MOMENTUM_QUOTE_WATCH_FULL', index=underlying, limit=MAX_CONTRACTS)
                return False
            c.execute('INSERT INTO momentum_quote_watch VALUES (?,?,?,?,?,?)',
                      (day, str(q.token), q.symbol, underlying, q.strike, q.opt_type))
        return True

    def cycle(self):
        clock = time.monotonic()
        if clock-self.last_poll < POLL_SECONDS:
            return
        self.last_poll = clock
        rows = self.connection().execute('SELECT * FROM momentum_quote_watch WHERE day=? LIMIT ?',
            (now_ist().date().isoformat(), MAX_CONTRACTS)).fetchall()
        if not rows:
            return
        from .full_universe_scanner import fetch_batch
        received = fetch_batch({('NFO', r['token']) for r in rows})
        for row in rows:
            quote = received.get(('NFO', row['token']))
            if quote is None:
                continue
            # Full-quote LTP is validated by fetch_batch; missing depth stays missing.
            depth = quote.get('depth') or {}
            def price(side):
                values = depth.get(side) or []
                value = values[0].get('price') if values else None
                return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
            storage.insert_ticks([(quote['_received_at'], row['token'], row['symbol'],
                float(quote['ltp']), quote.get('tradeVolume'), price('buy'), price('sell'))])
            storage.insert_oi_snapshot(row['underlying'], [dict(row, ltp=float(quote['ltp']),
                oi=quote.get('opnInterest'), volume=quote.get('tradeVolume'))])
