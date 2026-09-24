"""Offline five-minute swing/Fibonacci zone candidates, never execution authority.

Only information available at `now` is used. A two-right-bar pivot becomes
available at the second right bar's CLOSE, not at the historical pivot candle.
Custom numeric levels are deliberately not interpreted as prices or periods.
"""
from datetime import datetime, timedelta
import hashlib
import math

from .config import IST

FIB_RATIOS = (.236, .382, .5, .618, .786)  # .5 is a conventional midpoint.
CUSTOM_LEVEL_CANDIDATES = (18,33,44,55,66,77,88,103,108,122,133,135,155)
CUSTOM_LEVELS_ENABLED = False
BAR = timedelta(minutes=5)


def _stamp(value):
    t = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return t.astimezone(IST)


def _positive(value):
    return type(value) in (int,float) and math.isfinite(value) and value > 0


def closed_history(rows, now):
    """Require an ordered, contiguous current-session prefix of closed 5m bars.

    Forming/future bars are ignored, not used to confirm a past pivot. Historical
    replay must call with its historical as-of time, never today's timestamp.
    """
    now = _stamp(now)
    bars = []
    for row in rows:
        row = dict(row)  # Accept SQLite rows as well as normalized replay dicts.
        t = _stamp(row['start_ts'])
        if t.date() != now.date() or t + BAR > now or row.get('closed') != 1:
            continue
        if row.get('interval') != 5 or t.second or t.microsecond or t.minute % 5:
            raise ValueError('closed_5m_bars_required')
        values = [row[k] for k in ('open','high','low','close')]
        if not all(_positive(v) for v in values):
            raise ValueError('invalid_ohlc')
        o,h,l,c = values
        if not l <= min(o,c) <= max(o,c) <= h:
            raise ValueError('invalid_ohlc')
        if bars and t - bars[-1]['at'] != BAR:
            raise ValueError('unordered_duplicate_or_missing_bar')
        bars.append(dict(at=t, open=o, high=h, low=l, close=c))
    if len(bars) < 5 or not 0 <= (now - bars[-1]['at'] - BAR).total_seconds() <= 300:
        raise ValueError('insufficient_or_stale_history')
    return bars


def _pivots(bars):
    pivots = []
    for i in range(2, len(bars)-2):
        neighbours = [bars[j] for j in (i-2,i-1,i+1,i+2)]
        lo = all(bars[i]['low'] < b['low'] for b in neighbours)
        hi = all(bars[i]['high'] > b['high'] for b in neighbours)
        if lo == hi:  # Includes outside candles with ambiguous high/low order.
            continue
        role = 'support' if lo else 'resistance'
        pivots.append(dict(role=role, price=bars[i]['low' if lo else 'high'],
                           at=bars[i]['at'], available_at=bars[i+2]['at']+BAR))
    return pivots


def _fibs(pivots):
    anchors = []
    for p in pivots:
        if anchors and anchors[-1]['role'] == p['role']:
            more_extreme = (p['price'] < anchors[-1]['price'] if p['role'] == 'support'
                            else p['price'] > anchors[-1]['price'])
            if more_extreme:
                anchors[-1] = p
        else:
            anchors.append(p)
    levels = []
    # Fixed bounded lookback, not an optimization over whichever leg fits best.
    for a,b in list(zip(anchors, anchors[1:]))[-3:]:
        up = a['role'] == 'support'
        span = b['price']-a['price'] if up else a['price']-b['price']
        if span <= 0:
            continue
        for ratio in FIB_RATIOS:
            price = b['price']-ratio*span if up else b['price']+ratio*span
            levels.append(dict(price=price, ratio=ratio,
                role='support' if up else 'resistance',
                anchor_start=a['at'].isoformat(), anchor_end=b['at'].isoformat(),
                available_at=b['available_at'].isoformat()))
    return levels


def fibonacci_levels(rows, *, now):
    """Return unqualified mathematical levels, not a reversal probability."""
    return _fibs(_pivots(closed_history(rows, now)))


def build_zones(rows, *, asset, now, spot, atr, width_atr=.1,
                min_width_points=1., reaction_max_age_seconds=1800):
    """Return reacted-to, unbroken pivot zones, optionally with Fibonacci overlap.

    A Fib level cannot create a qualified zone by itself. A bounce must occur
    AFTER all contributing references were available. Consecutive touching
    bars count as one encounter. Closed-bar failure invalidates the zone.
    Output `ts` is computation time; `source_asof` is the actual last bar close.
    Caller must independently supply a fresh spot quote and matching forecast.
    """
    now = _stamp(now)
    if (not isinstance(asset,str) or not asset or not _positive(spot) or not _positive(atr)
            or not _positive(width_atr) or not _positive(min_width_points)
            or not _positive(reaction_max_age_seconds)):
        raise ValueError('invalid_zone_inputs')
    bars = closed_history(rows, now)
    pivots = _pivots(bars)
    fibs = _fibs(pivots)
    width = max(min_width_points, width_atr*atr)
    zones = []
    for pivot in pivots:
        support = pivot['role'] == 'support'
        lo,hi = pivot['price']-width, pivot['price']+width
        if lo <= 0 or (support and spot <= hi) or (not support and spot >= lo):
            continue
        overlap = [f for f in fibs if f['role'] == pivot['role'] and lo <= f['price'] <= hi]
        available = max([pivot['available_at']] + [_stamp(f['available_at']) for f in overlap])
        # A broken pivot cannot become valid again just because a later Fib fits.
        invalid = any((b['close'] < lo if support else b['close'] > hi)
                      for b in bars if b['at'] >= pivot['available_at'])
        if invalid:
            continue
        reactions, touching, reaction_at = 0, False, None
        for b in bars:
            if b['at'] < available:
                continue
            touches = b['low'] <= hi and b['high'] >= lo
            rejects = b['close'] > hi if support else b['close'] < lo
            if touches and rejects and not touching:
                reactions += 1
                reaction_at = b['at']+BAR
            touching = touches
        if not reactions or (now-reaction_at).total_seconds() > reaction_max_age_seconds:
            continue
        identity = '|'.join([asset,pivot['role'],pivot['at'].isoformat(),
                             available.isoformat(),repr(lo),repr(hi),repr(overlap)])
        zones.append(dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24],
            asset=asset, role=pivot['role'], low=lo, high=hi, ts=now.isoformat(),
            source_asof=(bars[-1]['at']+BAR).isoformat(), bar_seconds=300,
            available_at=available.isoformat(), reaction_at=reaction_at.isoformat(),
            confirmed_reactions=reactions, references=['pivot']+(['fibonacci'] if overlap else []),
            fibonacci=overlap, oi_valid=False, experimental=True))
    return sorted(zones, key=lambda z: abs(spot-(z['low']+z['high'])/2))
