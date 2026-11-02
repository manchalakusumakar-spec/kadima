"""Daily loss limit tied to the morning's cash (trader decision 2026-10-25).

limit = min(cap, pct% of the first broker cash reading of the day), floored at FLOOR.
cap defaults to config.DAILY_LOSS_LIMIT_INR (10,000) and pct to 30, so at ~22k cash
the limit is ~6,600 (one gamma stop ends the day) and at 60k it stays at 10,000.

Safety: the morning cash is read once (retried at most every 60 s until it succeeds)
and persisted in DATA_DIR/daily_cash.json, so a restart mid-day keeps the morning value
instead of re-reading a cash balance already reduced by losses. Any unreadable,
non-positive or failed reading falls back to the fixed cap (the old behaviour); the
result is never below FLOOR, so a bad reading can never make a tiny limit that
force-flattens positions.

Config: DATA_DIR/daily_limit.json {"mode": "live|off", "pct": 30, "cap": 10000}
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

from .config import DAILY_LOSS_LIMIT_INR, DATA_DIR
from .utils import now_ist

FLOOR = 2000.0
DEFAULTS = {'mode': 'live', 'pct': 30.0, 'cap': float(DAILY_LOSS_LIMIT_INR)}
_cfg = {'at': 0.0, 'value': None}
_state = {'day': None, 'cash': None, 'tried': 0.0}
_lock = threading.Lock()


def _ok(v, lo=0.0):
    try:
        return math.isfinite(float(v)) and float(v) > lo
    except (TypeError, ValueError):
        return False


def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'daily_limit.json')) as f:
                raw = json.load(f)
            if raw.get('mode') in ('live', 'off'):
                value['mode'] = raw['mode']
            if _ok(raw.get('pct')) and float(raw['pct']) <= 100:
                value['pct'] = float(raw['pct'])
            if _ok(raw.get('cap'), FLOOR - 1):
                value['cap'] = float(raw['cap'])
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        _cfg.update(at=now, value=value)
    return _cfg['value']


def _path():
    return os.path.join(DATA_DIR, 'daily_cash.json')


def morning_cash(now=None):
    """First successful broker cash reading today (persisted), or None."""
    now = now or now_ist()
    day = now.date().isoformat()
    with _lock:
        if _state['day'] == day and _state['cash'] is not None:
            return _state['cash']
        if _state['day'] != day:
            _state.update(day=day, cash=None, tried=0.0)
            try:
                with open(_path()) as f:
                    saved = json.load(f)
                if saved.get('day') == day and _ok(saved.get('cash')):
                    _state['cash'] = float(saved['cash'])
                    return _state['cash']
            except (OSError, ValueError, AttributeError):
                pass
        if (now.hour, now.minute) < (9, 0) or time.monotonic() - _state['tried'] < 60:
            return None
        _state['tried'] = time.monotonic()
    try:
        from . import capital_guard
        cash = capital_guard.broker_available_funds()
    except Exception:  # noqa: BLE001
        cash = None
    if not _ok(cash):
        return None
    with _lock:
        if _state['day'] == day and _state['cash'] is None:
            _state['cash'] = float(cash)
            try:
                tmp = _path() + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump({'day': day, 'cash': float(cash), 'at': now.isoformat()}, f)
                os.replace(tmp, _path())
            except OSError:
                pass
            try:
                from . import audit
                c = config()
                audit.log('daily_limit_set', day=day, morning_cash=float(cash), pct=c['pct'],
                          cap=c['cap'], limit=_compute(float(cash), c))
            except Exception:  # noqa: BLE001
                pass
        return _state['cash']


def _compute(cash, c):
    return max(FLOOR, min(c['cap'], cash * c['pct'] / 100.0))


def limit(now=None):
    """Today's daily loss limit in rupees (positive number)."""
    try:
        c = config()
        if c['mode'] != 'live':
            return float(DAILY_LOSS_LIMIT_INR)
        cash = morning_cash(now)
        if not _ok(cash):
            return max(FLOOR, c['cap'])
        return _compute(float(cash), c)
    except Exception:  # noqa: BLE001 - never break the loss check
        return float(DAILY_LOSS_LIMIT_INR)
