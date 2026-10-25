"""Sniper mode (24-Oct-2026): few, early, with-trend, confirmed gamma entries."""
import json, os, sys, tempfile, time
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from kadima import sniper as S, storage

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 23, tzinfo=IST)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def cfg(**kw):
    v = dict(S.DEFAULTS); v['ab_filter'] = False   # A+B has its own tests below
    v.update(kw)
    S._cfg.update(at=time.monotonic() + 1e9, value=v)
    S._last_log.clear(); S._np_logged.clear()


def reset(plans=()):
    c = storage.conn()
    c.execute('''CREATE TABLE IF NOT EXISTS trade_audit_plans (
          audit_id TEXT PRIMARY KEY, day TEXT, strategy TEXT, producer TEXT, episode TEXT,
          signal_id INTEGER, exchange TEXT, token TEXT, symbol TEXT, underlying TEXT,
          direction TEXT, suggested_at TEXT, origin TEXT, score REAL, plan TEXT, stage TEXT,
          blockers TEXT, last_seen_at TEXT, ltp REAL, quote_at TEXT, high_ltp REAL,
          t1_at TEXT, t2_at TEXT, sl_at TEXT)''')
    c.execute('DELETE FROM trade_audit_plans')
    S._conn().execute('DELETE FROM sniper_entries')
    c.execute('DELETE FROM positions')
    for i, (token, und, side, when, entry, sl) in enumerate(plans):
        c.execute('INSERT INTO trade_audit_plans (audit_id,day,strategy,token,underlying,direction,'
                  'suggested_at,plan,sl_at) VALUES (?,?,?,?,?,?,?,?,?)',
                  (f'a{i}', DAY.date().isoformat(), 'GAMMA', token, und, side, when.isoformat(),
                   json.dumps({'entry': entry}), sl and sl.isoformat()))
    c.commit()


def position(sid, status, entry, exit_=None, index='BANKNIFTY', side='PE'):
    c = storage.conn()
    c.execute('INSERT INTO positions (signal_id,kind,index_name,direction,entry_price,status,exit_price) '
              'VALUES (?,?,?,?,?,?,?)', (sid, 'gamma_blast', index, side, entry, status, exit_))
    c.commit()
    S.note_entry(sid, index, side, 't', at(9, 50))


QUIET = patch.object(S.audit, 'log', lambda *a, **k: None)
OK_PLANS = [('t1', 'BANKNIFTY', 'PE', at(9, 47), 270.0, None)]


def chk(now, token='t2', ltp=300.0, trend='DOWN', side='PE', index='BANKNIFTY'):
    with QUIET:
        return S.check(index, side, token, ltp, trend, now)


def test_window_and_trend():
    cfg(mode='live'); reset(OK_PLANS)
    assert chk(at(9, 44, 59)) == 'sniper_outside_window'
    assert chk(at(10, 30)) == 'sniper_outside_window'
    assert chk(at(9, 50)) is None
    assert chk(at(9, 50), trend='SIDEWAYS') == 'sniper_sideways_day'
    assert chk(at(9, 50), trend='UP') == 'sniper_counter_trend'
    assert chk(at(9, 50), trend='UP', side='CE') == 'sniper_first_signal'


def test_needs_an_earlier_other_contract_signal():
    cfg(mode='live')
    reset([])
    assert chk(at(9, 50)) == 'sniper_first_signal'
    reset([('t2', 'BANKNIFTY', 'PE', at(9, 46), 300.0, None)])         # same contract only
    assert chk(at(9, 50)) == 'sniper_first_signal'
    reset([('t1', 'BANKNIFTY', 'PE', at(9, 49, 30), 270.0, None)])     # only 30 s earlier
    assert chk(at(9, 50)) == 'sniper_first_signal'
    reset([('t1', 'BANKNIFTY', 'PE', at(9, 48), 270.0, None)])
    assert chk(at(9, 50)) is None


def test_chase_guard():
    cfg(mode='live')
    reset(OK_PLANS + [('t2', 'BANKNIFTY', 'PE', at(9, 48), 100.0, None)])
    assert chk(at(9, 50), ltp=115.0) is None
    assert chk(at(9, 50), ltp=115.1) == 'sniper_chasing_premium'


def test_side_stopped_by_any_observed_stop():
    cfg(mode='live')
    reset(OK_PLANS + [('t9', 'BANKNIFTY', 'PE', at(9, 46), 250.0, at(9, 49))])
    assert chk(at(9, 50)) == 'sniper_side_stopped'
    assert chk(at(9, 48, 50)) is None  # stop observed only at 09:49
    reset(OK_PLANS + [('t9', 'BANKNIFTY', 'PE', at(9, 46), 250.0, at(10, 5))])
    assert chk(at(9, 50)) is None


def test_daily_cap_and_after_loss():
    cfg(mode='live')
    reset(OK_PLANS + [('n1', 'NIFTY', 'PE', at(9, 47), 120.0, None),
                      ('s1', 'SENSEX', 'PE', at(9, 47), 500.0, None)])
    position(1, 'closed', 300.0, 200.0, 'BANKNIFTY', 'PE')            # lost
    assert chk(at(9, 55)) == 'sniper_side_stopped'                      # same index+side
    assert chk(at(9, 55), index='NIFTY') is None                        # different index ok
    position(2, 'open', 120.0, None, 'NIFTY', 'PE')
    assert chk(at(9, 56), index='NIFTY') == 'sniper_side_already_open'
    assert chk(at(9, 56), index='SENSEX') == 'sniper_daily_cap'


def test_unfilled_entry_is_not_a_trade():
    cfg(mode='live'); reset(OK_PLANS + [('n1', 'NIFTY', 'PE', at(9, 47), 120.0, None),
                                        ('s1', 'SENSEX', 'PE', at(9, 47), 500.0, None)])
    position(1, 'closed', 0.0, None, 'NIFTY', 'PE')
    position(2, 'open', 500.0, None, 'SENSEX', 'PE')
    assert chk(at(9, 55)) is None


def gate(mode_, existing=None, manual=False, spot=54000.0, band=None, now=None):
    cfg(mode=mode_)
    sel = dict(token='t2', option_ltp=300.0, symbol='BANKNIFTY54400PE')
    from kadima import trend_rules as T
    with QUIET, patch.object(S, '_trend', lambda *a: 'DOWN'), \
            patch.object(T, 'band', lambda *a, **k: band), patch.object(T, 'live', lambda: True):
        return S.gate('BANKNIFTY', 'idx', 'PE', spot, None, now or at(9, 50),
                      gate={'block': existing} if existing else {'trend': 'DOWN'},
                      selected=sel, manual=manual)


def test_gate_modes():
    reset(OK_PLANS)
    assert gate('off') == {}
    assert gate('live', manual=True) == {}
    assert gate('live') == {'pass': True}
    assert gate('live', now=at(10, 31)) == {'block': 'sniper_outside_window'}
    assert gate('shadow', now=at(10, 31)) == {}                          # shadow never blocks


def test_gate_overrides_r3_and_pullback_but_not_band_stop():
    reset(OK_PLANS)
    band = dict(upper=54200.0, lower=53800.0, mid=54000.0, buffer=10.0)  # PE stop = 54210
    r = gate('live', existing='gamma_move_exhausted_pe_0940', band=band)
    assert r['override'] and r['stop_level'] == 54210.0
    r = gate('live', existing='waiting_for_pullback_to_band_half', band=band)
    assert r['override'] == 'waiting_for_pullback_to_band_half'
    assert gate('live', existing='waiting_for_pullback_to_band_half', band=band,
                spot=54300.0) == {'block': 'spot_beyond_band_stop'}
    r = gate('live', existing='counter_trend_down_day', band=band)
    assert r == {'pass': True}   # Sniper passes; the caller still applies the old block


def test_gate_error_fails_closed_in_live_only():
    reset(OK_PLANS)
    with patch.object(S, '_gate_impl', side_effect=RuntimeError('x')), QUIET:
        cfg(mode='live')
        assert S.gate('BANKNIFTY', 'i', 'PE', 1, None, at(9, 50), selected={}) == \
            {'block': 'sniper_error_fail_closed'}
        cfg(mode='shadow')
        assert S.gate('BANKNIFTY', 'i', 'PE', 1, None, at(9, 50), selected={}) == {}


def test_size():
    with QUIET:
        cfg(mode='live')
        assert S.size('gamma_blast', False, 272.7, 177.25, 30, 3, 22000) == (1, None)     # 2864 < 3300
        assert S.size('gamma_blast', False, 153.95, 100.07, 120, 1, 22000) == \
            (1, 'sniper_risk_over_cash_limit')                                          # 6466 > 3300
        assert S.size('gamma_blast', False, 272.7, 177.25, 30, 1, None) == (1, 'sniper_cash_unreadable')
        assert S.size('gamma_blast', True, 153.95, 100.07, 120, 2, 22000) == (2, None)    # manual
        assert S.size('scalp', False, 153.95, 100.07, 120, 2, 22000) == (2, None)
        cfg(mode='shadow')
        assert S.size('gamma_blast', False, 153.95, 100.07, 120, 2, 22000) == (2, None)


def test_no_progress_exit():
    cfg(mode='live'); reset()
    position(7, 'open', 100.0)
    p = SimpleNamespace(kind='gamma_blast', status='open', signal_id=7, entry_price=100.0,
                        entry_ts=at(9, 50), peak_price=105.0)
    with QUIET:
        assert S.no_progress(p, at(10, 9)) is None
        assert S.no_progress(p, at(10, 10)) == 'sniper_no_progress'
        p.peak_price = 110.0
        assert S.no_progress(p, at(10, 30)) is None
        p.peak_price = 105.0; p.signal_id = 8                     # not a Sniper trade
        assert S.no_progress(p, at(10, 30)) is None
        p.signal_id = 7; cfg(mode='shadow')
        assert S.no_progress(p, at(10, 30)) is None


def test_relax_reasons():
    cfg(mode='live')
    notes = []
    assert S.relax_reasons(['oi_unconfirmed', 'contract_not_tradeable', 'direction_unconfirmed'],
                           notes) == ['contract_not_tradeable']
    assert notes == ['oi_unconfirmed', 'direction_unconfirmed']
    cfg(mode='shadow')
    assert S.relax_reasons(['oi_unconfirmed'], []) == ['oi_unconfirmed']


def bars(closes, start=(9, 15)):
    """5-min candles from closes; high/low = close +/- 2."""
    t0 = at(*start)
    return [(t0 + timedelta(minutes=5 * i), c, c + 2, c - 2, c) for i, c in enumerate(closes)]


def ab(direction, spot, rows, now, **kw):
    cfg(**{'mode': 'live', 'ab_filter': True, **kw})
    with patch.object(S, '_bars_today', lambda *a: rows):
        return S.ab_check('NIFTY', 'idx', direction, spot, now)


def test_ab_filter():
    # 09:15..09:45 closed (7 bars); 15 min before 10:00 = close of the 09:40 bar (ends 09:45)
    up = bars([25000, 25010, 25020, 25030, 25040, 25050, 25060, 25070, 25080])
    now = at(10, 0)
    assert ab('CE', 25150.0, up, now) is None                 # new high, +0.36% in 15 min
    assert ab('CE', 25120.0, up, now) == 'sniper_move_too_small'   # +0.24% < 0.3
    assert ab('CE', 25120.0, up, now, b_pct=0.2) is None
    assert ab('CE', 25050.0, up, now) == 'sniper_not_near_day_extreme'  # 0.13% below high
    assert ab('PE', 25150.0, up, now) == 'sniper_not_near_day_extreme'  # far from day low
    down = bars([25000, 24990, 24980, 24970, 24960, 24950, 24940, 24930, 24920])
    assert ab('PE', 24850.0, down, now) is None
    assert ab('CE', 24850.0, down, now) == 'sniper_not_near_day_extreme'


def test_ab_fails_closed_without_data():
    now = at(10, 0)
    up = bars([25000, 25010, 25020, 25030, 25040, 25050, 25060, 25070, 25080])
    assert ab('CE', None, up, now) == 'sniper_ab_no_data'
    assert ab('CE', 25150.0, up[:2], now) == 'sniper_ab_no_data'
    assert ab('CE', 25150.0, up[:4], now) == 'sniper_ab_no_data'    # last candle 09:30, stale
    cfg(mode='live', ab_filter=True)
    with patch.object(S, '_bars_today', lambda *a: up):
        assert S.ab_check('NIFTY', None, 'CE', 25150.0, now) == 'sniper_ab_no_data'
    assert ab('CE', 1.0, [], now, ab_filter=False) is None


def test_index_allow_list_and_second_signal_switch():
    cfg(mode='live'); reset(OK_PLANS)
    assert chk(at(9, 50), index='FINNIFTY') == 'sniper_index_excluded'
    cfg(mode='live', indices=''); assert chk(at(9, 50), index='FINNIFTY') == 'sniper_first_signal'
    cfg(mode='live', require_second_signal=False)
    assert chk(at(9, 50), index='NIFTY') is None


def test_ab_runs_inside_check():
    cfg(mode='live', ab_filter=True); reset(OK_PLANS)
    with QUIET, patch.object(S, '_bars_today', lambda *a: []):
        assert S.check('BANKNIFTY', 'PE', 't2', 300.0, 'DOWN', at(9, 50),
                       index_token='i', spot=54000.0) == 'sniper_ab_no_data'
        assert S.check('BANKNIFTY', 'PE', 't2', 300.0, 'UP', at(9, 50),
                       index_token='i', spot=54000.0) == 'sniper_counter_trend'


def test_config_file_and_bad_values():
    path = os.path.join(os.environ['KADIMA_DATA_DIR'], 'sniper.json')
    json.dump({'mode': 'live', 'max_trades': 3, 'max_risk_cash_pct': 'x', 'unknown': 1}, open(path, 'w'))
    S._cfg.update(at=0.0, value=None)
    c = S.config()
    assert c['mode'] == 'live' and c['max_trades'] == 3 and c['max_risk_cash_pct'] == 15.0
    json.dump({'mode': 'bogus'}, open(path, 'w')); S._cfg.update(at=0.0, value=None)
    assert S.config()['mode'] == 'off'
    os.remove(path)


if __name__ == '__main__':
    import inspect
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and inspect.isfunction(fn):
            fn(); n += 1; print('ok', name)
    print(n, 'tests passed')
