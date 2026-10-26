"""Gamma Blast CONFIRMED rule (trader decision 2026-10-25, v2 2026-10-25 night).

A Celestial gamma strike is CONFIRMED only when ALL of these hold:
  1. final score >= the Celestial score threshold (currently 50)
  2. the index direction (Bull Shield) agrees with the option side:
     PE needs a bearish shield reading, CE a bullish one (row bear/bull_shield_score)
  3. time 09:45 <= now < 10:10 IST
  4. strike concentration (HHI) >= min_hhi - v3 default 0.09 (v2 had 0 = not checked)
Only CONFIRMED strikes may auto-trade (manual Celestial buys are not touched).
v2 replay on minute prices with live exits, 21-23 Oct: 9 trades, 9 wins, avg +27%
(v1 HHI rule: 5 trades, 4 wins, avg +23%). HHI did not separate winners from losers.
v3 (trader 2026-10-26): HHI >= 0.09 back on top of the direction check. Audit 21-26 Oct,
09:45-10:10, score >= 50, fixed T1/SL: HHI >= 0.09 17 of 17 hit T1; below 0.09 14 of 27.

Config: DATA_DIR/gamma_confirm.json, re-read every 30 s:
  {"mode": "off|shadow|live", "min_hhi": 0.09, "require_direction": true,
   "window_start": "09:45", "window_end": "10:10"}
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

DEFAULTS = {'mode': 'live', 'min_hhi': 0.09, 'require_direction': True,   # CONFIRM v3 2026-10-26
            'window_start': '09:45', 'window_end': '10:10',
            'risk_fraction': 0.30}   # trader 2026-10-25: auto gamma may risk up to 30% of cash
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
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1:
                value['min_hhi'] = float(v)
            if isinstance(raw.get('require_direction'), bool):
                value['require_direction'] = raw['require_direction']
            v = raw.get('risk_fraction')
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 0.5:
                value['risk_fraction'] = float(v)
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
    if cfg['require_direction'] and not direction_agrees(row):
        out.append('gamma_not_confirmed_direction')
    if cfg['min_hhi'] > 0:
        try:
            hhi = float(row.get('strike_concentration_hhi'))
            if not (math.isfinite(hhi) and hhi >= cfg['min_hhi']):
                out.append('gamma_not_confirmed_concentration')
        except (TypeError, ValueError):
            out.append('gamma_not_confirmed_concentration')
    if not _hm(cfg['window_start']) <= (now.hour, now.minute) < _hm(cfg['window_end']):
        out.append('gamma_not_confirmed_window')
    return out


def direction_agrees(row):
    """True when the index's Bull Shield direction matches the option side.
    The scanner fills bear_shield_score only for a PE shield reading and
    bull_shield_score only for a CE reading; a missing reading never agrees."""
    side = row.get('direction')
    key = 'bear_shield_score' if side == 'PE' else 'bull_shield_score' if side == 'CE' else None
    if key is None:
        return False
    try:
        return math.isfinite(float(row.get(key)))
    except (TypeError, ValueError):
        return False


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


def scaled_allocation(allocation, base_fraction):
    """Auto gamma sizing: risk_budget allows allocation x base_fraction (0.15) of risk.
    Scaling the allocation lets one trade risk risk_fraction (0.30) of it instead.
    The real-cash checks still apply afterwards: Sniper rule 7 (risk <= max_risk_cash_pct
    of broker cash) and capital_guard's headroom check on the 1-lot cost."""
    try:
        if mode() == 'off' or not allocation or not base_fraction:
            return allocation
        return float(allocation) * config()['risk_fraction'] / float(base_fraction)
    except Exception:  # noqa: BLE001
        return allocation


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


def is_confirmed(selected, now, threshold=None):
    """BOTTLENECK FIX v1 (2026-10-25): True only when live and the Celestial row passes the
    CONFIRMED rule. Any error or missing data -> False (no bypass)."""
    try:
        if mode() != 'live' or not selected:
            return False
        if threshold is None:
            from . import celestial
            threshold = celestial.read_config()['score_threshold']
        return not reasons(selected, now, threshold)
    except Exception:  # noqa: BLE001
        return False
