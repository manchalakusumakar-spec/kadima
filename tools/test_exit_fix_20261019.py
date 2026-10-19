"""2026-10-19 early-exit fix: trail arms at +15%, retain ladder, confirmed EMA/VWAP exits."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
from kadima import trade_policy, celestial, celestial_exit

IST = timezone(timedelta(hours=5, minutes=30))

def test_trail():
    # +5% peak no longer arms the trail (old rule exited near breakeven)
    stop, active = trade_policy.ratchet(100, 105, None, 80)
    assert not active and stop == 80
    stop, active = trade_policy.ratchet(100, 120, None, 80)
    assert active and abs(stop - 110) < 1e-9          # keeps 50% below +35%
    stop, _ = trade_policy.ratchet(100, 150, None, 80)
    assert abs(stop - 132.5) < 1e-9                   # 65% from +35%
    stop, _ = trade_policy.ratchet(100, 300, None, 80)
    assert abs(stop - 250) < 1e-9                     # 75% from +100%
    stop, active = celestial.trail_stop(100, 108, None, 80)
    assert not active and stop == 80
    stop, active = celestial.trail_stop(100, 300, None, 80, .5)
    assert active and abs(stop - 250) < 1e-9

def run(ticks, structure):
    pos = SimpleNamespace(signal_id=991, exchange='NFO', token='1', index='NIFTY', direction='PE', entry_price=100)
    out = []
    with patch.object(celestial, 'news_impact', return_value={'news_blocked': False}):
        for now, ltp, spot in ticks:
            s = dict(structure, spot=spot, ts=now.isoformat(), spot_at=now.isoformat())
            health = lambda key, *a, s=s: s if key.startswith('structure:') else {'ts': now.isoformat(), 'bid': 1, 'ask': 2}
            with patch.object(celestial, 'health', side_effect=health):
                out.append(celestial_exit.evaluate(pos, ltp, now))
    return out

def test_spread_and_vwap_need_confirmation():
    t0 = datetime(2026, 10, 19, 11, 0, 5, tzinfo=IST)
    base = dict(vwap=23000, direction=None, score=0, oi_direction=None, shield_direction=None)
    # one spot reading above VWAP (bad for a PE) plus a 66%-wide spread: no exit any more
    r = run([(t0, 100, 23010)], base)
    assert r == [None], r
    # spot above VWAP for 2 completed minutes, still above now -> exit
    ticks = [(t0 + timedelta(seconds=30*i), 100, 23010) for i in range(6)]
    r = run(ticks, base)
    assert r[-1] == 'underlying_lost_vwap' and r[0] is None, r

def test_oi_flip_needs_score():
    t0 = datetime(2026, 10, 19, 12, 0, 5, tzinfo=IST)
    r = run([(t0, 100, 22900)], dict(vwap=23000, direction=None, score=20, oi_direction='CE', shield_direction=None))
    assert r == [None], r

if __name__ == '__main__':
    test_trail(); test_spread_and_vwap_need_confirmation(); test_oi_flip_needs_score()
    print('exit fix tests passed')
