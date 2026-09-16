
"""
Tick, candle, OI-snapshot and signal storage on SQLite (WAL mode).

SQLite is chosen over Postgres because a single EC2 box polling four indices
generates maybe 2-5M rows a day at full tick resolution. That is comfortably
within SQLite's range and removes a whole class of "the database went away"
failures from your uptime story. Move to TimescaleDB only if you later need
multi-day tick backtests.
"""
from __future__ import annotations

import os
from contextlib import nullcontext
import sqlite3
import threading
from datetime import datetime
from typing import Iterable

from .config import DB_PATH, DATA_DIR, IST

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS ticks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    token     TEXT NOT NULL,
    symbol    TEXT,
    ltp       REAL,
    volume    INTEGER,
    bid       REAL,
    ask       REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts ON ticks(token, ts);

CREATE TABLE IF NOT EXISTS candles (
    token     TEXT NOT NULL,
    interval  INTEGER NOT NULL,
    start_ts  TEXT NOT NULL,
    open      REAL, high REAL, low REAL, close REAL,
    volume    INTEGER,
    vwap      REAL,
    closed    INTEGER DEFAULT 0,
    PRIMARY KEY (token, interval, start_ts)
);

CREATE TABLE IF NOT EXISTS oi_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    index_name TEXT NOT NULL,
    token     TEXT NOT NULL,
    symbol    TEXT,
    strike    REAL,
    opt_type  TEXT,
    ltp       REAL,
    oi        INTEGER,
    oi_change INTEGER,
    volume    INTEGER,
    iv        REAL,
    delta     REAL, gamma REAL, theta REAL, vega REAL
);
CREATE INDEX IF NOT EXISTS idx_oi_idx_ts ON oi_snapshots(index_name, ts);

CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    index_name TEXT,
    tier      TEXT,
    score     REAL,
    direction TEXT,
    payload   TEXT,
    kind      TEXT DEFAULT 'bull_shield'
);

-- forecast vs actual: the table that tells you whether any of this works
CREATE TABLE IF NOT EXISTS outcomes (
    signal_id INTEGER PRIMARY KEY,
    reviewed_ts TEXT,
    acted     INTEGER,
    entry     REAL,
    exit      REAL,
    pnl       REAL,
    note      TEXT
);

-- auto_executor's live position book. This is a LOG/CACHE, not the source
-- of truth -- the broker's actual position book always wins on conflict.
-- See auto_executor.reconcile_on_startup(): a process restart with an
-- open position must reconcile against the broker before trusting this.
CREATE TABLE IF NOT EXISTS positions (
    signal_id     INTEGER PRIMARY KEY,
    kind          TEXT,
    index_name    TEXT,
    direction     TEXT,
    symbol        TEXT,
    token         TEXT,
    exchange      TEXT,
    entry_price   REAL,
    entry_ts      TEXT,
    lots          INTEGER,
    lot_size      INTEGER,
    remaining_qty INTEGER,
    order_id      TEXT,
    status        TEXT DEFAULT 'open',
    peak_price    REAL,
    trail_active  INTEGER DEFAULT 0,
    exit_price    REAL,
    exit_ts       TEXT,
    exit_reason   TEXT,
    exit_order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""


def _migrate(c: sqlite3.Connection) -> None:
    """Additive schema changes for databases created before this column
    existed. CREATE TABLE IF NOT EXISTS above only helps a fresh DB —
    the production database already has a `signals` table from before
    `kind` was added, so that column needs an explicit ALTER TABLE.
    Guarded so it's a no-op once the column exists."""
    position_cols = {row['name'] for row in c.execute('PRAGMA table_info(positions)')}
    for name in ('swing_level', 'structural_stop', 'effective_stop', 'allocated_capital'):
        if name not in position_cols:
            c.execute(f'ALTER TABLE positions ADD COLUMN {name} REAL')
    cols = {row["name"] for row in c.execute("PRAGMA table_info(signals)")}
    if "kind" not in cols:
        c.execute("ALTER TABLE signals ADD COLUMN kind TEXT DEFAULT 'bull_shield'")


def conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        _migrate(c)
        _local.conn = c
    return _local.conn


def insert_ticks(rows: Iterable[tuple]) -> None:
    c = conn()
    with c:
        c.executemany(
            "INSERT INTO ticks (ts, token, symbol, ltp, volume, bid, ask) "
            "VALUES (?,?,?,?,?,?,?)", rows)


def upsert_candle(token: str, interval: int, start_ts: str, o, h, l, cl,
                  vol, vwap, closed: bool) -> None:
    c = conn()
    with c:
        c.execute(
            "INSERT INTO candles (token, interval, start_ts, open, high, low, "
            "close, volume, vwap, closed) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(token, interval, start_ts) DO UPDATE SET "
            "high=excluded.high, low=excluded.low, close=excluded.close, "
            "volume=excluded.volume, vwap=excluded.vwap, closed=excluded.closed",
            (token, interval, start_ts, o, h, l, cl, vol, vwap, int(closed)))


def insert_oi_snapshot(index_name: str, rows: Iterable[dict]) -> None:
    ts = datetime.now(IST).isoformat()
    c = conn()
    with c:
        c.executemany(
            "INSERT INTO oi_snapshots (ts, index_name, token, symbol, strike, "
            "opt_type, ltp, oi, oi_change, volume, iv, delta, gamma, theta, vega) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, index_name, r.get("token"), r.get("symbol"), r.get("strike"),
              r.get("opt_type"), r.get("ltp"), r.get("oi"), r.get("oi_change"),
              r.get("volume"), r.get("iv"), r.get("delta"), r.get("gamma"),
              r.get("theta"), r.get("vega")) for r in rows])


def insert_signal(index_name: str, tier: str, score: float,
                  direction: str, payload: str, kind: str = "bull_shield") -> int:
    c = conn()
    with c:
        cur = c.execute(
            "INSERT INTO signals (ts, index_name, tier, score, direction, "
            "payload, kind) VALUES (?,?,?,?,?,?,?)",
            (datetime.now(IST).isoformat(), index_name, tier, score,
             direction, payload, kind))
        return cur.lastrowid


# ---------------------------------------------------------------- positions
def upsert_position(pos, _connection=None) -> None:
    """pos is an auto_executor.Position. Imported lazily by callers to
    avoid a circular import (auto_executor imports storage)."""
    c = _connection if _connection is not None else conn()
    with (c if _connection is None else nullcontext()):
        c.execute(
            "INSERT INTO positions (signal_id, kind, index_name, direction, "
            "symbol, token, exchange, entry_price, entry_ts, lots, lot_size, "
            "remaining_qty, order_id, status, peak_price, trail_active, "
            "exit_price, exit_ts, exit_reason, exit_order_id, "
            "swing_level, structural_stop, effective_stop, allocated_capital) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(signal_id) DO UPDATE SET "
            "entry_price=excluded.entry_price, entry_ts=excluded.entry_ts, "
            "remaining_qty=excluded.remaining_qty, lots=excluded.lots, "
            "status=excluded.status, peak_price=excluded.peak_price, "
            "trail_active=excluded.trail_active, exit_price=excluded.exit_price, "
            "exit_ts=excluded.exit_ts, exit_reason=excluded.exit_reason, "
            "exit_order_id=excluded.exit_order_id, order_id=excluded.order_id, "
            "swing_level=excluded.swing_level, structural_stop=excluded.structural_stop, "
            "effective_stop=excluded.effective_stop, allocated_capital=excluded.allocated_capital",
            (pos.signal_id, pos.kind, pos.index, pos.direction, pos.symbol,
             pos.token, pos.exchange, pos.entry_price,
             pos.entry_ts.isoformat() if pos.entry_ts else None,
             pos.lots, pos.lot_size, pos.remaining_qty, pos.order_id,
             pos.status, pos.peak_price, int(pos.trail_active),
             pos.exit_price,
             pos.exit_ts.isoformat() if pos.exit_ts else None,
             pos.exit_reason, pos.exit_order_id,
             getattr(pos, 'swing_level', None), getattr(pos, 'structural_stop', None),
             getattr(pos, 'effective_stop', None), getattr(pos, 'allocated_capital', None)))


def close_position(signal_id: int, exit_price: float | None, exit_ts: str,
                   exit_reason: str, exit_order_id: str | None) -> None:
    c = conn()
    with c:
        c.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_ts=?, "
            "exit_reason=?, exit_order_id=? WHERE signal_id=?",
            (exit_price, exit_ts, exit_reason, exit_order_id, signal_id))


def load_open_positions() -> list[dict]:
    """Rows shaped to match auto_executor.Position's field names directly,
    so callers can do Position(**row) after parsing the two timestamp
    strings back into datetimes. Returns [] on an empty table, not None —
    an empty result here is a normal, common state, unlike the broker-
    unreadable case in fetch_broker_positions(), which fails closed."""
    c = conn()
    rows = c.execute(
        "SELECT signal_id, kind, index_name AS 'index', direction, symbol, "
        "token, exchange, entry_price, entry_ts, lots, lot_size, "
        "remaining_qty, order_id, status, peak_price, trail_active, "
        "exit_price, exit_ts, exit_reason, exit_order_id, "
        "swing_level, structural_stop, effective_stop, allocated_capital "
        "FROM positions WHERE status IN ('open','entry_pending','exit_pending','order_unknown')"
    ).fetchall()
    return [dict(r) for r in rows]


def recent_candles(token: str, interval: int, limit: int = 200):
    c = conn()
    rows = c.execute(
        "SELECT * FROM candles WHERE token=? AND interval=? AND closed=1 "
        "ORDER BY start_ts DESC LIMIT ?", (token, interval, limit)).fetchall()
    return list(reversed(rows))



