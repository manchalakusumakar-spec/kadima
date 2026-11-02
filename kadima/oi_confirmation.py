"""Read-only, as-of option OI confirmation. No broker or storage mutations.

Strength is 100 * (abs(CE change) + abs(PE change)) / starting basket OI,
capped at 100, and zero unless the two legs have the required opposing signs.
Use matched contracts in complete ATM +/-3 batches, never daily oi_change.
"""
from datetime import datetime, time, timedelta
import math
from pathlib import Path
import sqlite3

from . import config


def _empty(now, reason):
    return dict(direction=None, strength=0.0, ce_oi_change=0, pe_oi_change=0,
                strikes=[], window_start=(now - timedelta(minutes=15)).isoformat(),
                samples=0, reason=reason)


def scalp_oi_block(direction, now, confirmation):
    """Strong opposing OI vetoes morning setups; other gates still apply."""
    if (time(9, 15) <= now.astimezone(config.IST).time().replace(tzinfo=None) < time(10, 30)
            and confirmation['strength'] >= 40
            and confirmation['direction'] in ('CE', 'PE')
            and confirmation['direction'] != direction):
        return 'oi_confirmation_opposes'
    return None


def confirmation_from_rows(index_name, now, rows, spot):
    """Pure calculation shared by the live observer and offline replay."""
    result = _empty(now, 'insufficient_samples')
    index = config.GAMMA_INDICES.get(index_name)
    if index is None or not rows:
        return result
    if spot is None or not math.isfinite(spot) or spot <= 0:
        result['reason'] = 'sample_spot_unavailable'
        return result
    start = now - timedelta(minutes=15)
    batches = {}
    for row in rows:
        at = datetime.fromisoformat(row['ts'])
        if at.tzinfo is not None and start <= at <= now:
            batches.setdefault(at, []).append(row)
    if not batches:
        return result
    latest_at = max(batches)
    if (now - latest_at).total_seconds() > 90:
        result['reason'] = 'stale_samples'
        return result
    step = index.strike_step
    atm = round(spot / step) * step
    strikes = [atm + offset * step for offset in range(-3, 4)]
    result['strikes'] = strikes
    expected = {(strike, side) for strike in strikes for side in ('CE', 'PE')}

    def basket(batch):
        out = {}
        for row in batch:
            key = (row['strike'], row['opt_type'])
            if key not in expected:
                continue
            oi = row['oi']
            if (oi is None or not isinstance(oi, (int, float))
                    or not math.isfinite(oi) or oi < 0 or key in out):
                return None
            out[key] = (str(row['token']), oi)
        return out if set(out) == expected else None

    latest = basket(batches[latest_at])
    if latest is None:
        result['reason'] = 'incomplete_basket'
        return result
    complete = []
    for at, batch in sorted(batches.items()):
        values = basket(batch)
        if values is not None and all(values[k][0] == latest[k][0] for k in expected):
            complete.append((at, values))
    result['samples'] = len(complete)
    if len(complete) < 2 or (latest_at - complete[0][0]).total_seconds() < 12 * 60:
        result['reason'] = 'incomplete_window'
        return result
    first = complete[0][1]
    ce = sum(latest[k][1] - first[k][1] for k in expected if k[1] == 'CE')
    pe = sum(latest[k][1] - first[k][1] for k in expected if k[1] == 'PE')
    total = sum(value[1] for value in first.values())
    direction = 'CE' if ce < 0 < pe else 'PE' if pe < 0 < ce else None
    result.update(direction=direction, ce_oi_change=ce, pe_oi_change=pe,
                  strength=round(min(100.0, 100 * (abs(ce) + abs(pe)) / total), 2)
                  if direction and total > 0 else 0.0,
                  sample_start=complete[0][0].isoformat(), reason=None)
    return result


def read_confirmation(connection, index_name, now):
    """Read one historical window through an already read-only connection."""
    now = now.astimezone(config.IST)
    start = now - timedelta(minutes=15)
    rows = [dict(r) for r in connection.execute(
        'SELECT ts, token, strike, opt_type, oi FROM oi_snapshots '
        'WHERE index_name=? AND ts>=? AND ts<=? ORDER BY ts, id',
        (index_name, start.isoformat(), now.isoformat()))]
    index = config.GAMMA_INDICES.get(index_name)
    if index is None or not rows:
        return _empty(now, 'insufficient_samples')
    latest = max(datetime.fromisoformat(row['ts']) for row in rows)
    # oi_snapshots has no spot field. Use an already completed, same-session
    # index candle, as authorised; never the current/future candle's close.
    cutoff = latest - timedelta(minutes=1)
    candle = connection.execute(
        'SELECT start_ts, close FROM candles WHERE token=? AND interval=1 '
        'AND closed=1 AND start_ts>=? AND start_ts<=? ORDER BY start_ts DESC LIMIT 1',
        (index.token, latest.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
         cutoff.isoformat())).fetchone()
    if candle is None:
        return _empty(now, 'sample_spot_unavailable')
    closed_at = datetime.fromisoformat(candle['start_ts']) + timedelta(minutes=1)
    if not 0 <= (latest - closed_at).total_seconds() <= 90:
        return _empty(now, 'sample_spot_stale')
    result = confirmation_from_rows(index_name, now, rows, candle['close'])
    result.update(spot_source='completed_index_1m_candle', spot=candle['close'],
                  spot_at=closed_at.isoformat())
    return result


def oi_confirmation(index_name, now) -> dict:
    """Public observer: all reads use mode=ro, including in the live engine."""
    if now.tzinfo is None:
        return _empty(now, 'timezone_required')
    try:
        uri = Path(config.DB_PATH).resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            return read_confirmation(connection, index_name, now)
        finally:
            connection.close()
    except sqlite3.Error:
        # An optional shadow observer must not interrupt established scoring.
        return _empty(now, 'storage_unavailable')
