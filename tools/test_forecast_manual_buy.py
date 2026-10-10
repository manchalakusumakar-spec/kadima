"""Gate tests for auto_executor.forecast_manual_buy(). Broker calls are stubbed.

    python3 tools/test_forecast_manual_buy.py
"""
import os
import sys
import tempfile
import time
from types import SimpleNamespace

os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima_fmb_')
sys.path.insert(0, '.')

from kadima import auto_executor as AE  # noqa: E402

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def setup(**over):
    ex = AE.AutoExecutor()
    ex._reconcile_blocked = False
    submitted = []
    env = dict(market=True, cutoff=False, kill=False, pnl=0.0, ltp=100.0, lot=65, afford=5,
               structure=None)
    env.update(over)
    AE.in_market_hours = lambda *a: env['market']
    AE.past_signal_cutoff = lambda *a: env['cutoff']
    AE.killswitch.engaged = lambda: env['kill']
    AE.exit_manager.realized_pnl_today = lambda: env['pnl']
    AE._poll_ltp = lambda exch, toks: {toks[0]: env['ltp']} if env['ltp'] else {}
    AE.get_lot_size = lambda sym, tok: env['lot']
    AE.capital_guard.broker_available_funds = lambda: 500000.0
    from kadima import portfolio_capacity, storage, execution_ledger
    portfolio_capacity.affordable_lots = lambda *a, **k: SimpleNamespace(
        lots=min(k['requested_lots'], env['afford']), reason='cash_headroom')
    portfolio_capacity.entry_block = lambda *a, **k: None
    execution_ledger.pending = lambda *a: []

    def submit(pos, side, qty, price):
        submitted.append((pos, side, qty, price))
        return {'ok': True, 'reason': 'accepted', 'order_id': 'X1'}
    AE.execution_ledger.submit = submit
    ex._structure = lambda chain, d, spot: env['structure']
    quote = SimpleNamespace(strike=25150.0, opt_type='CE', token='111', symbol='NIFTY25150CE', ltp=100.0)
    chain = SimpleNamespace(
        quotes={'111': quote}, atm=25100, _last_poll=time.monotonic(), _spot_received_at=time.monotonic(),
        index=SimpleNamespace(opt_exchange='NFO', strike_step=50),
        scrips=SimpleNamespace(strikes_around=lambda idx, atm, n: [{'token': '111'}]))
    return ex, chain, submitted


ex, chain, sub = setup()
r = ex.forecast_manual_buy('NIFTY', 'CE', 25150.0, 2, chain, 25110.0)
check('valid click submits one BUY', r['ok'] and len(sub) == 1 and sub[0][1] == 'BUY')
pos, _, qty, price = sub[0]
check('quantity = lots x lot size', qty == 130)
check('LIMIT priced from a fresh quote with the usual buffer', price == round(100.0 * 1.025, 1))
check('fallback stop 30% below when no swing exists', abs(pos.structural_stop - 70.0) < 1e-9)
check('position tagged as forecast_manual and monitored', pos.kind == 'forecast_manual'
      and str(pos.signal_id) in ex.state.positions)

for label, over, args, reason in (
        ('outside market hours', dict(market=False), (2,), 'outside_market_hours'),
        ('past signal cutoff', dict(cutoff=True), (2,), 'past_signal_cutoff'),
        ('kill switch', dict(kill=True), (2,), 'killswitch_engaged'),
        ('daily loss limit', dict(pnl=-20000.0), (2,), 'daily_loss_limit_breached'),
        ('no fresh quote', dict(ltp=None), (2,), 'fresh_entry_quote_unavailable'),
        ('more lots than affordable', dict(afford=1), (2,), 'only_1_lots_affordable'),
        ('lots above maximum', dict(), (99,), 'lots_must_be'),
        ('zero lots', dict(), (0,), 'lots_must_be')):
    ex, chain, sub = setup(**over)
    r = ex.forecast_manual_buy('NIFTY', 'CE', 25150.0, args[0], chain, 25110.0)
    check(f'blocked: {label}', not r['ok'] and r['reason'].startswith(reason) and not sub)

ex, chain, sub = setup()
r = ex.forecast_manual_buy('NIFTY', 'PE', 25150.0, 1, chain, 25110.0)
check('strike/side not in ladder refused', not r['ok'] and r['reason'] == 'strike_not_in_tracked_ladder')

ex, chain, sub = setup()
ex._reconcile_blocked = True
r = ex.forecast_manual_buy('NIFTY', 'CE', 25150.0, 1, chain, 25110.0)
check('refused until broker reconciliation completes', not r['ok'] and not sub)

ex, chain, sub = setup()
chain._last_poll = time.monotonic() - 120
r = ex.forecast_manual_buy('NIFTY', 'CE', 25150.0, 1, chain, 25110.0)
check('refused on a stale option chain', r['reason'] == 'option_chain_stale' and not sub)

AE.config.FORECAST_MANUAL_TRADE_ENABLED = False
ex, chain, sub = setup()
r = ex.forecast_manual_buy('NIFTY', 'CE', 25150.0, 1, chain, 25110.0)
check('config switch turns the buttons off', r['reason'] == 'forecast_manual_trade_disabled' and not sub)

print('\n%d failure(s)' % len(FAILS))
sys.exit(1 if FAILS else 0)
