"""STOCK BLAST v1 (trader 2026-10-27 01:00 IST).

Why: 26-Oct ATHERENERG ran 1414 -> 1612 between 14:26 and 14:54. Move Capture fired a CE
trigger (score 89.5) at 14:26:37, at the start of the move, and every attempt was rejected
"move_capture_no_liquid_option". The 1400 CE went 10.85 -> 215. Since 1 Oct Move Capture
fired ~90,000 stock triggers and placed zero orders (COFORGE, PATANJALI, POLICYBZR the same).

Root causes fixed here, for STOCK fast-momentum entries only (indices unchanged):
  1. Index-tuned strike filters (spread <= 1.5%, delta 0.45-0.60, OI >= 100,000, distance
     <= 2%) applied to stock options, whose spreads are normally 3-10% and whose delta band
     holds one strike or none the day before expiry.
  2. A 15-minute holding horizon against a 15:15 exit blocked every entry after 15:00.
     Trader: "no time bar" -> entries until 15:10 (Angel's intraday entry window closes 15:15,
     auto square-off 15:20).
  3. Rejections never said which strike failed which rule (now logged, throttled).

Strike rules
  normal (score < BLAST_SCORE): delta 0.25-0.85, spread <= 6% (or <= Rs 0.50 and <= 12%),
                                within 4% of spot.
  blast  (score >= BLAST_SCORE): also cheap OTM strikes, premium Rs 2-30, delta >= 0.05,
                                spread <= 8% (or <= Rs 0.75 and <= 12%), within 8% of spot
                                (e.g. ATHER 1500 CE at ~Rs 10).
  Ranking: premium % gained per 1% spot move (|delta| x spot / ask), discounted by spread,
  liquidity as tie-break. The trader asked to maximise profit, so cheap high-leverage strikes
  rank first in blast mode.

Sizing (trader: "basing on the forecast confidence you can take a call"):
  allocation x SIZE_BY_SCORE (score < 75: 40%, 75-85: 70%, >= 85: 100%). The daily loss limit,
  capital guard and broker checks still apply.

Exit (trader chose the existing PEAK RUNNER): hard stop -35% (the trader's hard cap);
  +12% locks +3%; from +20% the scalp_runner 15-second peak forecast runs the position.
  scalp_runner mode off/shadow -> this module returns None and the old exits apply.

Config DATA_DIR/stock_blast.json {"mode": "live"|"off"}; off -> every function falls back to
the previous behaviour. Any error -> previous behaviour.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

from . import audit
from .config import DATA_DIR

MARK = 'STOCK BLAST v1'
BLAST_SCORE = 80.0
ENTRY_START = (9, 15)
ENTRY_END = (15, 10)
NORMAL = dict(min_delta=0.25, max_delta=0.85, max_spread_pct=6.0, abs_spread=0.50,
              hard_spread_pct=12.0, min_volume=1000, min_oi=5000, max_distance_pct=4.0)
BLAST = dict(NORMAL, min_delta=0.05, max_distance_pct=8.0, max_spread_pct=8.0, abs_spread=0.75,
             cheap_min=2.0, cheap_max=30.0)
# v1.1 (trader 2026-10-27 01:30): on very strong triggers also far strikes (27-Oct ATHER 1580/1600 CE
# went ~34x from the previous close). Sized at LOTTERY_SIZE of the normal allocation.
LOTTERY_SCORE = 88.0
LOTTERY = dict(BLAST, min_delta=0.005, max_distance_pct=15.0, cheap_min=0.5,
               hard_spread_pct=20.0)   # paise strikes: a 1-2 tick book is 10-20%
LOTTERY_SIZE = 0.2
SIZE_BY_SCORE = ((85.0, 1.0), (75.0, 0.7), (0.0, 0.4))
HARD_STOP_PCT = 0.35
LOCK_AT, LOCK_TO = 0.12, 0.03
STRIKES_EACH_SIDE = 4
LOTTERY_LADDER = 10
REJECT_LOG_SEC = 60.0

_cfg = {'at': 0.0, 'value': None}
_lock = threading.Lock()
_last_reject_log: dict = {}


def mode():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = 'live'
        try:
            with open(os.path.join(DATA_DIR, 'stock_blast.json')) as f:
                raw = json.load(f)
            if raw.get('mode') in ('live', 'off'):
                value = raw['mode']
        except (OSError, ValueError, AttributeError):
            pass
        _cfg.update(at=now, value=value)
    return _cfg['value']


def enabled():
    return mode() == 'live'


def _finite(*values):
    try:
        return all(math.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


def is_index(name):
    from . import config
    names = set(getattr(config, 'INDICES', {}) or {})
    names |= set(getattr(config, 'GAMMA_EXTRA_INDICES', ()) or ())
    return name in names


def applies(chain, fast):
    """True for a stock fast-momentum entry while the feature is on."""
    try:
        return bool(fast) and enabled() and not is_index(chain.index.name)
    except Exception:  # noqa: BLE001
        return False


def ladder_width(score):
    """Strikes each side to quote: wider on lottery-grade triggers so far strikes are seen."""
    s = float(score) if _finite(score) else 0.0
    return LOTTERY_LADDER if s >= LOTTERY_SCORE else STRIKES_EACH_SIDE


def entry_time_allowed(now):
    return now.weekday() < 5 and ENTRY_START <= (now.hour, now.minute) < ENTRY_END


def gates(score):
    s = float(score) if _finite(score) else 0.0
    return LOTTERY if s >= LOTTERY_SCORE else BLAST if s >= BLAST_SCORE else NORMAL


def is_lottery(q, spot):
    """A strike only the LOTTERY tier allows: > 8% from spot or premium under Rs 2."""
    try:
        ask = q.ask or q.ltp
        return (abs(q.strike - spot) / spot * 100.0 > BLAST['max_distance_pct']
                or ask < BLAST['cheap_min'])
    except Exception:  # noqa: BLE001
        return False


def spread_ok(bid, ask, g):
    """(ok, spread_pct). Missing book is a failure."""
    if not _finite(bid, ask) or bid <= 0 or ask <= 0 or ask < bid:
        return False, None
    mid = (bid + ask) / 2
    pct = (ask - bid) / mid * 100.0
    if pct <= g['max_spread_pct']:
        return True, pct
    return (ask - bid) <= g['abs_spread'] + 1e-9 and pct <= g['hard_spread_pct'], pct


def fail_reason(q, spot, g):
    """None if the quote passes the stock gates, else a short reason."""
    ltp, bid, ask = q.ltp, q.bid, q.ask
    if not _finite(ltp, spot, q.strike) or ltp <= 0 or spot <= 0:
        return 'no_valid_ltp'
    ok, pct = spread_ok(bid, ask, g)
    if not ok:
        return 'book_missing' if pct is None else f'spread_{pct:.1f}pct'
    if (q.volume or 0) < g['min_volume']:
        return f'volume_{q.volume}'
    if (q.oi or 0) < g['min_oi']:
        return f'oi_{q.oi}'
    dist = abs(q.strike - spot) / spot * 100.0
    if dist > g['max_distance_pct']:
        return f'distance_{dist:.1f}pct'
    if q.delta is None:
        return 'delta_unavailable'
    d = abs(q.delta)
    if d < g['min_delta'] or d > g['max_delta']:
        return f'delta_{d:.2f}'
    if g is not NORMAL and d < NORMAL['min_delta'] and not (g['cheap_min'] <= ask <= g['cheap_max']):
        return f'cheap_premium_{ask:.2f}_outside_band'
    return None


def leverage(q, spot):
    """Premium % gained per 1% spot move, discounted by spread."""
    d = abs(q.delta or 0)
    ask = q.ask or q.ltp
    if not _finite(d, ask, spot) or ask <= 0:
        return 0.0
    _, pct = spread_ok(q.bid, q.ask, BLAST)
    return d * spot / ask * max(0.2, 1 - (pct or 0) / 25.0)


def rank(chain, direction, spot, days_to_expiry, score=0.0):
    """Stock strike ranking; returns strike_selector.RankedStrike objects (rejected ones too)."""
    from .greeks import implied_vol_and_greeks
    from .strike_selector import RankedStrike
    g = gates(score)
    out = []
    for q in chain.quotes.values():
        if q.opt_type != direction:
            continue
        if q.delta is None and q.ltp and q.ltp > 0:
            try:
                iv, greeks = implied_vol_and_greeks(
                    spot=spot, strike=q.strike, premium=q.ltp,
                    days=days_to_expiry, opt_type=q.opt_type)
            except Exception:  # noqa: BLE001
                iv, greeks = None, None
            if greeks:
                q.iv = q.iv or iv
                q.delta, q.gamma = greeks['delta'], greeks['gamma']
                q.theta, q.vega = greeks['theta'], greeks['vega']
        reject = fail_reason(q, spot, g)
        if reject:
            out.append(RankedStrike(q, 0.0, [], reject))
            continue
        lev = leverage(q, spot)
        liq = math.log10(max(q.volume or 1, 1))
        tier = ('lottery' if g is LOTTERY and is_lottery(q, spot)
                else 'normal' if g is NORMAL else 'blast')
        # A lottery strike is sized at LOTTERY_SIZE, so rank it by its sized payoff.
        weight = LOTTERY_SIZE if tier == 'lottery' else 1.0
        out.append(RankedStrike(q, lev * 10 * weight + liq, [f'leverage {lev:.1f}', f'liq {liq:.1f}', tier]))
    out.sort(key=lambda r: (r.rejected is None, r.score), reverse=True)
    return out


def log_rejects(name, direction, score, ranked):
    """Per-strike rejection reasons, at most once a minute per underlying."""
    try:
        now = time.monotonic()
        with _lock:
            if now - _last_reject_log.get(name, -1e9) < REJECT_LOG_SEC:
                return
            _last_reject_log[name] = now
        audit.log('STOCK_BLAST_STRIKE_REJECTS', index=name, direction=direction, score=score,
                  mode={id(NORMAL): 'normal', id(BLAST): 'blast', id(LOTTERY): 'lottery'}[id(gates(score))],
                  rejects=[(r.quote.strike, r.quote.ltp, r.rejected) for r in ranked][:12])
    except Exception:  # noqa: BLE001
        pass


def size_fraction(score):
    s = float(score) if _finite(score) else 0.0
    for floor, frac in SIZE_BY_SCORE:
        if s >= floor:
            return frac
    return SIZE_BY_SCORE[-1][1]


def allocation(base, score, fast=None):
    try:
        frac = size_fraction(score)
        if isinstance(fast, dict) and fast.get('lottery') is True:
            frac *= LOTTERY_SIZE
        out = float(base) * frac
        audit.log('STOCK_BLAST_SIZING', score=score, fraction=frac, base=round(float(base), 0),
                  allocation=round(out, 0))
        return out
    except Exception:  # noqa: BLE001
        return base


def risk_gates(score):
    """Gates in risk_budget's vocabulary for the chosen stock contract."""
    g = gates(score)
    return dict(max_spread_pct=g['max_spread_pct'], abs_spread=g['abs_spread'],
                hard_spread_pct=g['hard_spread_pct'], min_delta=g['min_delta'],
                min_volume=g['min_volume'], min_oi=g['min_oi'],
                max_distance_pct=g['max_distance_pct'])


def managed_stop(pos, ltp):
    """(structural, effective, active, reason) for an open stock fast-momentum position, else
    None (index positions, feature off, peak runner off). Never raises."""
    try:
        if not enabled() or getattr(pos, 'kind', None) != 'fast_momentum':
            return None
        if is_index(getattr(pos, 'index', None)):
            return None
        from . import scalp_runner
        if scalp_runner.config()['mode'] != 'live':
            return None
        sid, entry = getattr(pos, 'signal_id', None), getattr(pos, 'entry_price', None)
        if sid is None or not _finite(entry, ltp) or entry <= 0:
            return None
        peak = max(getattr(pos, 'peak_price', None) or entry, ltp)
        base = entry * (1 - HARD_STOP_PCT)
        stop = max(base, entry * (1 + LOCK_TO)) if peak >= entry * (1 + LOCK_AT) else base
        stop, reason, handled = scalp_runner.step(int(sid), entry, peak, ltp, stop)
        if reason:
            reason = 'stock_blast_' + reason
        elif ltp <= base:
            reason = 'stock_blast_hard_stop_35pct'
        return base, stop, stop > base, reason
    except Exception as exc:  # noqa: BLE001
        try:
            audit.log('stock_blast_stop_error', error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return None
