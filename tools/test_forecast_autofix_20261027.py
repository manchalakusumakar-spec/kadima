"""Offline test for the 27-Oct autofix: the forecast option-chain collector no longer drops a
whole 5-minute cycle when the shared quote budget is momentarily busy. No broker or network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_autofix_20261027.py

Live symptom (21-27 Oct): every 'wide' and 'eod' cycle was logged forecast_chain_cycle_skipped
TimeoutError, and chain_wide / chain_eod never received a row. The low-priority poll is tried
once per mark and raises TimeoutError('forecast_quote_budget_busy') whenever another quote is
in flight or was sent in the last two intervals, which on the live engine is nearly always.
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


# 1. Budget busy for the first few tries, then idle: the snapshot is taken (old code: skipped).
poll, state = busy_then_serve(3)
rows, err = run(poll)
check('busy budget for 3 tries then idle -> wide snapshot saved (62 rows), got %s rows, err=%r' % (rows, err),
      rows == 62 and err is None and state['sent'] == 2)

poll, state = busy_then_serve(2, reason='forecast_foreground_quote_active')
rows, err = run(poll)
check('foreground quote in flight, then idle -> snapshot saved', rows == 62 and err is None)

# 2. Budget busy the whole window: no quote is sent, the cycle ends at the deadline, nothing written.
poll, state = busy_then_serve(10 ** 9)
rows, err = run(poll, deadline_sec=10)
check('busy until the deadline -> TimeoutError, no rows, no quote sent',
      rows == 0 and isinstance(err, TimeoutError) and state['sent'] == 0 and state['calls'] < 100)

# 3. Any other TimeoutError is not retried.
poll, state = busy_then_serve(1, reason='snapshot_deadline_somewhere_else')
rows, err = run(poll)
check('a different TimeoutError is raised at once (no retry)', rows == 0 and state['calls'] == 1
      and isinstance(err, TimeoutError))

# 4. The skip event now carries the reason, so a future failure can be diagnosed from the log.
collector = F.Collector(engine, ('NIFTY',))
logged = []
with patch.object(F.audit, 'log', side_effect=lambda e, **k: logged.append((e, k))), \
        patch.object(collector, 'collect', side_effect=TimeoutError('forecast_quote_budget_busy')), \
        patch('kadima.market_session_gate.read_json', return_value={}), \
        patch('kadima.market_session_gate.validate_policy', return_value={'holidays': []}):
    collector.tick(now)
check('forecast_chain_cycle_skipped logs the error text',
      any(e == 'forecast_chain_cycle_skipped' and k.get('error') == 'forecast_quote_budget_busy' for e, k in logged))

print('ALL FORECAST AUTOFIX 20261027 TESTS PASS' if not FAILS else 'FAILED: %d' % len(FAILS))
sys.exit(1 if FAILS else 0)
