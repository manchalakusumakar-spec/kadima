"""FOCUS LIST v1 (trader 2026-10-27 08:36): special attention for named stocks (e.g. results day).

File DATA_DIR/focus_symbols.json:
  {"day": "YYYY-MM-DD" or "", "symbols": [...], "trigger_score": 60, "max_trades": 5}
"day" set -> the list applies only on that day; "" -> every day until edited.
Read with a 30-second cache, so names can be added or removed intraday without a restart.
Any error or a missing file -> empty list (previous behaviour everywhere).

Used by:
  move_capture.py            focus names never evicted from the hot list; trigger at trigger_score
  move_capture_execution.py  focus triggers prepared first
  stock_blast.py             per-stock daily trade cap raised to max_trades for focus names
  stock_universe_scanner.py  focus names always on the gamma shortlist (cap raised by their count)
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR

IST = timezone(timedelta(hours=5, minutes=30))
MAX_SYMBOLS = 25
DEFAULTS = dict(trigger_score=60.0, max_trades=5)
_cache = {'at': -1e9, 'value': None}
_lock = threading.Lock()


def _load():
    try:
        with open(os.path.join(DATA_DIR, 'focus_symbols.json')) as f:
            raw = json.load(f)
        day = raw.get('day', '')
        if day and day != datetime.now(IST).strftime('%Y-%m-%d'):
            return dict(DEFAULTS, symbols=frozenset())
        syms = [str(s).strip().upper() for s in raw.get('symbols', []) if str(s).strip()][:MAX_SYMBOLS]
        out = dict(DEFAULTS, symbols=frozenset(syms))
        ts = raw.get('trigger_score')
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and 30 <= ts <= 100:
            out['trigger_score'] = float(ts)
        mt = raw.get('max_trades')
        if isinstance(mt, int) and not isinstance(mt, bool) and 1 <= mt <= 20:
            out['max_trades'] = mt
        return out
    except (OSError, ValueError, AttributeError, TypeError):
        return dict(DEFAULTS, symbols=frozenset())


def config():
    now = time.monotonic()
    with _lock:
        if _cache['value'] is None or now - _cache['at'] > 30:
            _cache.update(at=now, value=_load())
        return _cache['value']


def symbols():
    return config()['symbols']


def is_focus(name):
    try:
        return bool(name) and str(name).upper() in config()['symbols']
    except Exception:  # noqa: BLE001
        return False


def trigger_score(default):
    try:
        return min(float(default), config()['trigger_score'])
    except Exception:  # noqa: BLE001
        return default


def max_trades(default):
    try:
        return max(int(default), config()['max_trades'])
    except Exception:  # noqa: BLE001
        return default
