"""Offline tests for the 2026-10-20 scalp v2 build (no broker, no network).

Run from the repo root after the installer has patched the tree (CI does this):
  KADIMA_DATA_DIR=$(mktemp -d) python tools/test_scalp_v2_20261020.py
"""
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kadima import scalp_v2  # noqa: E402
from kadima.config import IST  # noqa: E402

FAILS = []
LOGS = []
scalp_v2.audit.log = lambda event, **kw: LOGS.append((event, kw))


def check(name, ok):
    print(('PASS ' if ok else 'FAIL ') + name)
    if not ok:
        FAILS.append(name)


def feed(name, start, prices, step=3):
    """One tick every `step` seconds through `prices`."""
    t = start
    for p in prices:
        scalp_v2.on_tick(name, '26000', t, p)
        t += timedelta(seconds=step)
    return t


def reset():
    scalp_v2._states.clear()
    LOGS.clear()
    scalp_v2._cfg.update(at=0.0, value=None)


def base_session(day, minutes=20, low=100.0, high=110.0):
    """Quiet range 100-110 from 09:30, rising slightly so the 5-min context is up."""
    t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST)
    for m in range(minutes):
        drift = m * 0.05
        prices = [low + 2 + drift, high - 1 + drift, low + 1 + drift, high - 2 + drift] * 5
        feed('NIFTY', t + timedelta(minutes=m), prices)
    return t + timedelta(minutes=minutes)


DAY = datetime(2026, 10, 21, tzinfo=IST)

# ---------------------------------------------------------------- trigger
reset()
t = base_session(DAY)
bars = scalp_v2._states['NIFTY']
level = max(b.h for b in bars.closed1(t)[-5:])
width = sum(b.h - b.l for b in bars.closed1(t)[-5:]) / 5
# two 15 s closes just above the level (0.3 x width beyond)
t = feed('NIFTY', t, [level + 0.3 * width] * 5)
t = feed('NIFTY', t, [level + 0.35 * width] * 5)
t = feed('NIFTY', t, [level + 0.35 * width])        # closes the second 15 s bar
trig = scalp_v2.active_trigger('NIFTY')
check('15 s breakout triggers a CE scalp', trig is not None and trig['direction'] == 'CE')
check('stop is level - 1.5 x ATR', trig is not None and
      abs(trig['spot_stop'] - (trig['level'] - 1.5 * trig['atr'])) < 0.02)
check('trigger logged', any(e == 'SCALP_V2_TRIGGER' for e, _ in LOGS))

# cooldown: a fresh break of a higher level within 5 minutes does not re-trigger
n = sum(e == 'SCALP_V2_TRIGGER' for e, _ in LOGS)
t = feed('NIFTY', t, [level + 0.8 * width] * 25)
check('no second CE trigger inside the 5-minute cooldown',
      sum(e == 'SCALP_V2_TRIGGER' for e, _ in LOGS) == n)

# extended: price already far past the level -> no trigger
reset()
t = base_session(DAY)
bars = scalp_v2._states['NIFTY']
level = max(b.h for b in bars.closed1(t)[-5:])
width = sum(b.h - b.l for b in bars.closed1(t)[-5:]) / 5
t = feed('NIFTY', t, [level + 1.6 * width] * 11)
check('no trigger when already > 1 width past the level', scalp_v2.active_trigger('NIFTY') is None)

# run-up: a 3-minute burst bigger than 3 ATR is skipped
reset()
t = base_session(DAY)
bars = scalp_v2._states['NIFTY']
level = max(b.h for b in bars.closed1(t)[-5:])
atr = bars.atr(t.replace(second=0))
width = sum(b.h - b.l for b in bars.closed1(t)[-5:]) / 5
for b in bars.bars15:                      # pretend price was far lower 3 minutes ago
    if b.start <= t - timedelta(minutes=2, seconds=30):
        b.c = level - 4 * atr
t = feed('NIFTY', t, [level + 0.3 * width] * 11)
check('3-minute run-up over 3 ATR is skipped, not triggered',
      scalp_v2.active_trigger('NIFTY') is None and any(e == 'SCALP_V2_SKIPPED' for e, _ in LOGS))

# outside the window: nothing before 09:25
reset()
early = datetime(2026, 10, 21, 9, 16, tzinfo=IST)
feed('NIFTY', early, [100, 120, 90, 130] * 20)
check('no trigger before 09:25', scalp_v2.active_trigger('NIFTY') is None)

# off mode: the tick hook does nothing
reset()
os.environ['KADIMA_SCALP_V2'] = 'off'
scalp_v2._cfg.update(at=0.0, value=None)
scalp_v2.on_tick('NIFTY', '26000', datetime(2026, 10, 21, 10, 0, tzinfo=IST), 100.0)
check('off mode keeps no state', 'NIFTY' not in scalp_v2._states)
del os.environ['KADIMA_SCALP_V2']
scalp_v2._cfg.update(at=0.0, value=None)

# a bad tick never raises
try:
    scalp_v2.on_tick('NIFTY', '26000', 'not-a-time', 100.0)
    check('bad tick does not raise', True)
except Exception:  # noqa: BLE001
    check('bad tick does not raise', False)

# ---------------------------------------------------------------- score
clean = dict(extension_w=0.3, runup_atr=1.0)
chase = dict(extension_w=1.1, runup_atr=2.8)
check('clean with-trend breakout scores >= 85', scalp_v2.score(clean, 'UP', 2.5) >= 85)
check('chasing breakout scores lower than clean', scalp_v2.score(chase, 'UP', 9.0) < scalp_v2.score(clean, 'UP', 2.5) - 25)
check('sideways scores below with-trend', scalp_v2.score(clean, 'SIDEWAYS', 2.5) < scalp_v2.score(clean, 'UP', 2.5))
check('score stays within 0-100', 0 <= scalp_v2.score(dict(extension_w=5, runup_atr=9), 'SIDEWAYS', 50) <= 100)

# ---------------------------------------------------------------- poll order / wait
reset()
os.environ['KADIMA_SCALP_V2_POLL'] = 'live'
scalp_v2._cfg.update(at=0.0, value=None)
states = {n: types.SimpleNamespace(name=n) for n in ('SENSEX', 'BANKNIFTY', 'NIFTY')}
scalp_v2._states['NIFTY'] = scalp_v2.IndexBars('NIFTY', '26000')
scalp_v2._states['NIFTY'].trigger = dict(mono=time.monotonic())
check('triggered index is polled first when priority_poll is live',
      [s.name for s in scalp_v2.order(states)][0] == 'NIFTY')
scalp_v2._wake.clear()
start = time.monotonic()
import threading  # noqa: E402
threading.Timer(0.3, scalp_v2._wake.set).start()
scalp_v2.wait(5)
check('OI-loop wait returns early on a trigger', time.monotonic() - start < 2)
os.environ['KADIMA_SCALP_V2_POLL'] = 'shadow'
scalp_v2._cfg.update(at=0.0, value=None)
check('shadow keeps the old poll order', [s.name for s in scalp_v2.order(states)] == list(states))
del os.environ['KADIMA_SCALP_V2_POLL']
scalp_v2._cfg.update(at=0.0, value=None)

# ---------------------------------------------------------------- build_row with fakes
from kadima import trend_rules, celestial, strike_selector, trade_policy, fast_momentum  # noqa: E402

now = datetime(2026, 10, 21, 10, 30, tzinfo=IST)
Q = types.SimpleNamespace(symbol='NIFTY27OCT2622600CE', token='111', strike=22600, opt_type='CE',
                          ltp=100.0, bid=99.8, ask=100.2, tradeable=True)
chain = types.SimpleNamespace(_last_poll=time.monotonic(), _expiry='27OCT2026', quotes={'111': Q},
                              index=types.SimpleNamespace(token='26000', opt_exchange='NFO'))
st = types.SimpleNamespace(name='NIFTY', chain=chain, spot=22610.0, last_updated=now,
                           snap=types.SimpleNamespace(vwap=22590.0))
trig = dict(direction='CE', level=22600.0, width=10.0, atr=8.0, spot_stop=22588.0,
            runup_atr=1.2, extension_w=0.3, at=(now - timedelta(seconds=20)).isoformat(), mono=time.monotonic())
patches = dict(
    vwap_for=(trend_rules, lambda n, t=None: 22590.0),
    day_trend=(trend_rules, lambda *a, **k: 'UP'),
    fresh=(celestial, lambda *a, **k: True),
    rank=(strike_selector, lambda view, d, s, days: [types.SimpleNamespace(rejected=False, quote=Q)]),
    premium_stop=(trade_policy, lambda spot, lvl, k, ltp, days, d: 92.0),
    live_blocks=(fast_momentum, lambda *a, **k: []),
)
saved = {k: getattr(m, k) for k, (m, f) in patches.items()}
for k, (m, f) in patches.items():
    setattr(m, k, f)
try:
    os.environ['KADIMA_SCALP_V2'] = 'live'
    scalp_v2._cfg.update(at=0.0, value=None)
    row = scalp_v2.build_row(st, trig, None, now)
    check('v2 row is SETUP_READY and eligible in live mode', row['stage'] == 'SETUP_READY' and row['eligible'])
    check('v2 row has the executor fields', all(k in row for k in
          ('episode', 'token', 'symbol', 'exchange', 'direction', 'spot_stop', 'days', 'entry', 'stop', 't1', 't2', 'score')))
    check('v2 row keeps strategy fast_momentum for the executor', row['strategy'] == 'fast_momentum')
    check('entry at ask, T1 +12% and T2 +20% (spec Scalp targets)', row['entry'] == 100.2 and row['t1'] == round(100.2 * 1.12, 2) and row['t2'] == round(100.2 * 1.2, 2) and row['stop'] < row['entry'])
    trend_rules.day_trend = lambda *a, **k: 'DOWN'
    row = scalp_v2.build_row(st, trig, None, now)
    check('counter-trend scalp is blocked', 'counter_trend_scalp' in row['blocked_reasons'] and not row['eligible'])
    trend_rules.day_trend = lambda *a, **k: 'UP'
    st.spot = 22599.0
    row = scalp_v2.build_row(st, trig, None, now)
    check('breakout that failed before entry is blocked', 'breakout_failed_before_entry' in row['blocked_reasons'])
    st.spot = 22615.0
    row = scalp_v2.build_row(st, trig, None, now)
    check('extended before entry is blocked', 'extended_before_entry' in row['blocked_reasons'])
    st.spot = 22610.0
    trade_policy.premium_stop = lambda *a: 40.0
    row = scalp_v2.build_row(st, trig, None, now)
    check('35% hard cap applies to the premium stop', abs(row['stop'] - 100.2 * 0.65) < 0.02)
    os.environ['KADIMA_SCALP_V2'] = 'shadow'
    scalp_v2._cfg.update(at=0.0, value=None)
    trade_policy.premium_stop = lambda *a: 92.0
    row = scalp_v2.build_row(st, trig, None, now)
    check('shadow rows are never eligible', not row['eligible'] and 'scalp_v2_shadow' in row['blocked_reasons'])
    # merge: shadow returns v1 unchanged, live returns v2 or a blocked placeholder
    v1 = dict(underlying='NIFTY', stage='BLOCKED', blocked_reasons=['x'], eligible=False)
    scalp_v2._states['NIFTY'] = scalp_v2.IndexBars('NIFTY', '26000')
    scalp_v2._states['NIFTY'].trigger = trig
    check('shadow merge returns the v1 row', scalp_v2.merge(st, v1, record=False, now=now) is v1)
    os.environ['KADIMA_SCALP_V2'] = 'live'
    scalp_v2._cfg.update(at=0.0, value=None)
    check('live merge returns the v2 row', scalp_v2.merge(st, v1, record=False, now=now).get('engine') == 'scalp_v2')
    scalp_v2._states['NIFTY'].trigger = None
    out = scalp_v2.merge(st, v1, record=False, now=now)
    check('live merge without a trigger blocks (v1 not dispatched)', not out['eligible'] and
          out['blocked_reasons'] == ['scalp_v2_no_trigger'])
    check('audit code', scalp_v2.audit_code(out) == 'SCALP_V2' and scalp_v2.audit_code(v1) == 'SCALP')
finally:
    for k, (m, f) in patches.items():
        setattr(m, k, saved[k])
    os.environ.pop('KADIMA_SCALP_V2', None)
    scalp_v2._cfg.update(at=0.0, value=None)

# ---------------------------------------------------------------- v2 exit ladder
L = scalp_v2.ladder
check('before +1R the stop stays at the initial stop', L(100, 104, 92, 2.0) == 92)
check('+1R moves the stop to breakeven + costs', abs(L(100, 108.5, 92, 2.0) - 101.0) < 1e-9)
check('+12% locks +6% (or the ATR trail if higher)', abs(L(100, 112, 92, 2.0) - 110.0) < 1e-9)
check('trail is peak - 1 x premium ATR after +12%', abs(L(100, 118, 92, 2.0) - 116.0) < 1e-9)
check('the ladder never moves the stop down', L(100, 104, 92, 2.0, previous=106.0) == 106.0)
pos = types.SimpleNamespace(signal_id=990001, kind='fast_momentum', entry_price=100.0, peak_price=100.0)
scalp_v2.note_dispatch(dict(engine='scalp_v2', entry=100.0, stop=92.0, stop_pct=8.0, prem_atr=2.0, episode='e1'), 990001)
check('v2 position is managed: initial stop 8% below the fill', scalp_v2.managed_stop(pos, 100.0)[:3] == (92.0, 92.0, False))
pos.peak_price = 113.0
base, eff, active, reason = scalp_v2.managed_stop(pos, 112.0)
check('managed stop ratchets with the peak', active and abs(eff - 111.0) < 1e-9 and reason is None)
check('+20% target exits', scalp_v2.managed_stop(pos, 120.5)[3] == 'scalp_v2_target_20pct')
check('a v1 scalp is not managed by v2', scalp_v2.managed_stop(types.SimpleNamespace(
      signal_id=990002, kind='fast_momentum', entry_price=100.0, peak_price=100.0), 100.0) is None)
check('a gamma position is not managed by v2', scalp_v2.managed_stop(types.SimpleNamespace(
      signal_id=990001, kind='gamma_blast', entry_price=100.0, peak_price=100.0), 100.0) is None)

# ---------------------------------------------------------------- half-booking + thesis exits
check('half of 4 lots = 2 lots', scalp_v2.half_quantity(260, 65) == 130)
check('half of 3 lots = 1 lot (2 stay)', scalp_v2.half_quantity(195, 65) == 65)
check('1 lot: no partial', scalp_v2.half_quantity(65, 65) == 0)
import kadima.execution_ledger as _el  # noqa: E402
sold = []
_el.pending = lambda sid=None: []
_el.submit = lambda pos, side, qty, price: sold.append((side, qty, price)) or {'ok': True, 'pending': True}
pp = types.SimpleNamespace(signal_id=990010, kind='fast_momentum', entry_price=100.0, peak_price=113.0,
                           remaining_qty=260, lot_size=65, status='open', symbol='X', index='NIFTY',
                           entry_ts=datetime(2026, 10, 21, 10, 0, tzinfo=IST), exchange='NFO', token='1')
scalp_v2.note_dispatch(dict(engine='scalp_v2', entry=100.0, stop=92.0, stop_pct=8.0, prem_atr=2.0,
                            episode='e2', spread_pct=0.4, direction='CE'), 990010)
os.environ['KADIMA_SCALP_V2_PARTIAL'] = 'shadow'; scalp_v2._cfg.update(at=0.0, value=None)
scalp_v2.maybe_partial(None, pp, 112.5)
check('shadow: no partial order sent', sold == [] and any(e == 'scalp_v2_partial_would_book' for e, _ in LOGS))
c = scalp_v2._conn()
with c:
    c.execute('DELETE FROM scalp_v2_plans WHERE signal_id=990010')
scalp_v2._plans.clear()
scalp_v2.note_dispatch(dict(engine='scalp_v2', entry=100.0, stop=92.0, stop_pct=8.0, prem_atr=2.0,
                            episode='e2', spread_pct=0.4, direction='CE'), 990010)
os.environ['KADIMA_SCALP_V2_PARTIAL'] = 'live'; scalp_v2._cfg.update(at=0.0, value=None)
scalp_v2.maybe_partial(None, pp, 111.0)
check('no partial below +12%', sold == [])
scalp_v2.maybe_partial(None, pp, 112.5)
check('live: sells half the lots once at +12%', sold == [('SELL', 130, round(112.5 * 0.975, 1))])
scalp_v2.maybe_partial(None, pp, 115.0)
check('half is booked only once', len(sold) == 1)
os.environ.pop('KADIMA_SCALP_V2_PARTIAL'); scalp_v2._cfg.update(at=0.0, value=None)
T = scalp_v2.thesis_reasons
check('premium lower high below prior low exits', T([(110, 105, 108), (107, 101, 103)], None) == ['scalp_v2_premium_lower_high'])
check('a higher high does not exit', T([(110, 105, 108), (112, 104, 109)], None) == [])
check('two closes below VWAP after a CE entry exits', T([], None, 22600, [22598, 22595], 'CE') == ['scalp_v2_vwap_flip'])
check('one close below VWAP does not', T([], None, 22600, [22605, 22595], 'CE') == [])
check('PE flips on closes above VWAP', T([], None, 22600, [22603, 22605], 'PE') == ['scalp_v2_vwap_flip'])
check('spread doubling twice exits', T([], None, spread_now=1.0, spread_entry=0.4, spread_strikes=2) == ['scalp_v2_spread_widened'])
check('spread doubling once does not', T([], None, spread_now=1.0, spread_entry=0.4, spread_strikes=1) == [])

# ---------------------------------------------------------------- index lane
from kadima import market_session_gate as g  # noqa: E402
check('index underlying uses the index lane', g.lane_for('NFO', 'NIFTY') == 'index')
check('stock underlying uses the equity lane', g.lane_for('NFO', 'RELIANCE') == 'equity')
check('MCX lane unchanged', g.lane_for('MCX', 'CRUDEOIL') == 'mcx')
os.environ['KADIMA_PRIORITY_TRADING'] = '1'
import kadima.priority_sessions as ps  # noqa: E402
ps.calendar_reason = lambda *a, **k: None
g.calendar_reason = lambda *a, **k: None
g.permit_reason = lambda *a, **k: None
g.BOOT = types.SimpleNamespace(read_text=lambda: 'boot')
g.read_json = lambda *a, **k: {'lanes': {'equity': False, 'index': True}}
check('index entry allowed when only the stock lane is closed', g.entry_block('NFO', underlying='NIFTY') is None)
check('stock entry still blocked by its own lane', g.entry_block('NFO', underlying='RELIANCE') == 'lane_permit_not_granted')
g.read_json = lambda *a, **k: {'lanes': {'equity': False}}
check('old issuer without an index lane: unchanged behaviour', g.entry_block('NFO', underlying='NIFTY') == 'lane_permit_not_granted')
g.read_json = lambda *a, **k: {'lanes': {'equity': True}}
check('old issuer, equity open: index allowed as before', g.entry_block('NFO', underlying='NIFTY') is None)
del os.environ['KADIMA_PRIORITY_TRADING']

# ---------------------------------------------------------------- threshold + CSV
src = open(os.path.join(os.path.dirname(scalp_v2.__file__), 'celestial.py')).read()
check('threshold 60 allowed', 'THRESHOLDS = (60, 65, 70, 75, 80, 85, 90)' in src)
from kadima import trade_audit  # noqa: E402
names = [n for n, _ in trade_audit.CSV_COLUMNS]
check('CSV has Stage and Blocked by at the end', names[-2:] == ['Stage', 'Blocked by'])
check('Blocked by joins reasons in one cell',
      trade_audit.csv_value({'blocked_reasons': ['auto_paused', 'below_threshold']}, 'blocked_reasons')
      == 'auto_paused | below_threshold')
check('Scalping v2 is a known Trade Audit strategy', trade_audit.strategy_code('SCALP_V2') == 'SCALP_V2')

print(f'\n{len(FAILS)} failed' if FAILS else '\nall passed')
sys.exit(1 if FAILS else 0)
