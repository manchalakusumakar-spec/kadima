"""Gamma / Shield / fast / gap / forecast / exit dispatch parity scenarios.

Runs deterministic, broker-mocked scenarios through the REAL AutoExecutor of the
source tree given by --root and prints canonical JSON (results, orders sent to
the stubbed broker, executor diagnostics). tools/test_intraday_trend.py runs it
against an exact export of the integration baseline and against the current
tree and requires byte-identical output. No network, broker or AWS access.

    python tools/parity_scenarios.py --root /path/to/source/tree
"""
import argparse
import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS


def _deny(*a, **k):
    raise AssertionError('network forbidden in parity scenarios')


socket.socket.connect = _deny
socket.create_connection = _deny

ap = argparse.ArgumentParser()
ap.add_argument('--root', required=True)
args = ap.parse_args()
os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima-parity-')
for k in ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_SESSION_AUTOTRADE', 'KADIMA_FAST_MOMENTUM_LIVE',
          'KADIMA_INTRADAY_TREND_LIVE', 'KADIMA_GAP_AND_GO_LIVE'):
    os.environ.pop(k, None)
os.environ['HOME'] = os.environ['KADIMA_DATA_DIR']      # capital_guard state file lives under ~
sys.path.insert(0, args.root)

from kadima import (angel_orders, auto_executor, capital_guard, celestial, config,  # noqa: E402
                    execution_ledger, exit_manager, fast_momentum, gap_and_go, instrument_master,
                    risk_budget, storage, utils)
from kadima.config import IST, INDICES  # noqa: E402
from kadima.indicators import Snapshot  # noqa: E402
from kadima.option_chain import OptionQuote  # noqa: E402
from kadima.greeks import bs_price  # noqa: E402

NOW = datetime(2026, 10, 12, 11, 0, 30, tzinfo=IST)     # a Monday, mid-session
CLOCK = {'now': NOW}
ORDERS, OUT = [], []


def now():
    return CLOCK['now']


for module in (auto_executor, utils, celestial, execution_ledger, exit_manager):
    if hasattr(module, 'now_ist'):
        module.now_ist = now
auto_executor.in_market_hours = lambda *a: True
auto_executor.past_signal_cutoff = lambda *a: False
auto_executor.alerts.enqueue = lambda *a, **k: None
exit_manager.realized_pnl_today = lambda: 0.0
capital_guard.broker_available_funds = lambda: 200_000.0
for m in (auto_executor, risk_budget, instrument_master):
    m.get_lot_size = lambda symbol, token: 65
LTP = {}
auto_executor._poll_ltp = lambda exchange, tokens: {t: LTP[t] for t in tokens if t in LTP}


class Broker:
    seq = 0


def place(**kw):
    Broker.seq += 1
    ORDERS.append({k: kw[k] for k in ('tradingsymbol', 'symboltoken', 'exchange', 'transactiontype',
                                        'quantity', 'price')})
    return angel_orders.OrderResult(True, f'OID{Broker.seq}', 'ok')


angel_orders.place_limit_order = place
angel_orders.cancel_order = lambda *a, **k: angel_orders.OrderResult(True, None, 'ok')
angel_orders.fetch_order_book = lambda: []
auto_executor.fetch_broker_positions = lambda: []

SPOT = 25010.0
DAYS = 2.2


def quotes(direction):
    out = {}
    for i, strike in enumerate((24950, 25000, 25050, 25100)):
        for opt in ('CE', 'PE'):
            token = f'{opt}{strike}'
            prem = round(bs_price(SPOT, strike, DAYS, 0.13, opt), 2)
            out[token] = OptionQuote(token=token, symbol=f'NIFTY14OCT26{strike}{opt}', strike=float(strike),
                                     opt_type=opt, ltp=prem, oi=300000, oi_change=1000, volume=50000,
                                     bid=round(prem - 0.1, 2), ask=round(prem + 0.1, 2))
            LTP[token] = prem
    return out


class Scrips:
    def nearest_expiry(self, index):
        return '14OCT2026'

    def strikes_around(self, index, atm, n, expiry=None):
        return [dict(token=f'{o}{s}', symbol=f'NIFTY14OCT26{s}{o}')
                for s in (24950, 25000, 25050, 25100) for o in ('CE', 'PE')]


def chain(direction='CE'):
    mono = time.monotonic()
    c = NS(index=INDICES['NIFTY'], quotes=quotes(direction), scrips=Scrips(), atm=25000,
           _last_poll=mono, _spot_received_at=mono, _alignment_at=mono, _expiry='14OCT2026')
    c.walls = lambda: {'CE': [], 'PE': []}
    return c


def bars(direction='CE'):
    """Fresh closed 5-min bars with a confirmed swing pivot for _structure()."""
    rows, start = [], NOW.replace(hour=10, minute=0, second=0)
    lows = [24960, 24950, 24930, 24945, 24965, 24975, 24985, 24990, 24995, 25000, 25002, 25004]
    for i, lo in enumerate(lows):
        t = start + timedelta(minutes=5 * i)
        if direction == 'PE':
            hi = 50000 - lo
            rows.append(dict(start_ts=t.isoformat(), open=hi - 10, high=hi, low=hi - 25, close=hi - 15,
                             volume=1000, closed=1))
        else:
            rows.append(dict(start_ts=t.isoformat(), open=lo + 10, high=lo + 25, low=lo, close=lo + 15,
                             volume=1000, closed=1))
    return rows


BARS = {'dir': 'CE'}
storage.recent_candles = lambda token, interval, limit=200: bars(BARS['dir'])


def snap(direction='CE'):
    if direction == 'CE':
        return Snapshot(close=SPOT, vwap=24990.0, ema_fast=25000.0, ema_slow=24980.0, rsi=62.0)
    return Snapshot(close=SPOT, vwap=25030.0, ema_fast=25010.0, ema_slow=25040.0, rsi=38.0)


def executor():
    ex = auto_executor.AutoExecutor()
    ex._reconcile_blocked = False
    ex._positions_verified = True
    return ex


def canonical(result):
    return json.loads(json.dumps(result, sort_keys=True, default=str))


def reset_books():
    """Each scenario starts from an empty ledger/position book (own DB rows only)."""
    c = execution_ledger.connection()
    with c:
        for table in ('execution_orders', 'execution_fills', 'positions'):
            c.execute(f'DELETE FROM {table}')


def run(name, fn):
    ORDERS.clear()
    reset_books()
    try:
        result = canonical(fn())
    except Exception as exc:                        # record, do not hide
        result = dict(exception=type(exc).__name__, message=str(exc)[:200])
    OUT.append(dict(scenario=name, result=result, orders=list(ORDERS)))


def enter(kind, direction='CE', sid=1, tier='CONFIRMED', score=80.0, **extra):
    BARS['dir'] = direction
    ex = extra.pop('ex', None) or executor()
    c = chain(direction)
    res = ex.evaluate_and_enter(index_name='NIFTY', direction=direction, chain=c, spot=SPOT,
                                snap=snap(direction), days_to_expiry=DAYS, kind=kind,
                                source_signal_id=sid, tier=tier, score=score, **extra)
    diag = [{k: v for k, v in d.items() if k != 'ts'} for d in ex.entry_diagnostics]
    return dict(result=res, diagnostics=diag,
                positions={k: dict(kind=p.kind, status=p.status, lots=p.lots, token=p.token,
                                   effective_stop=round(p.effective_stop or 0, 4))
                           for k, p in ex.state.positions.items()})


# ---------------------------------------------------------------- entry scenarios
run('gamma_legacy_ce_confirmed', lambda: enter('gamma_blast', 'CE', sid=101))
run('gamma_legacy_pe_confirmed', lambda: enter('gamma_blast', 'PE', sid=102))
def misaligned():
    BARS['dir'] = 'CE'
    return executor().evaluate_and_enter('NIFTY', 'CE', chain('CE'), SPOT, snap('PE'), DAYS,
                                         'gamma_blast', 103, 'CONFIRMED', 80.0)


run('gamma_legacy_misaligned', misaligned)
run('gamma_legacy_below_threshold', lambda: enter('gamma_blast', 'CE', sid=104, score=60.0))
run('gamma_duplicate_signal', lambda: (lambda ex: [enter('gamma_blast', 'CE', sid=105, ex=ex),
                                                   enter('gamma_blast', 'CE', sid=105, ex=ex)])(executor()))
run('bull_shield_without_evidence', lambda: enter('bull_shield', 'CE', sid=201, shield_context=lambda: None))
run('fast_context_missing', lambda: enter('fast_momentum', 'CE', sid=301, tier='EXPERIMENTAL',
                                          fast_context=lambda: None))
run('gap_context_missing', lambda: enter('gap_and_go', 'CE', sid=401, tier='EXPERIMENTAL',
                                         gap_context=lambda: None))


def fast_row():
    q = chain('CE').quotes['CE25000']
    return dict(eligible=True, strategy='fast_momentum', underlying='NIFTY', direction='CE',
                ts=NOW.isoformat(), score=80, token=q.token, symbol=q.symbol, exchange='NFO',
                spot_stop=24930.0, days=DAYS, entry=q.ask, episode='NIFTY:CE:fast')


def fast_enter():
    fast_momentum.live_blocks = lambda *a, **k: []
    row = fast_row()
    return enter('fast_momentum', 'CE', sid=302, tier='EXPERIMENTAL', fast_context=lambda: dict(row))


run('fast_context_valid_live_gate_open', fast_enter)


def gap_enter():
    gap_and_go.live_blocks = lambda *a, **k: []
    q = chain('CE').quotes['CE25000']
    row = dict(eligible=True, strategy='gap_and_go', underlying='NIFTY', direction='CE', ts=NOW.isoformat(),
               token=q.token, contract_symbol=q.symbol, exchange='NFO', spot_stop=24930.0, days=DAYS,
               entry=q.ask, episode='NIFTY:gap')
    return enter('gap_and_go', 'CE', sid=402, tier='EXPERIMENTAL', gap_context=lambda: dict(row))


run('gap_context_valid_live_gate_open', gap_enter)


def killswitch_on():
    auto_executor.killswitch.engaged = lambda: True
    try:
        return enter('gamma_blast', 'CE', sid=106)
    finally:
        auto_executor.killswitch.engaged = lambda: False


auto_executor.killswitch.engaged = lambda: False
run('gamma_killswitch_engaged', killswitch_on)


def celestial_candidate():
    os.environ['KADIMA_CELESTIAL_ENABLED'] = '1'
    try:
        q = chain('CE').quotes['CE25000']
        row = dict(id='NFO:' + q.token, underlying='NIFTY', direction='CE', eligible=True,
                   final_score=80.0, last_updated=NOW.isoformat())
        celestial.entry_candidate = lambda *a, **k: dict(row)
        celestial.read_config = lambda: dict(celestial.DEFAULTS, auto_trade_enabled=True, score_threshold=65)
        return enter('gamma_blast', 'CE', sid=107, tier='ACTIVE', candidate_id=row['id'])
    finally:
        os.environ.pop('KADIMA_CELESTIAL_ENABLED', None)


run('gamma_celestial_candidate', celestial_candidate)


def celestial_legacy_rejected():
    os.environ['KADIMA_CELESTIAL_ENABLED'] = '1'
    try:
        return enter('gamma_blast', 'CE', sid=108)
    finally:
        os.environ.pop('KADIMA_CELESTIAL_ENABLED', None)


run('gamma_celestial_without_candidate', celestial_legacy_rejected)


def forecast_buy():
    BARS['dir'] = 'CE'
    ex = executor()
    return ex.forecast_manual_buy('NIFTY', 'CE', 25000.0, 1, chain('CE'), SPOT)


run('forecast_manual_buy', forecast_buy)


# ---------------------------------------------------------------- exit scenarios
def position(kind, sid, entry=100.0, stop=80.0, minutes_ago=10, peak=None, token='CE25000'):
    return auto_executor.Position(
        signal_id=sid, kind=kind, index='NIFTY', direction='CE', symbol='NIFTY14OCT2625000CE',
        token=token, exchange='NFO', entry_price=entry, entry_ts=NOW - timedelta(minutes=minutes_ago),
        lots=1, lot_size=65, remaining_qty=65, status='open', peak_price=peak or entry,
        structural_stop=stop, effective_stop=stop)


def exits(kind, ltp, at=NOW, **kw):
    CLOCK['now'] = at
    try:
        ex = executor()
        pos = position(kind, 900 + len(OUT), **kw)
        ex.state.positions[str(pos.signal_id)] = pos
        LTP[pos.token] = ltp
        ex.check_exits()
        return dict(status=pos.status, exit_reason=pos.exit_reason,
                    effective_stop=round(pos.effective_stop, 4), trail=pos.trail_active)
    finally:
        CLOCK['now'] = NOW


run('exit_gamma_stop_breached', lambda: exits('gamma_blast', 75.0))
run('exit_gamma_holding', lambda: exits('gamma_blast', 104.0))
run('exit_gamma_trail', lambda: exits('gamma_blast', 112.0, peak=130.0))
run('exit_gamma_eod', lambda: exits('gamma_blast', 104.0, at=NOW.replace(hour=15, minute=39)))
run('exit_gamma_1515_no_time_exit', lambda: exits('gamma_blast', 104.0, at=NOW.replace(hour=15, minute=15)))
run('exit_fast_timeout', lambda: exits('fast_momentum', 101.0, minutes_ago=6))
run('exit_bull_shield_holding', lambda: exits('bull_shield', 103.0, minutes_ago=60))
run('exit_cas_kind_holding', lambda: exits('cas', 103.0, minutes_ago=60))
run('exit_mcx_kind_holding', lambda: exits('mcx_momentum', 103.0, minutes_ago=60))

print(json.dumps(OUT, sort_keys=True, indent=1, default=str))
