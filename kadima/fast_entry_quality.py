"""Pure fast-entry filters over already validated, closed candle windows.

Thresholds are hypotheses for replay, not evidence of profitability. The noise
estimate is a simple five-bar mean true range, NOT Wilder ATR(14). No extra
quotes, broker calls, volume proxies or future candles are used here.
"""
import math

from . import config


REASONS = frozenset({
    'fast_quality_invalid_inputs', 'fast_quality_invalid_policy',
    'fast_choppy_five_minute_context', 'fast_weak_breakout_candle',
    'fast_entry_overextended', 'fast_buffered_stop_invalid',
})


def assess(one, five, spot, direction, level, raw_stop):
    """Return entry evidence and a buffered stop for a NEW trade only.

    Caller validates timestamps, continuity, session, freshness and the original
    breakout/hold rules. Both CE and PE are long option premiums; the returned
    stop is an UNDERLYING level, later mapped to premium and risk-sized.
    """
    if direction not in ('CE', 'PE') or len(one) < 7 or len(five) < 3:
        raise ValueError('fast_quality_invalid_inputs')
    base, breakout = one[-7:-2], one[-2]
    try:
        values = [spot, level, raw_stop]
        values += [r[k] for r in base + [breakout] + list(five[-3:])
                   for k in ('open', 'high', 'low', 'close')]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in values):
            raise ValueError('fast_quality_invalid_inputs')
        if any(not r['low'] <= min(r['open'], r['close'])
               <= max(r['open'], r['close']) <= r['high']
               for r in base + [breakout] + list(five[-3:])):
            raise ValueError('fast_quality_invalid_inputs')
    except (TypeError, KeyError):
        raise ValueError('fast_quality_invalid_inputs') from None

    efficiency_min = config.FAST_MIN_DIRECTIONAL_EFFICIENCY
    body_min = config.FAST_MIN_BREAKOUT_BODY_FRACTION
    close_min = config.FAST_MIN_BREAKOUT_CLOSE_LOCATION
    extension_max = config.FAST_MAX_EXTENSION_TRUE_RANGE
    buffer_multiple = config.FAST_STOP_BUFFER_TRUE_RANGE
    bounds = ((efficiency_min, 0, 1), (body_min, 0, 1), (close_min, 0, 1),
              (extension_max, 0, 1.5), (buffer_multiple, 0, 0.5))
    if any(type(v) not in (int, float) or not math.isfinite(v) or not lo < v <= hi
           for v, lo, hi in bounds):
        raise ValueError('fast_quality_invalid_policy')

    sign = 1 if direction == 'CE' else -1
    changes = [b['close'] - a['close'] for a, b in zip(five[-3:], five[-2:])]
    travelled = sum(abs(change) for change in changes)
    efficiency = sign * sum(changes) / travelled if travelled else 0.0
    if efficiency < efficiency_min:
        raise ValueError('fast_choppy_five_minute_context')

    span = breakout['high'] - breakout['low']
    body = sign * (breakout['close'] - breakout['open']) / span if span else 0.0
    close_location = ((breakout['close'] - breakout['low']) if sign == 1
                      else (breakout['high'] - breakout['close'])) / span if span else 0.0
    if body < body_min or close_location < close_min:
        raise ValueError('fast_weak_breakout_candle')

    # First baseline candle has no supplied prior close: use its high-low.
    ranges = [base[0]['high'] - base[0]['low']]
    for previous, current in zip(base, base[1:]):
        ranges.append(max(current['high'] - current['low'],
                          abs(current['high'] - previous['close']),
                          abs(current['low'] - previous['close'])))
    noise = sum(ranges) / len(ranges)
    extension = sign * (spot - level)
    if noise <= 0 or not 0 < extension <= extension_max * noise:
        raise ValueError('fast_entry_overextended')
    buffer_points = buffer_multiple * noise
    stop = raw_stop - sign * buffer_points
    if not math.isfinite(stop) or stop <= 0 or sign * (spot - raw_stop) <= 0:
        raise ValueError('fast_buffered_stop_invalid')
    return dict(spot_stop=stop, entry_quality=dict(
        version=1, five_minute_efficiency=round(efficiency, 6),
        breakout_body_fraction=round(body, 6),
        breakout_close_location=round(close_location, 6),
        baseline_mean_true_range=round(noise, 6), baseline_bars=len(base),
        extension_true_range=round(extension / noise, 6),
        raw_spot_stop=raw_stop, stop_buffer_points=round(buffer_points, 6),
        thresholds=dict(min_efficiency=efficiency_min, min_body=body_min,
                        min_close_location=close_min, max_extension=extension_max,
                        stop_buffer=buffer_multiple)))
