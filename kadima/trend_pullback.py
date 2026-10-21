"""Early entry on a trend pullback (trader ask, 21-Oct-2026). Pure detector; no orders.

21-Oct, the PEs were bought late in each leg (BANKNIFTY 55100 PE at 473 after a run from
~300). This detector enters earlier and at lower risk: once the day trend is UP or DOWN
(trend_rules rule 1, the same label rule 2 gates on), it waits for the index to pull back
into the 38.2%-61.8% Fibonacci zone of the current leg and enters on the first completed
1-minute close that turns back with the trend. Thresholds are in 5-minute ATR of the index.

  leg      the move from the leg start (highest close for DOWN / lowest for UP since the
           trend was set or since the last failed leg) to its extreme; it must be at least
           LEG_MIN_ATR long
  zone     pullback retracement between ZONE_LO and ZONE_HI of the leg
  turn     a completed close TURN_ATR back from the pullback extreme, in the trend direction
  invalid  a retracement beyond ZONE_FAIL: the leg failed, a new leg starts from there
One entry per leg, at most MAX_PER_DAY per index, only between WINDOW times.
"""
from __future__ import annotations

import math

LEG_MIN_ATR = 1.5
ZONE_LO = 0.382
ZONE_HI = 0.618
ZONE_FAIL = 0.786
TURN_ATR = 0.3
MAX_PER_DAY = 3
WINDOW = ((9, 30), (14, 45))


def _ok(*values):
    try:
        return all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


def new_state():
    return dict(trend=None, start=None, ext=None, pull=None, zone=False, used_legs=[], entries=0)


def _reset(state, trend, close):
    state.update(trend=trend, start=close, ext=close, pull=None, zone=False)


def step(state, close, atr, trend, at=None, side_allowed=True):
    """Feed one completed 1-min index close. Returns None or a dict
    {'direction': 'CE'|'PE', 'retrace': r, 'leg': pts, 'stop_ref': pullback extreme}."""
    if trend not in ('UP', 'DOWN') or not _ok(close, atr):
        state['trend'] = None
        return None
    if state['trend'] != trend:
        _reset(state, trend, close)
        return None
    sign = 1 if trend == 'UP' else -1
    if (close - state['ext']) * sign > 0:          # new extreme: the leg extends
        state['ext'], state['pull'], state['zone'] = close, None, False
        return None
    leg = (state['ext'] - state['start']) * sign
    if leg < LEG_MIN_ATR * atr:
        if (close - state['start']) * sign < 0:     # moved past the leg start: restart there
            _reset(state, trend, close)
        return None
    state['pull'] = close if state['pull'] is None or (close - state['pull']) * sign < 0 else state['pull']
    retrace = (state['ext'] - state['pull']) * sign / leg
    if retrace > ZONE_FAIL:                         # the leg failed: a new one starts here
        _reset(state, trend, state['pull'])
        return None
    if retrace >= ZONE_LO:
        state['zone'] = True
    if not state['zone']:
        return None
    turned = (close - state['pull']) * sign >= TURN_ATR * atr
    leg_id = round(state['ext'], 2)
    if not turned or leg_id in state['used_legs'] or state['entries'] >= MAX_PER_DAY:
        return None
    if at is not None and not (WINDOW[0] <= (at.hour, at.minute) <= WINDOW[1]):
        return None
    if not side_allowed:
        return None
    state['used_legs'].append(leg_id)
    state['entries'] += 1
    return dict(direction='CE' if trend == 'UP' else 'PE', retrace=round(retrace, 3),
                leg=round(leg, 2), stop_ref=state['pull'])


def side_allowed(index_name, direction):
    """The side gate built in the forecast-robustness work, when installed; else allowed."""
    try:
        from .side_gate import side_allowed as gate   # noqa: WPS433 - optional module
        return bool(gate(index_name, direction).get('allowed', True))
    except Exception:  # noqa: BLE001 - absent or failing gate never blocks a shadow signal
        return True
