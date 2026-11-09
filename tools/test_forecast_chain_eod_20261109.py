"""Offline test for the 09-Nov chain change (no broker, no network). NEEDS YES to install.

1. Closing (EOD) option chain: ~460 contracts = 10 quote calls, each one fair FIFO turn behind
   trading quotes. The 15:30-15:32 window ran out ('quote_budget_busy_until_deadline' at 15:32:00
   on 03, 05 and 09 Nov), so chain_eod had no rows. The window now runs to 15:40 (market closed).
2. Previous-day PCR / OI walls are compared over the same +-15 strikes as the live snapshot
   (06 Nov: all-strike PCR 1.79 vs 0.81 near the money -> a false 'PCR collapsed' vote on 09 Nov).

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_chain_eod_20261109.py
"""
import io
import os
import sqlite3
import sys
import tempfile
import urllib.error
import zipfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF                                 # noqa: E402
from kadima import forecast_chain_data as F                              # noqa: E402
from kadima import forecast_bhavcopy as B                                # noqa: E402
from kadima import market_session_gate as G                              # noqa: E402
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


POLICY = {'holidays': []}

# ---------------------------------------------------------------- 1. EOD chain window
start = datetime(2026, 11, 9, 15, 30, 1, tzinfo=HF.IST)
index = Index('NIFTY', 'NSE', 'spot', 'NFO', 'NIFTY', 50, 1)
master = []
for expiry in ('10NOV2026', '17NOV2026'):
    for strike in range(23000, 28750, 50):  # 115 strikes x 2 sides x 2 expiries = 460
        for side in ('CE', 'PE'):
            master.append(dict(name='NIFTY', exch_seg='NFO', instrumenttype='OPTIDX', expiry=expiry,
                               strike=str(strike * 100), symbol='NIFTY' + expiry + str(strike) + side,
                               token=str(len(master) + 1)))


class Clock:
    def __init__(self, t, step=0.3):
        self.t, self.step = t, timedelta(seconds=step)

    def __call__(self):
        self.t += self.step
        return self.t


def eod_run(fifo_wait_sec):
    """Live 09-Nov condition: idle capacity never appears and each FIFO turn waits behind
    trading quotes. Drives the collector through tick(), so window and deadline are the code's."""
    clock = Clock(start)
    st = SimpleNamespace(is_live_tick=True, last_updated=start, spot=25044.65,
                         chain=SimpleNamespace(index=index))
    st_fresh = lambda: setattr(st, 'last_updated', clock.t)             # noqa: E731
    engine = SimpleNamespace(states={'NIFTY': st}, scrips=ScripMaster(master))

    def low(**kwargs):
        st_fresh()
        raise TimeoutError('forecast_quote_budget_busy')
    fifo = {'n': 0}

    def quote(**kwargs):
        fifo['n'] += 1
        clock.t += timedelta(seconds=fifo_wait_sec)
        tokens = kwargs['exchangeTokens']['NFO']
        return dict(status=True, data=dict(fetched=[dict(symbolToken=t, exchange='NFO', ltp=10,
                                                         opnInterest=5000, tradeVolume=100) for t in tokens]))
    quote._kadima_budgeted = True
    quote._kadima_low_priority = low
    c = database()
    collector = F.Collector(engine, ('NIFTY',))
    logs = []
    with patch.object(HF, 'conn', return_value=c), \
            patch.object(SESSION, 'api', SimpleNamespace(getMarketData=quote)), \
            patch.object(SESSION, '_stale', return_value=False), \
            patch.object(F, 'now_ist', clock), \
            patch.object(G, 'read_json', return_value=POLICY), \
            patch.object(G, 'validate_policy', side_effect=lambda p: p), \
            patch.object(collector.stop_event, 'wait', return_value=False), \
            patch.object(F.audit, 'log', side_effect=lambda e, **k: logs.append((e, k))):
        collector.tick(start)
        rows = c.execute('SELECT COUNT(*) FROM chain_eod').fetchone()[0]
        # a second tick in the window must not start another EOD attempt
        collector.tick(start + timedelta(minutes=1))
        late = F.Collector(engine, ('NIFTY',))
        with patch.object(late.stop_event, 'wait', return_value=False):
            late.tick(datetime(2026, 11, 9, 15, 40, 5, tzinfo=HF.IST))
    return rows, fifo['n'], logs


rows, calls, logs = eod_run(fifo_wait_sec=12)
skipped = [k for e, k in logs if e == 'forecast_chain_cycle_skipped']
check('EOD chain with 10 slow FIFO turns (~180 s) is saved: rows=%d calls=%d skips=%s' % (rows, calls, skipped),
      rows == 460 and calls == 10 and not skipped)
check('only one EOD attempt per day, none after the window', calls == 10)
rows, calls, logs = eod_run(fifo_wait_sec=1)
check('fast EOD chain still saved (%d rows)' % rows, rows == 460)
check('EOD window is after the close only', F.EOD_START.hour * 60 + F.EOD_START.minute >= 15 * 60 + 30)




# ---------------------------------------------------------------- 2. previous-day strike range
def prev_db():
    c = database()
    rows = []
    spot = 25030.35
    for strike in range(23000, 27050, 50):
        far = abs(strike - 25050) > 750
        ce = 20000 if strike == 25000 else (500 if far else 4000)
        pe = (90000 if strike == 23500 else 30000) if far else (6000 if strike == 24500 else 3000)
        rows.append(('NIFTY', '2026-11-06', '2026-11-10', strike, 'CE', 1, ce, None, 1, spot))
        rows.append(('NIFTY', '2026-11-06', '2026-11-10', strike, 'PE', 1, pe, None, 1, spot))
    c.executemany('INSERT INTO chain_eod VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
    return c


c = prev_db()
t0 = datetime(2026, 11, 9, 10, 15, tzinfo=HF.IST)
with patch.object(G, 'read_json', return_value=POLICY), patch.object(G, 'validate_policy', side_effect=lambda p: p):
    prev = F.previous_chain(c, 'NIFTY', t0, 50)
near = [dict(strike=r['strike'], opt_type=r['opt_type'], oi=r['oi']) for r in
        c.execute("SELECT * FROM chain_eod WHERE abs(strike-25050)<=750")]
want = F.aggregate(near)
check('previous-day PCR uses the same +-15 strikes as the live snapshot: %s vs %s' % (prev and round(prev['pcr'], 3), round(want['pcr'], 3)),
      prev is not None and abs(prev['pcr'] - want['pcr']) < 1e-9)
check('previous-day PE wall is the near-the-money wall (24500), not the deep-OTM 23500: %s' % (prev and prev['pe_wall']),
      prev is not None and prev['pe_wall'] == 24500)

print('ALL FORECAST CHAIN EOD 20261109 TESTS PASS' if not FAILS else 'FAILED: %d' % len(FAILS))
sys.exit(1 if FAILS else 0)
