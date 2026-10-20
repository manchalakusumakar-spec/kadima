"""Offline tests: Gamma Blast priority (2026-10-20). KADIMA_DATA_DIR=$(mktemp -d) python tools/test_gamma_priority_20261020.py"""
import os, sys, tempfile, types
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kadima import gamma_priority as gp  # noqa: E402

FAILS, LOGS = [], []
gp.audit.log = lambda e, **k: LOGS.append((e, k))


def check(name, ok):
    print(('PASS ' if ok else 'FAIL ') + name)
    if not ok:
        FAILS.append(name)


def P(sid, kind, index, entry, ltp, qty=65):
    return types.SimpleNamespace(signal_id=sid, kind=kind, index=index, entry_price=entry, remaining_qty=qty,
                                 status='open', token=str(sid), symbol=f'S{sid}', _ltp=ltp)


price = lambda p: p._ltp
check('capital reasons are recognised', gp.blocking('portfolio_cash_headroom_exhausted') == 'capital'
      and gp.blocking('capital_guard_no_capital') == 'capital')
check('manual block is recognised', gp.blocking('manual_carryforward_same_underlying') == 'manual')
check('unrelated reasons never trigger exits', gp.blocking('below_threshold') is None
      and gp.blocking('counter_trend_bull_day') is None and gp.blocking('killswitch_engaged') is None)

book = [P(1, 'bull_shield', 'NIFTY', 100, 90), P(2, 'fast_momentum', 'NIFTY', 100, 110),
        P(3, 'fast_momentum', 'BANKNIFTY', 100, 95), P(4, 'cas', 'NIFTY', 10, 5),
        P(5, 'MANUAL_PROTECTED', 'NIFTY', 100, 80), P(6, 'gamma_blast', 'NIFTY', 100, 50)]
order = [x[0].signal_id for x in gp.candidates(book, 'NIFTY', 'capital', price, 0.0, 10000)]
check('order: scalps (weakest first) -> intraday -> CAS -> manual', order == [3, 2, 1, 4, 5])
check('an open Gamma Blast is never exited for another', 6 not in order)
order = [x[0].signal_id for x in gp.candidates(book, 'NIFTY', 'manual', price, 0.0, 10000)]
check('manual block: only manual positions in that index', order == [5])
order = [x[0].signal_id for x in gp.candidates(book, 'NIFTY', 'capital', price, -9500.0, 10000)]
check('never books a loss that breaches the daily limit', 5 not in order and 1 not in order and 2 in order)
order = [x[0].signal_id for x in gp.candidates(book, 'NIFTY', 'capital', price, 0.0, 10000, pending=lambda p: p.signal_id == 3)]
check('a position with a pending order is skipped', 3 not in order and order[0] == 2)
check('no fresh price, no exit', [x[0].signal_id for x in gp.candidates(
      [P(9, 'fast_momentum', 'NIFTY', 100, None)], 'NIFTY', 'capital', price, 0.0, 10000)] == [])

closed = []
ex = types.SimpleNamespace(state=types.SimpleNamespace(positions={str(p.signal_id): p for p in book},
                           premium_tracker=types.SimpleNamespace(latest=lambda tok, age: next(p._ltp for p in book if p.token == tok))),
                           _close_position=lambda pos, ltp, reason: closed.append((pos.signal_id, reason)) or {'ok': True})
import kadima.execution_ledger as el, kadima.exit_manager as em, kadima.killswitch as ks  # noqa: E402
el.pending = lambda sid=None: []
em.realized_pnl_today = lambda: 0.0
ks.engaged = lambda: False
os.environ['KADIMA_GAMMA_PRIORITY'] = 'shadow'; gp._cfg.update(at=0.0, value=None)
gp.make_room(ex, 'NIFTY', 'PE', 'portfolio_cash_headroom_exhausted', 77)
check('shadow logs the exit and sells nothing', not closed and any(e == 'gamma_priority_would_exit' for e, _ in LOGS))
os.environ['KADIMA_GAMMA_PRIORITY'] = 'live'; gp._cfg.update(at=0.0, value=None); gp._last.clear()
gp.make_room(ex, 'NIFTY', 'PE', 'portfolio_cash_headroom_exhausted', 77)
check('live exits exactly one position, the first in line', closed == [(3, 'gamma_priority_exit')])
gp.make_room(ex, 'NIFTY', 'PE', 'portfolio_cash_headroom_exhausted', 77)
check('one attempt per 5 s per Gamma side', len(closed) == 1)
gp._last.clear(); ks.engaged = lambda: True
gp.make_room(ex, 'NIFTY', 'PE', 'portfolio_cash_headroom_exhausted', 77)
check('kill switch: no exits', len(closed) == 1)
ks.engaged = lambda: False; gp._last.clear()
gp.make_room(ex, 'NIFTY', 'PE', 'below_threshold', 77)
check('a Gamma refused for its own reasons exits nothing', len(closed) == 1)
os.environ['KADIMA_GAMMA_PRIORITY'] = 'off'; gp._cfg.update(at=0.0, value=None); gp._last.clear()
gp.make_room(ex, 'NIFTY', 'PE', 'portfolio_cash_headroom_exhausted', 77)
check('off: no exits', len(closed) == 1)
src = open(os.path.join(os.path.dirname(gp.__file__), 'auto_executor.py')).read()
check('hook wired into evaluate_and_enter', 'gamma_priority.make_room(self, index_name, direction' in src)
print(f'\n{len(FAILS)} failed' if FAILS else '\nall passed'); sys.exit(1 if FAILS else 0)
