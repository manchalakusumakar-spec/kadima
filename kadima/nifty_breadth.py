"""NIFTY 50 weighted breadth, SHADOW ONLY: recorded beside the hourly forecast, never used by it.

Reads the snapshot kadima-universe-scanner already writes every ~60 s (ltp for every F&O stock,
which includes all NIFTY 50 names). No broker calls, no new process, no extra polling: the
forecaster thread calls observe() at most once a minute and stores one small row.

Per minute, against each stock's price at the start of the current forecast slot:
  breadth      = index weight moving up minus weight moving down, -1..+1
  move_pct     = weight-averaged % move of the constituents since slot start
  top10_vote   = same as breadth over the 10 heaviest names
Fails closed (records nothing) when the weights file is missing or older than WEIGHTS_MAX_AGE_DAYS,
the snapshot is stale, or fewer than MIN_FRESH constituents have a fresh price.

Weights: $KADIMA_DATA_DIR/nifty50_weights.json  {"as_of": "YYYY-MM-DD", "weights": {"HDFCBANK": 13.1, ...}}
Mode:    $KADIMA_DATA_DIR/nifty_breadth.json    {"mode": "off" | "shadow"}  (default shadow; anything
         else is treated as shadow, so this build can never change a forecast)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from . import audit
from .config import DATA_DIR, IST

SNAPSHOT_PATH = os.environ.get('KADIMA_UNIVERSE_SNAPSHOT',
                               os.path.expanduser('~/kadima-py/state/universe_snapshot.json'))
WEIGHTS_PATH = os.path.join(DATA_DIR, 'nifty50_weights.json')
MODE_PATH = os.path.join(DATA_DIR, 'nifty_breadth.json')
WEIGHTS_MAX_AGE_DAYS = 45
MIN_FRESH = 45                # of 50 constituents
FRESH_SEC = 180               # per-stock quote and whole-snapshot age limit
OBSERVE_EVERY_SEC = 60
TOP_N = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_forecast_breadth (
    day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL, ts TEXT NOT NULL,
    breadth REAL NOT NULL, move_pct REAL NOT NULL, top10_vote REAL NOT NULL,
    fresh INTEGER NOT NULL, weight_covered REAL NOT NULL, weights_as_of TEXT NOT NULL,
    PRIMARY KEY (day, index_name, slot, ts)
);
"""

_state = {'last': None, 'base_key': None, 'base': {}}


def _dt(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def mode():
    try:
        with open(MODE_PATH) as f:
            m = json.load(f).get('mode')
    except (OSError, ValueError, AttributeError):
        m = None
    return 'off' if m == 'off' else 'shadow'


def load_weights(now, path=None):
    """{name: weight} normalised to sum 1, or None if missing/stale/implausible."""
    try:
        with open(path or WEIGHTS_PATH) as f:
            data = json.load(f)
        as_of = datetime.fromisoformat(data['as_of']).date()
        raw = {str(k): float(v) for k, v in data['weights'].items() if float(v) > 0}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, None
    if not 45 <= len(raw) <= 55 or (now.date() - as_of).days > WEIGHTS_MAX_AGE_DAYS:
        return None, None
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}, as_of.isoformat()


def read_snapshot(path=None):
    try:
        with open(path or SNAPSHOT_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fresh_prices(snapshot, names, now):
    if not snapshot or 'ts' not in snapshot:
        return {}
    try:
        if not 0 <= (now - _dt(snapshot['ts'])).total_seconds() <= FRESH_SEC:
            return {}
    except (TypeError, ValueError):
        return {}
    out = {}
    for name in names:
        q = (snapshot.get('prices') or {}).get(name) or {}
        try:
            ltp, at = float(q['ltp']), _dt(q['received_at'])
        except (KeyError, TypeError, ValueError):
            continue
        if ltp > 0 and 0 <= (now - at).total_seconds() <= FRESH_SEC:
            out[name] = ltp
    return out


def compute(prices, base, weights):
    """Breadth of each constituent's move since its slot-start price. None if too few names."""
    names = [n for n in weights if n in prices and n in base and base[n] > 0]
    if len(names) < MIN_FRESH:
        return None
    covered = sum(weights[n] for n in names)
    moves = {n: prices[n] / base[n] - 1 for n in names}
    sign = lambda x: 1 if x > 0 else -1 if x < 0 else 0          # noqa: E731
    breadth = sum(weights[n] * sign(moves[n]) for n in names) / covered
    move = sum(weights[n] * moves[n] for n in names) / covered * 100
    top = sorted(names, key=lambda n: weights[n], reverse=True)[:TOP_N]
    tw = sum(weights[n] for n in top)
    vote = sum(weights[n] * sign(moves[n]) for n in top) / tw if tw else 0.0
    return dict(breadth=round(breadth, 4), move_pct=round(move, 4), top10_vote=round(vote, 4),
                fresh=len(names), weight_covered=round(covered, 4))


def observe(conn, name, slot_label, now, *, snapshot=None, weights_path=None):
    """Record one shadow row for the live slot (at most once a minute). Returns the row or None.
    Never raises into the forecaster and never changes a forecast."""
    try:
        if name != 'NIFTY' or slot_label is None or mode() == 'off':
            return None
        if _state['last'] and (now - _state['last']).total_seconds() < OBSERVE_EVERY_SEC:
            return None
        _state['last'] = now
        weights, as_of = load_weights(now, weights_path)
        if weights is None:
            return None
        prices = fresh_prices(snapshot if snapshot is not None else read_snapshot(), weights, now)
        key = (now.date().isoformat(), slot_label)
        if _state['base_key'] != key:
            if len(prices) < MIN_FRESH:
                return None            # wait for a complete baseline before tracking this slot
            _state.update(base_key=key, base=dict(prices))
            return None
        row = compute(prices, _state['base'], weights)
        if row is None:
            return None
        conn.executescript(SCHEMA)
        with conn:
            conn.execute('INSERT OR IGNORE INTO hourly_forecast_breadth VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (key[0], name, slot_label, now.replace(microsecond=0).isoformat(), row['breadth'],
                          row['move_pct'], row['top10_vote'], row['fresh'], row['weight_covered'], as_of))
        return row
    except Exception as exc:                            # noqa: BLE001 - shadow only, never fatal
        try:
            audit.log('nifty_breadth_error', error=str(exc)[:200])
        except Exception:                               # noqa: BLE001
            pass
        return None


def shadow_report(conn, days=10, horizon_min=15):
    """Does breadth at time t lead the NIFTY's verified move over the next `horizon_min`
    beyond what the original forecast already expected? Read-only evidence for a later decision."""
    rows = conn.execute("SELECT day, slot, ts, breadth FROM hourly_forecast_breadth WHERE index_name='NIFTY' "
                        "AND day >= date('now', ?) ORDER BY ts", (f'-{days} day',)).fetchall()
    hits = total = 0
    for day, slot, ts, breadth in rows:
        t = _dt(ts)
        if t.minute % 5 or abs(breadth) < 0.2:
            continue
        end = (t + timedelta(minutes=horizon_min)).isoformat()
        a = conn.execute('SELECT p.actual, p.forecast FROM hourly_forecast_points p WHERE p.day=? AND '
                         "p.index_name='NIFTY' AND p.ts=? AND p.actual_source IN ('closed_5m','closed_1m')",
                         (day, end)).fetchone()
        b = conn.execute('SELECT p.actual, p.forecast FROM hourly_forecast_points p WHERE p.day=? AND '
                         "p.index_name='NIFTY' AND p.ts=? AND p.actual_source IN ('closed_5m','closed_1m')",
                         (day, t.replace(second=0).isoformat())).fetchone()
        if not a or not b:
            continue
        surprise = (a[0] - b[0]) - (a[1] - b[1])          # actual move minus forecast move
        if surprise == 0:
            continue
        total += 1
        hits += (surprise > 0) == (breadth > 0)
    return dict(samples=total, hit_rate=round(hits / total, 3) if total else None,
                note='Breadth sign vs the forecast surprise over the next %d min; '
                     'needs >=200 samples and a hit rate clearly above 0.55 before any live use.' % horizon_min)
