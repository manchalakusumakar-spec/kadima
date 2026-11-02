#!/usr/bin/env python3
"""Offline OI, Shield scoring and morning-veto regression tests."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima-oi-test-')
os.environ['OI_CONFIRM_MODE'] = 'shadow'
sys.path.insert(0, '.')
from kadima import config, engine, indicators, storage
from kadima.oi_confirmation import confirmation_from_rows, oi_confirmation, scalp_oi_block

now = datetime(2026, 10, 9, 10, tzinfo=config.IST)


def samples(ce, pe):
    out = []
    for minute in (15, 10, 5, 0):
        for strike in range(54900, 55600, 100):
            for side, change in (('CE', ce), ('PE', pe)):
                out.append(dict(ts=(now - timedelta(minutes=minute)).isoformat(),
                                strike=strike, opt_type=side, token=f'{strike}{side}',
                                oi=1000 + change * (15 - minute) / 15))
    return out


bull = confirmation_from_rows('BANKNIFTY', now, samples(-500, 500), 55200)
bear = confirmation_from_rows('BANKNIFTY', now, samples(500, -500), 55200)
assert bull['direction'] == 'CE' and bull['strength'] == 50
assert bull['ce_oi_change'] == -3500 and bull['pe_oi_change'] == 3500
assert bull['strikes'] == list(range(54900, 55600, 100)) and bull['samples'] == 4
assert bear['direction'] == 'PE' and bear['strength'] == 50
assert confirmation_from_rows('BANKNIFTY', now, samples(500, 500), 55200)['direction'] is None
assert confirmation_from_rows('BANKNIFTY', now, samples(-500, -500), 55200)['strength'] == 0
assert confirmation_from_rows('BANKNIFTY', now, samples(-500, 500)[:-1], 55200)['direction'] is None
assert confirmation_from_rows('BANKNIFTY', now, samples(-500, 500), None)['reason'] == 'sample_spot_unavailable'
future = samples(-500, 500) + [dict(ts=(now + timedelta(minutes=1)).isoformat(),
    token='future', strike=55200, opt_type='CE', oi=0)]
assert confirmation_from_rows('BANKNIFTY', now, future, 55200) == bull
assert scalp_oi_block('PE', now, bull) == 'oi_confirmation_opposes'
assert scalp_oi_block('CE', now, bull) is None
assert scalp_oi_block('PE', now.replace(hour=10, minute=30), bull) is None
assert scalp_oi_block('PE', now, bull | {'strength': 39.99}) is None
assert oi_confirmation('BANKNIFTY', now)['direction'] is None  # missing DB is neutral

# Test the public read-only path, same-session candle selection and no lookahead.
c = storage.conn()
with c:
    c.executemany('INSERT INTO oi_snapshots (ts,index_name,token,strike,opt_type,oi) VALUES (?,?,?,?,?,?)',
                  [(r['ts'], 'BANKNIFTY', r['token'], r['strike'], r['opt_type'], r['oi'])
                   for r in samples(-500, 500)])
storage.upsert_candle(config.GAMMA_INDICES['BANKNIFTY'].token, 1,
    (now - timedelta(minutes=1)).isoformat(), 55200, 55200, 55200, 55200, 1, 55200, True)
storage.upsert_candle(config.GAMMA_INDICES['BANKNIFTY'].token, 1,
    now.isoformat(), 58000, 58000, 58000, 58000, 1, 58000, True)
observed = oi_confirmation('BANKNIFTY', now)
assert observed['direction'] == 'CE' and observed['strength'] == 50
assert observed['spot'] == 55200 and observed['spot_at'] == now.isoformat()
assert observed['spot_source'] == 'completed_index_1m_candle'

# Exercise the real _score without constructing an Engine or starting workers.
instance = engine.Engine.__new__(engine.Engine)
instance._shield_context = lambda st: st.shield_evidence
snapshot = indicators.Snapshot(close=101, vwap=100, ema_fast=101, ema_slow=100, rsi=60)
with patch.object(engine, 'oi_confirmation', return_value=bull), patch.object(engine.audit, 'log') as log:
    state = engine.IndexState('NIFTY', spot=101, snap=snapshot)
    instance._score(state, now, 101)
    assert state.score == 65 and state.components['oi_confirmation'] == bull
    assert any(call.args[0] == 'SHIELD_SCORE' and 'oi_confirmation' in call.kwargs['components']
               for call in log.call_args_list)
assert sum(engine.WEIGHTS.values()) == 100 and engine.WEIGHTS['oi_confirm'] == 0
live_weights = engine.WEIGHTS | {'ema_cross': 15, 'rsi_50': 10, 'oi_confirm': 15}
with patch.object(config, 'OI_CONFIRM_MODE', 'live'), patch.dict(engine.WEIGHTS, live_weights), \
        patch.object(engine, 'oi_confirmation', return_value=bull), patch.object(engine.audit, 'log'):
    state = engine.IndexState('NIFTY', spot=101, snap=snapshot)
    instance._score(state, now, 101)
    assert state.score == 65 and sum(engine.WEIGHTS.values()) == 100
with patch.object(config, 'OI_CONFIRM_MODE', 'live'), patch.dict(engine.WEIGHTS, live_weights), \
        patch.object(engine, 'oi_confirmation', return_value=bear), patch.object(engine.audit, 'log'):
    state = engine.IndexState('NIFTY', spot=101, snap=snapshot)
    instance._score(state, now, 101)
    assert state.score == 50  # opposing OI contributes to PE, not CE
print('ALL PASS: OI signs/strength, matched baskets, no lookahead, shadow/live weights, morning veto')
