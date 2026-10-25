"""Gamma Blast CONFIRMED rule (trader decision 2026-10-25).

A Celestial gamma strike is CONFIRMED only when ALL of these hold:
  1. final score >= the Celestial score threshold (currently 50)
  2. strike concentration (HHI of chain OI) >= MIN_HHI (0.09)
  3. time 09:45 <= now < 10:00 IST
Only CONFIRMED strikes may auto-trade (manual Celestial buys are not touched).
21-23 Oct replay: 18 such suggestions, 14 T1 (+50%) / 0 SL / 4 no result.

Config: DATA_DIR/gamma_confirm.json, re-read every 30 s:
  {"mode": "off|shadow|live", "min_hhi": 0.09, "window_start": "09:45", "window_end": "10:00"}
  off    - old tiers, no gate
  shadow - CONFIRMED tier shown and audited, auto entries not blocked
  live   - tier shown and auto entries blocked unless CONFIRMED (fails closed on error)
"""
from __future__ import annotations

import json
import math
import os
import time

from .config import DATA_DIR

DEFAULTS = {'mode': 'live', 'min_hhi': 0.09, 'window_start': '09:45', 'window_end': '10:00'}
_cfg = {'at': 0.0, 'value': None}
_logged: dict = {}


def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'gamma_confirm.json')) as f:
                raw = json.load(f)
            if raw.get('mode') in ('off', 'shadow', 'live'):
                value['mode'] = raw['mode']
            v = raw.get('min_hhi')
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1:
                value['min_hhi'] = float(v)
            for k in ('window_start', 'window_end'):
                if isinstance(raw.get(k), str) and _hm(raw[k]) is not None:
                    value[k] = raw[k]
        except (OSError, ValueError, AttributeError):
            pass
        _cfg.update(at=now, value=value)
    return _cfg['value']


def mode():
    return config()['mode']


def _hm(text):
    try:
        h, m = text.split(':')
        h, m = int(h), int(m)
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None
    except (AttributeError, ValueError):
        return None


def reasons(row, now, threshold):
    """[] when the Celestial row is CONFIRMED, else why not (in rule order)."""
    cfg = config()
    out = []
    try:
        score = float(row.get('final_score'))
        if not (math.isfinite(score) and score >= float(threshold)):
            out.append('gamma_not_confirmed_score')
    except (TypeError, ValueError):
        out.append('gamma_not_confirmed_score')
    try:
        hhi = float(row.get('strike_concentration_hhi'))
        if not (math.isfinite(hhi) and hhi >= cfg['min_hhi']):
            out.append('gamma_not_confirmed_concentration')
    except (TypeError, ValueError):
        out.append('gamma_not_confirmed_concentration')
    if not _hm(cfg['window_start']) <= (now.hour, now.minute) < _hm(cfg['window_end']):
        out.append('gamma_not_confirmed_window')
    return out


def tier(row, now, threshold, old_tier):
    """Display tier. off: the old score tier. shadow/live: CONFIRMED only when the rule
    passes; a score-only BLAST/CONFIRMED becomes ACTIVE."""
    if mode() == 'off':
        return old_tier
    try:
        if not reasons(row, now, threshold):
            return 'CONFIRMED'
    except Exception:  # noqa: BLE001 - display only
        pass
    return 'ACTIVE' if old_tier in ('BLAST', 'CONFIRMED') else old_tier


def entry_block(selected, now, threshold):
    """Auto-entry gate. Returns a block reason or None. Live errors fail closed."""
    m = mode()
    if m == 'off':
        return None
    try:
        why = reasons(selected or {}, now, threshold) if selected else ['gamma_not_confirmed_no_candidate']
    except Exception:  # noqa: BLE001
        why = ['gamma_confirm_error']
    try:
        key = ((selected or {}).get('symbol'), now.date(), tuple(why))
        if _logged.get(key[:2]) != key[2]:           # one line per contract per decision change
            _logged[key[:2]] = key[2]
            if len(_logged) > 2000:
                _logged.clear()
            from . import audit
            audit.log('gamma_confirm_gate', mode=m, symbol=(selected or {}).get('symbol'),
                      score=(selected or {}).get('final_score'),
                      hhi=(selected or {}).get('strike_concentration_hhi'), block=why or None)
    except Exception:  # noqa: BLE001
        pass
    return why[0] if (why and m == 'live') else None
