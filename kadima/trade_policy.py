"""Shared entry and long-option protection policy. No broker/order calls.

The option stop is a model mapping of confirmed underlying structure, not a
guaranteed fill price. Cost reserves below are estimates, not broker tariffs.
"""
import math
from datetime import datetime, timedelta
from .greeks import bs_price, implied_vol_and_greeks

SHIELD_MIN_SCORE = 65.0
GAMMA_MIN_SCORE = 70.0

RISK_FRACTION = 0.15
ACTIVATION = 0.05
RETAIN_PROFIT = 0.50
ENTRY_BUFFER = 0.025
EXIT_BUFFER = 0.025
COST_RESERVE = 0.01  # round-trip reserve as a fraction of purchase premium


def positive(*values):
    try:
        return all(math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (ValueError, TypeError):
        return False


def eligible(kind, tier, score, direction, snap, spot):
    threshold = {'bull_shield': SHIELD_MIN_SCORE, 'bear_shield': SHIELD_MIN_SCORE,
                 'gamma_blast': GAMMA_MIN_SCORE}.get(kind)
    if (threshold is None or tier != 'CONFIRMED' or not positive(score)
            or not threshold <= score <= 100):
        return False
    if direction not in ('CE', 'PE') or snap is None:
        return False
    values = [spot] + [getattr(snap, k, None) for k in ('vwap', 'ema_fast', 'ema_slow', 'rsi')]
    if not positive(*values) or snap.rsi > 100:
        return False
    return ((spot > snap.vwap and snap.ema_fast > snap.ema_slow and snap.rsi > 50)
            if direction == 'CE' else
            (spot < snap.vwap and snap.ema_fast < snap.ema_slow and snap.rsi < 50))


def swing(rows, direction, spot, now):
    """Latest unbroken 2-left/2-right confirmed pivot in fresh closed 5m bars.

    No future bars, previous-day pivots, missing bars or intra-bar guesses.
    start_ts denotes bar OPEN; five minutes are added for its close.
    """
    if direction not in ('CE', 'PE') or not positive(spot):
        return None
    try:
        bars = [(datetime.fromisoformat(r['start_ts']), float(r['low']), float(r['high']))
                for r in rows]
        bars = [b for b in bars if b[0].date() == now.date()]
        if len(bars) < 5 or any(t.tzinfo is None or not positive(lo, hi) or lo > hi
                               for t, lo, hi in bars):
            return None
        age = (now - (bars[-1][0] + timedelta(minutes=5))).total_seconds()
        if not 0 <= age <= 360:
            return None
        if any((b[0] - a[0]).total_seconds() != 300 for a, b in zip(bars, bars[1:])):
            return None
        idx = 1 if direction == 'CE' else 2
        for i in range(len(bars) - 3, 1, -1):
            level = bars[i][idx]
            neighbours = [bars[j][idx] for j in (i-2, i-1, i+1, i+2)]
            later = [b[idx] for b in bars[i+1:]] + [spot]
            valid = (level < min(neighbours + later) if direction == 'CE'
                     else level > max(neighbours + later))
            if valid:
                return level
    except (ValueError, TypeError, KeyError, OverflowError):
        pass
    return None


def premium_stop(spot, level, strike, premium, days, direction):
    if direction not in ('CE', 'PE') or not positive(spot, level, strike, premium, days):
        return None
    if not (level < spot if direction == 'CE' else level > spot):
        return None
    iv, _ = implied_vol_and_greeks(spot, strike, premium, days, direction)
    if not positive(iv):
        return None
    current = bs_price(spot, strike, days, iv / 100, direction)
    at_stop = bs_price(level, strike, days, iv / 100, direction)
    if not positive(current, at_stop) or abs(current - premium) > max(0.01, premium * 0.001):
        return None  # IV solver did not converge to a usable quote
    stop = premium + at_stop - current
    return stop if 0 < stop < premium else None


def risk_per_unit(entry_limit, stop):
    if not positive(entry_limit, stop) or stop >= entry_limit:
        return None
    return entry_limit - stop * (1 - EXIT_BUFFER) + entry_limit * COST_RESERVE


def lots_within_allocation(allocation, entry_limit, stop, lot_size, maximum):
    risk = risk_per_unit(entry_limit, stop)
    if not positive(allocation, lot_size, maximum) or risk is None:
        return 0
    return max(0, min(int(maximum), int(allocation * RISK_FRACTION // (risk * lot_size)),
                      int(allocation // (entry_limit * (1 + COST_RESERVE) * lot_size))))


def ratchet(entry, peak, previous, structural):
    """Both CE and PE are long premiums: their protection prices only rise."""
    if not positive(entry, peak, structural):
        raise ValueError('Missing position protection')
    active = peak >= entry * (1 + ACTIVATION)
    trail = entry + (peak - entry) * RETAIN_PROFIT if active else structural
    return max(previous or 0, structural, trail), active

