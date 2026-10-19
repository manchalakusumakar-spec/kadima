"""Trader rules 2026-10-19: day trend, gamma with trend, band stop, min-risk entry."""
import os, sys, tempfile
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from kadima import trend_rules as T, trade_policy, celestial

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 19, 11, 30, tzinfo=IST)


def candles(day_closes, today_path):
    """Synthetic 5-min candles: prior days with ~1% range, then today's path."""
    rows = []
    for d, close in enumerate(day_closes):
        day = NOW.date() - timedelta(days=len(day_closes) - d)
        t = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        for i in range(75):
            c = close * (1 + 0.005 * ((i % 10) - 5) / 5)
            rows.append((t + timedelta(minutes=5 * i), c, c * 1.001, c * 0.999, c))
    t = NOW.replace(hour=9, minute=15)
    for i, c in enumerate(today_path):
        st = t + timedelta(minutes=5 * i)
        if st + timedelta(minutes=5) <= NOW:
            rows.append((st, today_path[i - 1] if i else c, max(c, today_path[i - 1] if i else c) + 2,
                         min(c, today_path[i - 1] if i else c) - 2, c))
    return rows


def fresh():
    T._inputs_cache.clear(); T._trend.clear(); T._cfg.update(value=None)


def test_mode_defaults_to_shadow():
    fresh()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('KADIMA_TREND_RULES', None)
        assert T.mode() == 'shadow' and not T.live()


def test_down_day_blocks_ce_allows_pe():
    fresh()
    path = [23150 - 12 * i for i in range(27)]          # steady fall from the open
    with patch.object(T, '_rows', return_value=candles([23146] * 14, path)), \
         patch.object(T, 'forecast_point', return_value=None), \
         patch.dict(os.environ, {'KADIMA_TREND_RULES': 'live'}):
        T._cfg.update(value=None)
        spot, vwap = path[-1], 23000
        assert T.day_trend('NIFTY', 't', spot, vwap, NOW) == 'DOWN'
        g = T.entry_gate('NIFTY', 't', 'CE', spot, vwap, NOW)
        assert g['block'] == 'counter_trend_down_day', g
        g = T.entry_gate('NIFTY', 't', 'PE', spot, vwap, NOW)
        assert g['block'] != 'counter_trend_down_day', g


def test_shadow_never_blocks():
    fresh()
    path = [23150 - 12 * i for i in range(27)]
    with patch.object(T, '_rows', return_value=candles([23146] * 14, path)), \
         patch.object(T, 'forecast_point', return_value=None), \
         patch.dict(os.environ, {'KADIMA_TREND_RULES': 'shadow'}):
        T._cfg.update(value=None)
        g = T.entry_gate('NIFTY', 't', 'CE', path[-1], 23000, NOW)
        assert not g.get('block') and g['trend'] == 'DOWN', g


def test_sideways_allows_both():
    fresh()
    path = [23146 + (5 if i % 2 else -5) for i in range(27)]
    with patch.object(T, '_rows', return_value=candles([23146] * 14, path)):
        assert T.raw_trend(23146, 23146, T.inputs('t', NOW)) == 'SIDEWAYS'


def test_entry_half_band_rule():
    fresh()
    b = dict(upper=23100.0, lower=22900.0, source='forecast')
    with patch.object(T, 'day_trend', return_value='SIDEWAYS'), \
         patch.object(T, 'inputs', return_value=dict(atr=8.0)), \
         patch.object(T, 'forecast_point', return_value=dict(b)), \
         patch.dict(os.environ, {'KADIMA_TREND_RULES': 'live', 'KADIMA_TREND_ENTRY': 'half'}):
        T._cfg.update(value=None)
        assert T.entry_gate('NIFTY', 't', 'CE', 22950, None, NOW)['block'] is None   # lower half
        assert T.entry_gate('NIFTY', 't', 'CE', 23050, None, NOW)['block'] == 'waiting_for_pullback_to_band_half'
        assert T.entry_gate('NIFTY', 't', 'CE', 23120, None, NOW)['block'] == 'chasing_beyond_band'
        assert T.entry_gate('NIFTY', 't', 'CE', 22890, None, NOW)['block'] == 'spot_beyond_band_stop'
        g = T.entry_gate('NIFTY', 't', 'PE', 23050, None, NOW)
        assert g['block'] is None and abs(g['stop_level'] - (23100 + 0.25 * 8)) < 1e-9


def test_bound_stop_cap_and_noise():
    # hard cap: never more than 35% below entry
    assert abs(T.bound_stop(10, 100, 100, None) - 65) < 1e-9
    # noise floor: initial stop pulled out of 2x 1-min noise (5% -> ceiling 90)
    assert abs(T.bound_stop(97, 100, 100, 5.0) - 90) < 1e-9
    # band moving down (loosening) is allowed, above the cap
    assert abs(T.bound_stop(80, 100, 100, 5.0, previous=90) - 80) < 1e-9
    # raise cannot go into noise but never below the previous stop
    assert abs(T.bound_stop(99, 100, 100, 5.0, previous=92) - 92) < 1e-9


def test_noise_pct():
    samples = [(NOW - timedelta(minutes=m), 100 * (1.02 if m % 2 else 1.0)) for m in range(8)]
    n = T.premium_noise_pct(samples, NOW)
    assert 1.9 < n < 2.1, n


def test_managed_stop_trail_and_lock():
    fresh()
    pos = SimpleNamespace(signal_id='s1', kind='gamma_blast', entry_price=100.0, peak_price=200.0)
    T.save_state('s1', dict(stop=80.0, ts=NOW.isoformat(), noise=3.0, lock=0))
    with patch.dict(os.environ, {'KADIMA_TREND_RULES': 'live', 'KADIMA_CELESTIAL_ENABLED': '0'}):
        T._cfg.update(value=None)
        base, eff, active, reason = T.managed_stop(pos, 180, None, now=NOW)
        assert base == 80 and active and abs(eff - 175) < 1e-9 and reason is None   # 75% of +100%
        pos.peak_price = 110.0                                     # +10%: below 15% activation
        base, eff, active, _ = T.managed_stop(pos, 105, None, now=NOW)
        assert not active and eff == 80
        T.note_lock('s1', 101.0)
        assert T.managed_stop(pos, 105, None, now=NOW)[1] == 101.0
        T.save_state('s1', dict(stop=None, breached=True, ts=NOW.isoformat()))
        assert T.managed_stop(pos, 105, None, now=NOW)[3] == 'forecast_band_breached'
        T.save_state('s1', dict(stop=80.0, ts=(NOW - timedelta(minutes=5)).isoformat()))
        assert T.managed_stop(pos, 105, None, now=NOW) is None                    # stale -> fallback


def test_activation_param():
    assert not trade_policy.ratchet(100, 118, None, 70, activation=0.2)[1]
    assert trade_policy.ratchet(100, 118, None, 70)[1]
    assert not celestial.trail_stop(100, 118, None, 70, .5, activation=0.2)[1]


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
    print('trend rules tests passed')
