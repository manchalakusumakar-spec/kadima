"""PULSE LINE LOG v1 (trader 2026-11-04 00:39 IST: "deploy"). Display-only logger.

Once a minute per index, stores what the PULSE live forecast line said the index would be at
+5 / +15 / +30 / +60 minutes, with the spot and revision at that moment, so the daily after-market
PULSE audit can score the line against what price actually did (until now only the last snapshot
was kept). Never changes the forecast, an order or a stop; any error is logged once and skipped.
Table forecast.db pulse_line_log; ~375 rows per index per day.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

MARK = 'PULSE LINE LOG v1'
EVERY_SEC = 60.0
HORIZONS = (5, 15, 30, 60)
SCHEMA = """
CREATE TABLE IF NOT EXISTS pulse_line_log (
    day TEXT NOT NULL, index_name TEXT NOT NULL, ts TEXT NOT NULL, spot REAL,
    rev INTEGER, mode TEXT, f5 REAL, f15 REAL, f30 REAL, f60 REAL, line_end TEXT,
    PRIMARY KEY (day, index_name, ts)
)
"""
_last: dict = {}
_ready: set = set()
_err = {'logged': False}


def value_at(line, t):
    """Interpolated line value at t; None if t is outside the line (no clamping)."""
    pts = []
    for p in line or []:
        try:
            pts.append((datetime.fromisoformat(p['ts']), float(p['v'])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(pts) < 2 or t < pts[0][0] or t > pts[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds() or 1.0
            return round(v0 + (v1 - v0) * (t - t0).total_seconds() / span, 2)
    return None


def row(name, snap, now):
    pulse = (snap or {}).get('pulse') or {}
    line = pulse.get('line') or []
    if not line:
        return None
    vals = [value_at(line, now + timedelta(minutes=h)) for h in HORIZONS]
    return (now.date().isoformat(), name, now.replace(microsecond=0).isoformat(), snap.get('spot'),
            pulse.get('rev'), pulse.get('mode'), *vals, line[-1].get('ts'))


def record(name, snap, db, now):
    """Called from live_forecast's tick loop after the snapshot is built. Never raises."""
    try:
        t = time.monotonic()
        if t - _last.get(name, -1e9) < EVERY_SEC:
            return False
        r = row(name, snap, now)
        if r is None:
            return False
        _last[name] = t
        from . import hourly_forecast as HF
        c = HF.conn(db)
        with c:
            if db not in _ready:
                c.execute(SCHEMA)
                _ready.add(db)
            c.execute('INSERT OR REPLACE INTO pulse_line_log VALUES (?,?,?,?,?,?,?,?,?,?,?)', r)
        return True
    except Exception as exc:  # noqa: BLE001 - display only
        if not _err['logged']:
            _err['logged'] = True
            try:
                from . import audit
                audit.log('pulse_line_log_error', error=str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
        return False
