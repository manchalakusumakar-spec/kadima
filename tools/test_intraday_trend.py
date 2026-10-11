"""Offline tests for the INTRADAY TREND v1 lane. No broker, no network, no AWS.

Software regressions with fixture candles/quotes: they are NOT a market replay
and say nothing about profitability.

Run:  KADIMA_DATA_DIR=/tmp/kadima-test python tools/test_intraday_trend.py
Also runs tools/parity_scenarios.py against the integration baseline (exported
with `git archive`) and this tree, and requires identical output.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp(prefix='kadima-test-'))
os.environ['HOME'] = os.environ['KADIMA_DATA_DIR']
for k in ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_INTRADAY_TREND_LIVE', 'KADIMA_SESSION_AUTOTRADE'):
    os.environ.pop(k, None)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)


def _deny(*a, **k):
    raise AssertionError('network forbidden')


socket.socket.connect = _deny
socket.create_connection = _deny

from kadima import (angel_orders, auto_executor, capital_guard, execution_ledger,  # noqa: E402
                    exit_manager, instrument_master, intraday_trend as it, risk_budget, storage)
from kadima.config import IST, INDICES  # noqa: E402
from kadima.greeks import bs_price  # noqa: E402
from kadima.indicators import Snapshot  # noqa: E402
from kadima.option_chain import OptionQuote  # noqa: E402

FAILED = []
BASELINE = 'de33ef36a3b8e1da64f5a24fe64e1b08f0a0de9b'
DAY = datetime(2026, 10, 12, tzinfo=IST)   # Monday


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def bar(h, m, o, hi, lo, c):
    return dict(start_ts=at(h, m).isoformat(), open=o, high=hi, low=lo, close=c, volume=1000, closed=1)


def mirror_rows(rows, axis=50000.0):
    return [dict(r, open=axis - r['open'], high=axis - r['low'], low=axis - r['high'], close=axis - r['close'])
            for r in rows]


def snap_ce():
    return Snapshot(close=25060, vwap=25030.0, ema_fast=25040.0, ema_slow=25020.0, rsi=60.0)


def snap_pe():
    return Snapshot(close=24940, vwap=24970.0, ema_fast=24960.0, ema_slow=24980.0, rsi=40.0)


OPENING = [bar(9, 15, 25000, 25030, 24990, 25020), bar(9, 20, 25020, 25040, 25010, 25035),
           bar(9, 25, 25035, 25045, 25020, 25030)]


def opening_rows(close=25065, high=25070):
    return OPENING + [bar(9, 30, 25030, high, 25028, close)]


# ------------------------------------------------------------------ setups
def test_opening_continuation():
    print('setup: opening continuation')
    now = at(9, 35, 5)
    s, ev = it.detect(opening_rows(), 25066, snap_ce(), now - timedelta(seconds=60), now)
    check('CE opening continuation detected', s and s['family'] == 'opening_continuation' and s['direction'] == 'CE')
    check('stop is the breakout bar low', s and s['spot_stop'] == 25028)
    check('decision timestamp is the bar close', s and s['decided_at'] == at(9, 35).isoformat())
    s2, _ = it.detect(opening_rows() + [bar(9, 35, 25065, 25200, 25060, 25190)], 25066, snap_ce(),
                      now - timedelta(seconds=60), now)
    check('a bar still forming at the decision time is ignored', s2 == s)
    sp, _ = it.detect(mirror_rows(opening_rows()), 50000 - 25066, snap_pe(), now - timedelta(seconds=60), now)
    check('PE mirror detected symmetrically', sp and sp['direction'] == 'PE'
          and sp['spot_stop'] == 50000 - 25028 and sp['family'] == 'opening_continuation')


def test_opening_rejections():
    print('setup: opening rejections')
    now = at(9, 35, 5)
    tech = now - timedelta(seconds=60)
    s, ev = it.detect(opening_rows(close=25040, high=25070), 25041, snap_ce(), tech, now)
    check('failed breakout (pierced, closed inside) is not a setup',
          s is None and ev['opening_continuation'] == 'no_close_beyond_opening_range')
    s, ev = it.detect(opening_rows(close=25100, high=25105), 25101, snap_ce(), tech, now)
    check('extended breakout rejected', s is None and ev['opening_continuation'] == 'breakout_extended')
    misaligned = Snapshot(close=25060, vwap=25030.0, ema_fast=25040.0, ema_slow=25020.0, rsi=45.0)
    s, ev = it.detect(opening_rows(), 25066, misaligned, tech, now)
    check('trend not aligned rejected with the failing factor',
          s is None and ev['opening_continuation'] == 'trend_not_aligned:rsi')
    s, ev = it.detect(opening_rows(), 25040, snap_ce(), tech, now)
    check('breakout already undone at decision rejected',
          s is None and ev['opening_continuation'] == 'breakout_invalidated_at_decision')
    s, ev = it.detect(opening_rows(), 25066, snap_ce(), now - timedelta(seconds=600), now)
    check('stale trend snapshot rejected', s is None and ev['opening_continuation'] == 'trend_snapshot_stale')
    for label, rows, when, reason in (
            ('stale candles', opening_rows(), at(9, 50, 5), 'stale_candles'),
            ('before 09:30 decision', OPENING, at(9, 30, 5), 'before_first_decision'),
            ('no bars', [], now, 'candle_warmup')):
        try:
            it.detect(rows, 25066, snap_ce(), when - timedelta(seconds=30), when)
            check(label, False)
        except ValueError as exc:
            check(f'{label} -> {reason}', str(exc) == reason)


def trend_rows(pullback=True):
    rows, price, t = [], 25000.0, at(9, 15)
    for i in range(12):               # steady rise 09:15 .. 10:10
        rows.append(dict(start_ts=t.isoformat(), open=price, high=price + 22, low=price - 4,
                         close=price + 18, volume=1000, closed=1))
        price += 18
        t += timedelta(minutes=5)
    if pullback:                      # 10:15 .. 10:25 shallow dip that touches EMA9
        for drop in (12, 14, 4):
            rows.append(dict(start_ts=t.isoformat(), open=price, high=price + 4, low=price - drop - 30,
                             close=price - drop, volume=1000, closed=1))
            price -= drop
            t += timedelta(minutes=5)
    rows.append(dict(start_ts=t.isoformat(), open=price, high=price + 30, low=price - 2,
                     close=price + 26, volume=1000, closed=1))   # 10:30 recovery
    return rows, price + 26, t + timedelta(minutes=5)


def test_pullback_recovery():
    print('setup: pullback recovery')
    rows, close, bar_close = trend_rows()
    now = bar_close + timedelta(seconds=5)
    snap = Snapshot(close=close, vwap=close - 80, ema_fast=close - 20, ema_slow=close - 60, rsi=62.0)
    s, ev = it.detect(rows, close + 1, snap, now - timedelta(seconds=60), now)
    check('CE pullback recovery detected', s and s['family'] == 'pullback_recovery' and s['direction'] == 'CE')
    check('stop is the pullback low', s and s['spot_stop'] == min(r['low'] for r in rows[-4:-1]))
    snap_m = Snapshot(close=50000 - close, vwap=50000 - close + 80, ema_fast=50000 - close + 20,
                      ema_slow=50000 - close + 60, rsi=38.0)
    sp, _ = it.detect(mirror_rows(rows), 50000 - close - 1, snap_m, now - timedelta(seconds=60), now)
    check('PE pullback recovery symmetric', sp and sp['family'] == 'pullback_recovery' and sp['direction'] == 'PE')
    rows2, close2, bar_close2 = trend_rows(pullback=False)
    now2 = bar_close2 + timedelta(seconds=5)
    snap2 = Snapshot(close=close2, vwap=close2 - 80, ema_fast=close2 - 20, ema_slow=close2 - 60, rsi=62.0)
    s2, ev2 = it.detect(rows2, close2 + 1, snap2, now2 - timedelta(seconds=60), now2)
    check('no pullback -> no pullback setup', s2 is None and ev2['pullback_recovery'] == 'no_shallow_pullback_to_ema')


def afternoon_rows(close=25125):
    rows = [bar(12, m, 25090, 25110, 25075, 25095) for m in range(0, 60, 5)]
    rows += [bar(13, 0, 25095, close + 5, 25092, close)]
    return rows


def test_afternoon_breakout():
    print('setup: afternoon breakout')
    now = at(13, 5, 5)
    snap = Snapshot(close=25125, vwap=25090.0, ema_fast=25100.0, ema_slow=25080.0, rsi=58.0)
    s, ev = it.detect(afternoon_rows(), 25126, snap, now - timedelta(seconds=60), now)
    check('CE afternoon breakout detected', s and s['family'] == 'afternoon_breakout' and s['level'] == 25110)
    s, ev = it.detect(afternoon_rows(close=25105), 25106, snap, now - timedelta(seconds=60), now)
    check('close inside the consolidation -> none',
          s is None and ev['afternoon_breakout'] == 'no_close_beyond_consolidation')
    late = [dict(r, start_ts=(datetime.fromisoformat(r['start_ts']) + timedelta(hours=1, minutes=35)).isoformat())
            for r in afternoon_rows()]
    try:
        it.detect(late, 25126, snap, at(14, 40, 5) - timedelta(seconds=60), at(14, 40, 5))
        check('no new setup at/after 14:30', False)
    except ValueError as exc:
        check('no new setup at/after 14:30', str(exc) == 'after_last_entry')


# ------------------------------------------------------------------ admission
SPOT, DAYS = 25066.0, 2.2


def option_chain(spread=0.2, first_spread=None, wide_token=None):
    quotes = {}
    for strike in (24950, 25000, 25050, 25100, 25150):
        for opt in ('CE', 'PE'):
            prem = round(bs_price(SPOT, strike, DAYS, 0.13, opt), 2)
            sp = first_spread if (first_spread and f'{opt}{strike}' == wide_token) else spread
            quotes[f'{opt}{strike}'] = OptionQuote(
                token=f'{opt}{strike}', symbol=f'NIFTY14OCT26{strike}{opt}', strike=float(strike), opt_type=opt,
                ltp=prem, oi=300000, oi_change=1000, volume=50000, bid=round(prem - sp / 2, 2),
                ask=round(prem + sp / 2, 2))
    mono = time.monotonic()
    chain = NS(index=INDICES['NIFTY'], quotes=quotes, atm=25050, _last_poll=mono, _spot_received_at=mono,
               _alignment_at=mono, _expiry='14OCT2026',
               scrips=NS(nearest_expiry=lambda idx: '14OCT2026',
                         strikes_around=lambda idx, atm, n, expiry=None: [
                             dict(token=k, symbol=q.symbol) for k, q in quotes.items()]))
    chain.walls = lambda: {'CE': [], 'PE': []}
    return chain


def state(now, chain=None):
    return NS(name='NIFTY', spot=SPOT, chain=chain or option_chain(), snap=snap_ce(),
              last_updated=now - timedelta(seconds=2), technical_at=now - timedelta(seconds=60))


def pending_setup(now, bar_start=None):
    bar_start = bar_start or (now - timedelta(minutes=5, seconds=30)).replace(second=0, microsecond=0)
    return dict(underlying='NIFTY', strategy=it.STRATEGY, policy_id=it.POLICY_ID, ts=now.isoformat(),
                stage='SETUP_OBSERVED', family='opening_continuation', direction='CE', level=25045.0,
                spot_stop=25028.0, bar_start=bar_start.isoformat(),
                decided_at=(bar_start + timedelta(minutes=5)).isoformat(), avg_range=30.0,
                episode='IT:NIFTY:opening_continuation:CE:' + bar_start.isoformat(),
                blocked_reasons=[], evaluations={})


def lane_with(now, **kw):
    lane = it.Lane()
    lane.pending['NIFTY'] = pending_setup(now, **kw)
    return lane


def test_contract_selection_and_gates():
    print('admission: contract selection, live gates, freshness')
    now = datetime.now(IST)
    lane = lane_with(now)
    row = lane.prepare(state(now), {}, now, claims={})
    check('default is observation only', row['stage'] == 'SETUP_READY' and not row['eligible']
          and 'intraday_trend_observation_only' in row['blocked_reasons'])
    os.environ['KADIMA_INTRADAY_TREND_LIVE'] = '1'
    try:
        row = lane.prepare(state(now), {}, now, claims={})
        check('live opt-in makes a valid setup eligible', row['eligible'] and row['token'])
        check('ranked contract evidence recorded',
              [c['token'] for c in row['contracts'] if c['reason'] == 'SELECTED'] == [row['token']])
        check('exit policy declared before entry', row['exit_policy']['time_exit'] == '15:10'
              and row['exit_policy']['lots'] == 1)
        check('stop below entry and within policy', 0 < row['stop'] < row['entry']
              and (row['entry'] - row['stop']) / row['entry'] * 100 <= it.POLICY['max_stop_pct'])
        first = row['token']
        wide = lane_with(now).prepare(state(now, option_chain(first_spread=8.0, wide_token=first)), {}, now, claims={})
        reasons = {c['token']: c['reason'] for c in wide['contracts']}
        check('a rejected contract is recorded and another ranked one is selected',
              wide['eligible'] and wide['token'] != first and reasons.get(first, '').startswith('ranking:'))
        check('every ranked contract has a recorded reason',
              all(c.get('reason') for c in wide['contracts']) and len(wide['contracts']) >= 3)
        stale = option_chain()
        stale._last_poll -= 31
        row = lane_with(now).prepare(state(now, stale), {}, now, claims={})
        check('stale option chain blocks and keeps the setup pending',
              'option_chain_stale' in row['blocked_reasons'] and not row['eligible'])
        st = state(now)
        st.last_updated = now - timedelta(seconds=45)
        row = lane_with(now).prepare(st, {}, now, claims={})
        check('stale spot blocks', 'underlying_quote_stale' in row['blocked_reasons'])
        st = state(now)
        st.spot = 25040.0
        lane = lane_with(now)
        row = lane.prepare(st, {}, now, claims={})
        check('breakout undone before entry -> failed and recorded',
              'breakout_failed_before_entry' in row['blocked_reasons'] and 'NIFTY' not in lane.pending)
        old = now - timedelta(minutes=12)
        lane = lane_with(now, bar_start=old.replace(second=0, microsecond=0))
        row = lane.prepare(state(now), {}, now, claims={})
        check('setup not admitted by the next bar expires with a record',
              'setup_expired_next_bar' in row['blocked_reasons'] and 'NIFTY' not in lane.pending)
        stages = [d['record_stage'] for d in it.recent_decisions(20)]
        check('expired and failed setups have outcome records', 'SETUP_EXPIRED' in stages and 'SETUP_FAILED' in stages)
    finally:
        os.environ.pop('KADIMA_INTRADAY_TREND_LIVE', None)


def test_strike_claims():
    print('strike priority: intraday steers clear of Gamma / scalping / held strikes')
    os.environ['KADIMA_INTRADAY_TREND_LIVE'] = '1'
    try:
        now = datetime.now(IST)
        base = lane_with(now).prepare(state(now), {}, now, claims={})
        first = base['token']
        for reason in ('strike_claimed_by_GAMMA', 'strike_claimed_by_SCALPING', 'held_by_GAMMA', 'held_by_CAS'):
            row = lane_with(now).prepare(state(now), {}, now, claims={('NFO', first): reason})
            ev = {c['token']: c['reason'] for c in row['contracts']}
            check(f'{reason}: intraday takes its next-ranked contract',
                  row['eligible'] and row['token'] != first and ev.get(first) == reason)
        everything = {('NFO', q): 'strike_claimed_by_GAMMA' for q in state(now).chain.quotes}
        row = lane_with(now).prepare(state(now), {}, now, claims=everything)
        check('all strikes claimed -> no intraday entry, reason recorded',
              not row['eligible'] and 'no_contract_meets_intraday_policy' in row['blocked_reasons'])
        row = lane_with(now).prepare(state(now), {}, now, claims=None)
        check('unreadable claims fail closed', not row['eligible']
              and 'strike_claims_unavailable' in row['blocked_reasons'])

        ex = auto_executor.AutoExecutor()
        ex.state.positions['5'] = position('gamma_blast', sid=5, token='CE25100')
        ex.state.positions['6'] = position('fast_momentum', sid=6, token='CE25150', status='closed')
        ex.fast_rows = {'NIFTY': dict(eligible=True, token='CE25000', exchange='NFO'),
                        'BANKNIFTY': dict(eligible=False, token='CE99', exchange='NFO')}
        claims = it.strike_claims(ex)
        check('held Gamma position is claimed', claims.get(('NFO', 'CE25100')) == 'held_by_GAMMA')
        check('closed position is not a claim', ('NFO', 'CE25150') not in claims)
        check('eligible scalping selection is claimed', claims.get(('NFO', 'CE25000')) == 'strike_claimed_by_SCALPING')
        check('ineligible scalping row is not a claim', ('NFO', 'CE99') not in claims)
        os.environ['KADIMA_CELESTIAL_ENABLED'] = '1'
        from kadima import celestial
        real_top20 = celestial.top20
        celestial.top20 = lambda *a, **k: [dict(eligible=True, exchange='NFO', token='CE24950'),
                                           dict(eligible=False, exchange='NFO', token='CE24900')]
        try:
            claims = it.strike_claims(ex)
            check('eligible Celestial (Gamma) row is claimed', claims.get(('NFO', 'CE24950')) == 'strike_claimed_by_GAMMA')
            check('ineligible Celestial row is not a claim', ('NFO', 'CE24900') not in claims)

            def broken(*a, **k):
                raise ValueError('Invalid persistent config; entry disabled')
            celestial.top20 = broken
            check('Celestial read failure -> claims None (fail closed)', it.strike_claims(ex) is None)
        finally:
            celestial.top20 = real_top20
            os.environ.pop('KADIMA_CELESTIAL_ENABLED', None)
    finally:
        os.environ.pop('KADIMA_INTRADAY_TREND_LIVE', None)


def test_claim_appears_before_submission():
    print('strike priority: a claim arriving just before submission moves intraday off that strike')
    install_broker_mocks()
    os.environ['KADIMA_INTRADAY_TREND_LIVE'] = '1'
    try:
        now = datetime.now(IST)
        chain = option_chain()
        st = state(now, chain)
        auto_executor._poll_ltp = lambda exch, toks: {t: chain.quotes[t].ltp for t in toks}
        lane = lane_with(now)
        ex = auto_executor.AutoExecutor()
        ex._reconcile_blocked, ex._positions_verified = False, True
        row = lane.prepare(st, ex.state.positions, now, claims=it.strike_claims(ex))
        reset_books()
        ORDERS.clear()
        ex.fast_rows = {'NIFTY': dict(eligible=True, token=row['token'], exchange='NFO')}  # scalping claims it now
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend',
                                    it.signal_id(dict(row, episode=row['episode'] + ':late')), 'TREND', 0.0,
                                    trend_context=lambda: lane.revalidate(st, ex))
        check('no intraday order on the newly claimed strike', all(o[1] != row['token'] for o in ORDERS))
        check('intraday re-ranks to its next contract instead', res['ok'] and len(ORDERS) == 1
              and ORDERS[0][1] == lane.rows['NIFTY']['token'] != row['token'])
        lane.settle('NIFTY', row, res)
        last = it.recent_decisions(1)[0]
        check('dispatch record logs the contract actually submitted',
              last['record_stage'] == 'DISPATCH' and last['submitted']['token'] == ORDERS[0][1])
    finally:
        os.environ.pop('KADIMA_INTRADAY_TREND_LIVE', None)


def position(kind, status='open', exit_ts=None, sid=1, index='NIFTY', **kw):
    base = dict(signal_id=sid, kind=kind, index=index, direction='CE', symbol='NIFTY14OCT2625050CE',
                token='CE25050', exchange='NFO', entry_price=100.0, entry_ts=datetime.now(IST) - timedelta(minutes=10),
                lots=1, lot_size=65, remaining_qty=65 if status != 'closed' else 0, status=status,
                peak_price=100.0, structural_stop=80.0, effective_stop=80.0, exit_ts=exit_ts)
    base.update(kw)
    return auto_executor.Position(**base)


def test_reentry_and_episodes():
    print('re-entry: confirmed exit, genuinely new setup, immutable episodes')
    now = datetime.now(IST)
    setup = pending_setup(now)
    check('open intraday_trend position blocks a new entry on that underlying',
          it.reentry_block({'1': position(it.STRATEGY)}, 'NIFTY', setup, now) == 'previous_intraday_trend_exit_unconfirmed')
    check('an open position of another lane does not count as an intraday exit',
          it.reentry_block({'1': position('gamma_blast')}, 'NIFTY', setup, now) is None)
    after = datetime.fromisoformat(setup['bar_start']) + timedelta(minutes=1)
    check('setup that started before the previous exit is not new',
          it.reentry_block({'1': position(it.STRATEGY, 'closed', exit_ts=after)}, 'NIFTY', setup, now)
          == 'setup_not_new_since_previous_exit')
    before = datetime.fromisoformat(setup['bar_start']) - timedelta(minutes=1)
    check('fresh setup after a confirmed exit is allowed',
          it.reentry_block({'1': position(it.STRATEGY, 'closed', exit_ts=before)}, 'NIFTY', setup, now) is None)
    row = dict(setup, ts=now.isoformat())
    a, b = it.signal_id(row), it.signal_id(row)
    c = it.signal_id(dict(row, episode=row['episode'] + ':other'))
    check('same episode -> same signal id (restart-safe)', a == b)
    check('new episode -> new signal id', c != a)


def test_exit_policy():
    print('exit policy: intraday_trend only')
    now = datetime.now(IST).replace(hour=11, minute=0)
    p = position(it.STRATEGY, entry_ts=now - timedelta(minutes=46), peak_price=103.0)
    check('no progress after 45 minutes exits', it.exit_reason(p, now) == 'intraday_trend_no_progress_45min')
    p = position(it.STRATEGY, entry_ts=now - timedelta(minutes=46), peak_price=106.0)
    check('progress >= 5% keeps holding', it.exit_reason(p, now) is None)
    p = position(it.STRATEGY, entry_ts=now.replace(hour=15, minute=0))
    check('time exit at 15:10', it.exit_reason(p, now.replace(hour=15, minute=10)) == 'intraday_trend_time_exit')
    check('before 15:10 no time exit', it.exit_reason(p, now.replace(hour=15, minute=9)) is None)
    for kind in ('gamma_blast', 'fast_momentum', 'bull_shield', 'cas', 'cas_blast', 'gap_and_go'):
        q = position(kind, entry_ts=now - timedelta(minutes=50), peak_price=100.0)
        check(f'{kind} unaffected by the intraday exit policy',
              it.exit_reason(q, now.replace(hour=15, minute=12)) is None)


# ------------------------------------------------------------------ executor (broker-mocked)
ORDERS = []


def install_broker_mocks():
    auto_executor.in_market_hours = lambda *a: True
    auto_executor.past_signal_cutoff = lambda *a: False
    auto_executor.alerts.enqueue = lambda *a, **k: None
    auto_executor.killswitch.engaged = lambda: False
    exit_manager.realized_pnl_today = lambda: 0.0
    capital_guard.broker_available_funds = lambda: 200_000.0
    for m in (auto_executor, risk_budget, instrument_master):
        m.get_lot_size = lambda symbol, token: 65

    def place(**kw):
        ORDERS.append((kw['transactiontype'], kw['symboltoken'], kw['quantity'], kw['price']))
        return angel_orders.OrderResult(True, f'OID{len(ORDERS)}', 'ok')
    angel_orders.place_limit_order = place


def reset_books():
    c = execution_ledger.connection()
    with c:
        for table in ('execution_orders', 'execution_fills', 'positions'):
            c.execute(f'DELETE FROM {table}')


def test_executor_dispatch():
    print('executor: intraday_trend through the shared entry path (broker mocked)')
    install_broker_mocks()
    os.environ['KADIMA_INTRADAY_TREND_LIVE'] = '1'
    try:
        now = datetime.now(IST)
        chain = option_chain()
        st = state(now, chain)
        auto_executor._poll_ltp = lambda exch, toks: {t: chain.quotes[t].ltp for t in toks}
        lane = lane_with(now)
        row = lane.prepare(st, {}, now, claims={})
        ex = auto_executor.AutoExecutor()
        ex._reconcile_blocked, ex._positions_verified = False, True
        reset_books()
        ORDERS.clear()
        sid = it.signal_id(row)
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid,
                                    'TREND', 0.0, trend_context=lambda: lane.revalidate(st, ex))
        check('eligible setup reaches the shared ledger as one BUY', res.get('ok') and len(ORDERS) == 1
              and ORDERS[0][0] == 'BUY')
        check('exactly 1 lot of the lane-selected contract', ORDERS and ORDERS[0][1] == row['token']
              and ORDERS[0][2] == 65)
        cap = int(row['entry'] * 1.005 * 20) / 20
        check('limit price never above entry + 0.5%', ORDERS and ORDERS[0][3] <= cap + 1e-9)
        pos = ex.state.positions.get(str(sid))
        check('position carries the intraday_trend identity and lane stop',
              pos and pos.kind == 'intraday_trend' and pos.structural_stop > 0)
        res2 = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid,
                                     'TREND', 0.0, trend_context=lambda: lane.revalidate(st, ex))
        check('same episode cannot be submitted twice', not res2['ok'] and len(ORDERS) == 1)

        reset_books()
        ORDERS.clear()
        ex = auto_executor.AutoExecutor()
        ex._reconcile_blocked, ex._positions_verified = False, True
        os.environ.pop('KADIMA_INTRADAY_TREND_LIVE', None)
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid + 1,
                                    'TREND', 0.0, trend_context=lambda: dict(row))
        check('observation-only lane cannot reach the broker',
              res['reason'] == 'intraday_trend_context_or_live_gate_blocked' and not ORDERS)
        os.environ['KADIMA_INTRADAY_TREND_LIVE'] = '1'
        calls = {'n': 0}

        def changing():
            calls['n'] += 1
            return dict(row, ts=datetime.now(IST).isoformat()) if calls['n'] == 1 else \
                dict(row, ts=datetime.now(IST).isoformat(), episode='other')
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid + 2,
                                    'TREND', 0.0, trend_context=changing)
        check('setup changed before submission is rejected',
              res['reason'] == 'intraday_trend_invalidated_before_submission' and not ORDERS)
        auto_executor.killswitch.engaged = lambda: True
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid + 3,
                                    'TREND', 0.0, trend_context=lambda: dict(row, ts=datetime.now(IST).isoformat()))
        check('kill switch still blocks the intraday lane', res['reason'] == 'killswitch_engaged' and not ORDERS)
        auto_executor.killswitch.engaged = lambda: False
        ex._reconcile_blocked = True
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid + 4,
                                    'TREND', 0.0, trend_context=lambda: dict(row, ts=datetime.now(IST).isoformat()))
        check('unfinished reconciliation still blocks', res['reason'] == 'startup_reconcile_not_complete')
        ex._reconcile_blocked = False
        exit_manager.realized_pnl_today = lambda: -20_000.0
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid + 5,
                                    'TREND', 0.0, trend_context=lambda: dict(row, ts=datetime.now(IST).isoformat()))
        check('daily loss limit still blocks', res['reason'] == 'daily_loss_limit_breached' and not ORDERS)
        exit_manager.realized_pnl_today = lambda: 0.0
        os.environ['KADIMA_SESSION_AUTOTRADE'] = '1'      # no permit files in the test sandbox
        res = ex.evaluate_and_enter('NIFTY', 'CE', chain, SPOT, st.snap, row['days'], 'intraday_trend', sid + 6,
                                    'TREND', 0.0, trend_context=lambda: dict(row, ts=datetime.now(IST).isoformat()))
        check('session permit still required', res['reason'] == 'session_schedule_unavailable' and not ORDERS)
        os.environ.pop('KADIMA_SESSION_AUTOTRADE', None)
    finally:
        os.environ.pop('KADIMA_INTRADAY_TREND_LIVE', None)


def test_executor_exit():
    print('executor: intraday_trend exit through shared check_exits')
    install_broker_mocks()
    reset_books()
    ORDERS.clear()
    ex = auto_executor.AutoExecutor()
    ex._reconcile_blocked, ex._positions_verified = False, True
    now = datetime.now(IST)
    p = position(it.STRATEGY, sid=777, entry_ts=now - timedelta(minutes=50), peak_price=101.0)
    ex.state.positions['777'] = p
    auto_executor._poll_ltp = lambda exch, toks: {t: 101.0 for t in toks}
    real = auto_executor.now_ist
    auto_executor.now_ist = lambda: now.replace(hour=11, minute=0)
    try:
        ex.check_exits()
    finally:
        auto_executor.now_ist = real
    check('no-progress exit sends one SELL via the shared ledger',
          p.exit_reason == 'intraday_trend_no_progress_45min' and ORDERS and ORDERS[0][0] == 'SELL')


# ------------------------------------------------------------------ engine hooks
def test_engine_hooks():
    print('engine: hooks are isolated and only touch the intraday lane')
    from kadima import engine as eng
    calls, logged = [], []
    real_log = eng.audit.log
    eng.audit.log = lambda event, **kw: logged.append((event, kw))
    try:
        class Boom:
            pending = {'NIFTY': {}}
            rows = {}

            def observe_close(self, st):
                raise RuntimeError('lane failure')

            def prepare(self, st, positions, claims=None):
                raise RuntimeError('lane failure')
        fake = NS(trend_lane=Boom())
        st = NS(name='NIFTY')
        eng.Engine._observe_intraday_trend(fake, st)
        eng.Engine._admit_intraday_trend(fake, st)
        check('lane exceptions are contained and audited',
              [e for e, _ in logged].count('INTRADAY_TREND_LANE_ERROR') == 2)
        logged.clear()
        eng.Engine._observe_intraday_trend(fake, NS(name='SOMESTOCK'))
        check('non-index states are ignored', not logged)

        class Ready:
            pending = {'NIFTY': {}}
            rows = {}

            def prepare(self, st, positions, claims=None):
                return dict(eligible=True, direction='CE', days=2.0, episode='IT:test', ts=datetime.now(IST).isoformat(),
                            underlying='NIFTY')

            def revalidate(self, st, executor):
                return None

            def settle(self, underlying, row, result):
                calls.append(('settle', result))
        real_eval = auto_executor.EXECUTOR.evaluate_and_enter
        auto_executor.EXECUTOR.evaluate_and_enter = lambda *a, **kw: (calls.append(('eval', a[6], kw.keys())) or
                                                                      {'ok': False, 'reason': 'stub'})
        try:
            eng.Engine._admit_intraday_trend(NS(trend_lane=Ready()), NS(name='NIFTY', chain=None, spot=1.0, snap=None))
        finally:
            auto_executor.EXECUTOR.evaluate_and_enter = real_eval
        check('admission dispatches kind intraday_trend with a trend_context',
              calls and calls[0][0] == 'eval' and calls[0][1] == 'intraday_trend' and 'trend_context' in calls[0][2])
        check('dispatch outcome is settled/recorded', ('settle', {'ok': False, 'reason': 'stub'}) in calls)
    finally:
        eng.audit.log = real_log


# ------------------------------------------------------------------ parity
def test_dispatch_parity():
    print('parity: Gamma / Shield / fast / gap / forecast / exits identical to the baseline')
    base = tempfile.mkdtemp(prefix='kadima-baseline-')
    try:
        archive = subprocess.run(['git', '-C', ROOT, 'archive', BASELINE], capture_output=True, check=True).stdout
        subprocess.run(['tar', '-x', '-C', base], input=archive, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        check(f'baseline {BASELINE[:7]} export available ({type(exc).__name__})', False)
        return
    script = os.path.join(ROOT, 'tools', 'parity_scenarios.py')
    outs = []
    for root in (base, ROOT):
        r = subprocess.run([sys.executable, script, '--root', root], capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, KADIMA_DATA_DIR=tempfile.mkdtemp(prefix='kadima-parity-')))
        outs.append(r.stdout if r.returncode == 0 else 'ERROR:' + r.stderr[-500:])
    check('both trees ran every scenario', all(not o.startswith('ERROR') for o in outs))
    same = outs[0] == outs[1]
    check('byte-identical scenario output (results, orders, diagnostics)', same)
    if not same:
        for a, b in zip(json.loads(outs[0]), json.loads(outs[1])):
            if a != b:
                print('    differs:', a['scenario'])
    data = json.loads(outs[1]) if outs[1] and not outs[1].startswith('ERROR') else []
    names = {d['scenario'] for d in data}
    check('scenario set covers gamma, celestial, shield, fast, gap, forecast and exits',
          {'gamma_legacy_ce_confirmed', 'gamma_celestial_candidate', 'bull_shield_without_evidence',
           'fast_context_valid_live_gate_open', 'gap_context_valid_live_gate_open', 'forecast_manual_buy',
           'exit_gamma_trail', 'exit_fast_timeout', 'exit_cas_kind_holding'} <= names)
    print(f'    ({len(data)} scenarios)')


if __name__ == '__main__':
    test_opening_continuation()
    test_opening_rejections()
    test_pullback_recovery()
    test_afternoon_breakout()
    test_contract_selection_and_gates()
    test_reentry_and_episodes()
    test_exit_policy()
    test_executor_dispatch()
    test_executor_exit()
    test_strike_claims()
    test_claim_appears_before_submission()
    test_engine_hooks()
    test_dispatch_parity()
    print('-' * 40)
    print('ALL PASS' if not FAILED else f'{len(FAILED)} FAILED: {FAILED}')
    sys.exit(1 if FAILED else 0)
