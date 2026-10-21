"""Gamma red-reduction (22-Oct-2026): R2 09:45 opening blackout, R3 30-min exhaustion
block after a same-side +50%, threshold 50 accepted end to end, volume as a note."""
import os, sys, tempfile, time
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kadima import celestial, storage, trade_policy
from kadima import trend_rules as T

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 21, tzinfo=IST)
FULL = dict(prev_close=74800.0, day_open=74700.0, expected_pct=1.0, atr=40.0, opening=None, bars15=[])


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def modes(**kw):
    value = dict(T.FEATURE_DEFAULTS); value.update(kw)
    T._cfg.update(at=time.monotonic() + 1e9, value=value)
    T._trend.clear(); T._inputs_cache.clear()


def plans(*rows):
    c = storage.conn()
    c.execute('''CREATE TABLE IF NOT EXISTS trade_audit_plans (
          audit_id TEXT PRIMARY KEY, day TEXT NOT NULL, strategy TEXT NOT NULL,
          producer TEXT NOT NULL, episode TEXT NOT NULL, signal_id INTEGER,
          exchange TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT NOT NULL,
          underlying TEXT NOT NULL, direction TEXT, suggested_at TEXT NOT NULL,
          origin TEXT NOT NULL, score REAL, plan TEXT NOT NULL,
          stage TEXT, blockers TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          ltp REAL, quote_at TEXT, high_ltp REAL, t1_at TEXT, t2_at TEXT, sl_at TEXT)''')
    c.execute('DELETE FROM trade_audit_plans')
    for i, (strategy, und, side, t1, t2) in enumerate(rows):
        c.execute('INSERT INTO trade_audit_plans (audit_id,day,strategy,producer,episode,exchange,token,'
                  'symbol,underlying,direction,suggested_at,origin,plan,blockers,last_seen_at,t1_at,t2_at) '
                  'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (f'a{i}', DAY.date().isoformat(), strategy, 'engine', f'e{i}', 'BFO', str(i), f'S{i}',
                   und, side, at(9, 46).isoformat(), 'SUGGESTION', '{}', '', at(9, 46).isoformat(),
                   t1 and t1.isoformat(), t2 and t2.isoformat()))
    c.commit()


def gate(direction, now, label='DOWN', index='SENSEX'):
    with patch.object(T, 'day_trend', lambda *a, **k: label), \
            patch.object(T, 'inputs', lambda token, now: FULL), \
            patch.object(T, 'band', lambda *a, **k: None), \
            patch.object(T.audit, 'log', lambda *a, **k: None):
        return T.entry_gate(index, '1', direction, 74500.0, None, now)


def test_r2_blocks_before_0945_only():
    plans()
    modes(mode='live', open_blackout='live', exhaustion='live')
    assert gate('PE', at(9, 44, 59))['block'] == 'gamma_before_0945_trend_unset'
    assert not gate('PE', at(9, 45)).get('block')


def test_r2_shadow_never_blocks():
    plans()
    modes(mode='live', open_blackout='shadow', exhaustion='live')
    assert not gate('PE', at(9, 30)).get('block')


def test_r3_blocks_same_side_for_30_min_after_plus_50():
    # 21-Oct: SENSEX 74600 PE T1 10:05:35, T2 10:12:48; the 10:12:48 PE signal went red.
    plans(('GAMMA', 'SENSEX', 'PE', at(10, 5, 35), at(10, 12, 48)))
    modes(mode='live', open_blackout='live', exhaustion='live')
    assert gate('PE', at(10, 12, 48))['block'] == 'gamma_move_exhausted_pe_1012'
    assert gate('PE', at(10, 42, 48))['block'] == 'gamma_move_exhausted_pe_1012'
    assert not gate('PE', at(10, 42, 49)).get('block')


def test_r3_ignores_other_side_index_and_strategy():
    plans(('GAMMA', 'SENSEX', 'PE', at(10, 5), None),
          ('SCALP', 'NIFTY', 'PE', at(10, 5), None),
          ('GAMMA', 'BANKNIFTY', 'CE', at(10, 5), None))
    modes(mode='live', open_blackout='live', exhaustion='live')
    assert not gate('PE', at(10, 10), index='NIFTY').get('block')
    assert not gate('CE', at(10, 10), label='SIDEWAYS').get('block')
    assert gate('PE', at(10, 10))['block'].startswith('gamma_move_exhausted')


def test_r3_ignores_future_observations():
    plans(('GAMMA', 'SENSEX', 'PE', at(11, 0), None))
    modes(mode='live', open_blackout='live', exhaustion='live')
    assert not gate('PE', at(10, 10)).get('block')


def test_rules_independent_of_trend_mode_off():
    plans()
    modes(mode='off', open_blackout='live', exhaustion='live')
    assert gate('PE', at(9, 30))['block'] == 'gamma_before_0945_trend_unset'
    assert gate('PE', at(10, 0)) == {}


def test_trend_rule_still_blocks_counter_trend():
    plans()
    modes(mode='live', open_blackout='live', exhaustion='live')
    assert gate('CE', at(12, 31))['block'] == 'counter_trend_down_day'


def test_audit_query_failure_fails_open():
    modes(mode='off', open_blackout='live', exhaustion='live')
    with patch.object(T, '_exhausted_at', side_effect=RuntimeError('db')):
        assert gate('PE', at(10, 10)) == {}


def test_threshold_50_accepted_everywhere():
    assert 50 in celestial.THRESHOLDS and 60 in celestial.THRESHOLDS
    for m in celestial.THRESHOLDS:
        # every Celestial threshold must be accepted by trade_policy (60 was not, 20-Oct to 22-Oct)
        assert trade_policy.eligible('gamma_blast', 'ACTIVE', 99.0, 'PE', None, 1.0, minimum=m) is False  # snap None
    class Snap:
        vwap, ema_fast, ema_slow, rsi = 100.0, 99.0, 100.0, 40.0
    assert trade_policy.eligible('gamma_blast', 'ACTIVE', 51.5, 'PE', Snap, 99.0, minimum=50) is True
    assert trade_policy.eligible('gamma_blast', 'ACTIVE', 49.9, 'PE', Snap, 99.0, minimum=50) is False
    assert trade_policy.eligible('gamma_blast', 'ACTIVE', 60.1, 'PE', Snap, 99.0, minimum=60) is True


def test_volume_is_a_note_not_a_blocker():
    now = at(10, 0)
    row = dict(id='BFO:1', underlying='SENSEX', direction='PE', last_updated=now.isoformat(),
               spot_at=now.isoformat(), premium_move_pct=8.0, gamma_score=55.0, priority=1,
               volume_spike=False, oi_support=True, spread_status='CLEAN', tradeable=True,
               direction_clear=True)
    with patch.object(celestial, 'read_config', lambda: dict(celestial.DEFAULTS, score_threshold=50)), \
            patch.object(celestial, 'news_impact', lambda *a: dict(news_score=0, news_available=True,
                                                                   news_blocked=False, news_sources=[])), \
            patch('kadima.index_event_gate.index_event_block', lambda *a: None), \
            patch.object(celestial, 'enabled', lambda: True):
        out = celestial.ranked_candidates(now, manual=True, candidates=[row])
    assert out[0]['notes'] == ['volume_unconfirmed']
    assert 'volume_unconfirmed' not in out[0]['blocked_reasons']
    assert 'below_threshold' not in out[0]['blocked_reasons']


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            try:
                fn(); print('PASS', name)
            except Exception as exc:  # noqa: BLE001
                failed += 1; print('FAIL', name, repr(exc))
    sys.exit(1 if failed else 0)
