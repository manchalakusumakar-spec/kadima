"""Rule 2 live on its own (21-Oct-2026): gamma only with the day trend; NIFTY proxy
for an index without enough history (e.g. MIDCPNIFTY)."""
import os, sys, tempfile, time
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kadima import trend_rules as T

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 21, 10, 30, tzinfo=IST)
FULL = dict(prev_close=22889.0, day_open=22900.0, expected_pct=1.0, atr=15.0, opening=None, bars15=[])
BAND = dict(upper=22900.0, lower=22800.0, mid=22850.0, buffer=4.0, source='forecast')


def modes(**kw):
    value = dict(T.FEATURE_DEFAULTS); value.update(kw)
    T._cfg.update(at=time.monotonic() + 1e9, value=value)
    T._trend.clear(); T._inputs_cache.clear()


def gate(direction, label='DOWN', data=FULL, band=BAND, index='NIFTY', spot=22880.0):
    with patch.object(T, 'day_trend', lambda *a, **k: label), \
            patch.object(T, 'inputs', lambda token, now: data), \
            patch.object(T, 'band', lambda *a, **k: band), \
            patch.object(T.audit, 'log', lambda *a, **k: None):
        return T.entry_gate(index, '99926000', direction, spot, None, NOW)


def test_rule2_live_blocks_counter_trend_only():
    modes(mode='shadow', rule2='live')
    assert gate('CE')['block'] == 'counter_trend_down_day'
    assert not gate('PE', spot=22820.0).get('block')        # band would block: rules 3-4 stay shadow
    assert gate('PE').get('stop_level') is None             # band stop not applied


def test_rule2_shadow_never_blocks():
    modes(mode='shadow', rule2='shadow')
    assert not gate('CE').get('block')


def test_sideways_allows_both_sides():
    modes(mode='shadow', rule2='live')
    assert not gate('CE', label='SIDEWAYS').get('block')
    assert not gate('PE', label='SIDEWAYS').get('block')


def test_mode_off_disables_everything():
    modes(mode='off', rule2='live')
    assert gate('CE') == {}


def test_full_live_mode_unchanged():
    modes(mode='live', rule2='shadow')
    assert gate('CE')['block'] == 'counter_trend_down_day'
    assert gate('PE', spot=22820.0)['block'] == 'waiting_for_pullback_to_band_half'


def test_index_without_history_uses_nifty_trend():
    modes(mode='shadow', rule2='live')
    T._trend['NIFTY'] = dict(day=NOW.date().isoformat(), label='DOWN', since=None, seen=0, against=0)
    thin = dict(FULL, prev_close=None)
    with patch.object(T, 'day_trend', lambda *a, **k: 'SIDEWAYS'), \
            patch.object(T, 'inputs', lambda token, now: thin), \
            patch.object(T, 'band', lambda *a, **k: None), \
            patch.object(T.audit, 'log', lambda *a, **k: None):
        T._trend['NIFTY'] = dict(day=NOW.date().isoformat(), label='DOWN', since=None, seen=0, against=0)
        out = T.entry_gate('MIDCPNIFTY', '99926074', 'CE', 13500.0, None, NOW)
    assert out['block'] == 'counter_trend_down_day' and out['trend'] == 'DOWN'


def test_index_with_history_keeps_its_own_trend():
    modes(mode='shadow', rule2='live')
    T._trend['NIFTY'] = dict(day=NOW.date().isoformat(), label='DOWN', since=None, seen=0, against=0)
    with patch.object(T, 'day_trend', lambda *a, **k: 'UP'), \
            patch.object(T, 'inputs', lambda token, now: FULL), \
            patch.object(T, 'band', lambda *a, **k: None), \
            patch.object(T.audit, 'log', lambda *a, **k: None):
        out = T.entry_gate('MIDCPNIFTY', '99926074', 'CE', 13500.0, None, NOW)
    assert not out.get('block') and out['trend'] == 'UP'


def test_bad_rule2_value_falls_back_to_shadow():
    T._cfg.update(value=None, at=0)
    path = os.path.join(os.environ['KADIMA_DATA_DIR'], 'trend_rules.json')
    with open(path, 'w') as f:
        f.write('{"rule2": "yes"}')
    try:
        assert T.feature('rule2') == 'shadow'
        with open(path, 'w') as f:
            f.write('{"rule2": "live"}')
        T._cfg.update(value=None, at=0)
        assert T.feature('rule2') == 'live' and T.mode() == 'shadow'
    finally:
        os.remove(path); T._cfg.update(value=None, at=0)


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn(); print('PASS', name)
            except Exception as exc:  # noqa: BLE001
                failed += 1; print('FAIL', name, repr(exc))
    sys.exit(1 if failed else 0)
