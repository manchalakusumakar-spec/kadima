"""Offline test for the 02-Nov autofix: the forecast option-chain collector still skipped every
cycle after the 27-Oct retry fix, because the live engine keeps the shared quote budget busy all
session (chain_wide 0 rows 27 Oct-02 Nov, 76 forecast_chain_cycle_skipped a day,
'quote_budget_busy_until_deadline'). No broker or network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_autofix_20261102.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF                                 # noqa: E402
from kadima import forecast_chain_data as F                              # noqa: E402
from kadima.config import Index                                          # noqa: E402
from kadima.scrip_master import ScripMaster                              # noqa: E402
from kadima.session import SESSION                                       # noqa: E402

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def database():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript(F.SCHEMA)
    return c


now = datetime(2026, 10, 27, 11, 5, 20, tzinfo=HF.IST)
index = Index('NIFTY', 'NSE', 'spot', 'NFO', 'NIFTY', 50, 1)
master = []
for expiry in ('29OCT2026', '05NOV2026'):
    for strike in range(50, 1600, 50):
        for side in ('CE', 'PE'):
            master.append(dict(name='NIFTY', exch_seg='NFO', instrumenttype='OPTIDX', expiry=expiry,
                               strike=str(strike * 100), symbol='NIFTY' + expiry + str(strike) + side,
                               token=str(len(master) + 1)))
st = SimpleNamespace(is_live_tick=True, last_updated=now, spot=800, chain=SimpleNamespace(index=index))
engine = SimpleNamespace(states={'NIFTY': st}, scrips=ScripMaster(master))


def busy_then_serve(busy_calls, reason='forecast_quote_budget_busy'):
    state = {'calls': 0, 'sent': 0}

    def poll(**kwargs):
        state['calls'] += 1
        if state['calls'] <= busy_calls:
            raise TimeoutError(reason)
        state['sent'] += 1
        tokens = kwargs['exchangeTokens']['NFO']
        return dict(status=True, data=dict(fetched=[dict(symbolToken=t, exchange='NFO', ltp=100,
                                                         opnInterest=1000, tradeVolume=3000) for t in tokens]))
    poll._kadima_low_priority = poll
    return poll, state


class Clock:
    """now_ist() that advances a little on each read, so a deadline is eventually reached."""
    def __init__(self, start, step=0.3):
        self.t, self.step = start, timedelta(seconds=step)

    def __call__(self):
        self.t += self.step
        return self.t


def run(poll, deadline_sec=39, clock_step=0.3):
    c = database()
    collector = F.Collector(engine, ('NIFTY',))
    err = None
    with patch.object(HF, 'conn', return_value=c), \
            patch.object(SESSION, 'api', SimpleNamespace(getMarketData=poll)), \
            patch.object(SESSION, '_stale', return_value=False), \
            patch.object(F, 'now_ist', Clock(now, clock_step)), \
            patch.object(collector.stop_event, 'wait', return_value=False), \
            patch.object(F.audit, 'log'):
        try:
            collector.collect('NIFTY', now, now + timedelta(seconds=deadline_sec), False)
        except Exception as exc:                                         # noqa: BLE001
            err = exc
    return c.execute('SELECT COUNT(*) FROM chain_wide').fetchone()[0], err




def budgeted_wrapper(low_poll, fifo_state):
    """Stands in for rate_limiter.protect_quote_api's quote(): FIFO-budgeted foreground call."""
    def quote(**kwargs):
        fifo_state['fifo'] += 1
        tokens = kwargs['exchangeTokens']['NFO']
        return dict(status=True, data=dict(fetched=[dict(symbolToken=t, exchange='NFO', ltp=100,
                                                         opnInterest=1000, tradeVolume=3000) for t in tokens]))
    quote._kadima_budgeted = True
    quote._kadima_low_priority = low_poll
    return quote


# 1. Live 02-Nov condition: idle capacity never appears. Old code: TimeoutError, 0 rows.
low, low_state = busy_then_serve(10 ** 9)
fifo = {'fifo': 0}
rows, err = run(budgeted_wrapper(low, fifo), deadline_sec=39)
check('budget busy all window -> snapshot saved via one FIFO turn per batch (62 rows), got %s rows, err=%r, fifo=%d'
      % (rows, err, fifo['fifo']), rows == 62 and err is None and fifo['fifo'] == 2 and low_state['sent'] == 0)

# 2. Idle capacity appears quickly -> the FIFO path is not used.
low, low_state = busy_then_serve(3)
fifo = {'fifo': 0}
rows, err = run(budgeted_wrapper(low, fifo), deadline_sec=39)
check('idle after 3 busy tries -> low-priority used, no FIFO call', rows == 62 and err is None and fifo['fifo'] == 0)

# 3. Too little time left in the window -> never queue (would finish late); old behaviour kept.
low, low_state = busy_then_serve(10 ** 9)
fifo = {'fifo': 0}
rows, err = run(budgeted_wrapper(low, fifo), deadline_sec=12)
check('short window -> no FIFO call, TimeoutError, no rows',
      rows == 0 and isinstance(err, TimeoutError) and fifo['fifo'] == 0)

# 4. getMarketData without the budget wrapper is never called directly.
low, low_state = busy_then_serve(10 ** 9)
rows, err = run(low, deadline_sec=39)
check('unbudgeted SDK method is never used as fallback', rows == 0 and isinstance(err, TimeoutError)
      and low_state['sent'] == 0)

# 5. A non-busy TimeoutError is still raised at once.
low, low_state = busy_then_serve(1, reason='something_else')
fifo = {'fifo': 0}
rows, err = run(budgeted_wrapper(low, fifo))
check('other TimeoutError raised at once, no FIFO', rows == 0 and fifo['fifo'] == 0 and low_state['calls'] == 1)

print('ALL FORECAST AUTOFIX 20261102 TESTS PASS' if not FAILS else 'FAILED: %d' % len(FAILS))
sys.exit(1 if FAILS else 0)
