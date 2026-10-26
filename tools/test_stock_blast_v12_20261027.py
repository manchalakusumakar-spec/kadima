#!/usr/bin/env python3
"""Offline tests for STOCK BLAST v1.2 loser filters. Run: python3 tools/test_stock_blast_v12_20261027.py"""
import os, sys, tempfile, types, sqlite3
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())

from kadima import stock_blast as sb, price_history                       # noqa: E402
from kadima.option_chain import OptionQuote                               # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
fails = []


def check(name, cond):
    print(('ok   ' if cond else 'FAIL ') + name)
    if not cond:
        fails.append(name)


def q(strike, ltp, typ='CE'):
    return OptionQuote(token=f'T{strike}{typ}', symbol=f'X{strike}{typ}', strike=strike, opt_type=typ,
                       ltp=ltp, bid=ltp * 0.98, ask=ltp, oi=100000, volume=500000, delta=0.45)


now = datetime(2026, 10, 27, 11, 0, 30, tzinfo=IST)
spot = 1000.0

# strike gates: premium floor and ITM limit
check('Rs 2.50 main strike rejected (floor Rs 3)', 'premium' in (sb.fail_reason(q(1020, 2.5), spot, sb.NORMAL) or ''))
check('Rs 3.20 main strike allowed', sb.fail_reason(q(1010, 3.2), spot, sb.NORMAL) is None)
check('CE 1% in the money rejected', 'itm' in (sb.fail_reason(q(990, 20.0), spot, sb.NORMAL) or ''))
check('CE 0.4% in the money allowed', sb.fail_reason(q(996, 15.0), spot, sb.NORMAL) is None)
check('PE 1% in the money rejected', 'itm' in (sb.fail_reason(q(1010, 20.0, 'PE'), spot, sb.NORMAL) or ''))
far = q(1120, 0.8); far.delta = 0.03
check('far-strike Rs 0.80 still allowed in the lottery tier', sb.fail_reason(far, spot, sb.LOTTERY) is None)

# entry filters
data = dict(direction='CE', score=75, return_5m_pct=0.35)
check('CE with +0.35% in 5 min passes', sb.entry_filter('AAA', data, q(1010, 10), now) is None)
check('CE with +0.10% in 5 min blocked', sb.entry_filter('AAA', dict(data, return_5m_pct=0.10), q(1010, 10), now)
      == 'stock_not_moving_5m')
check('PE needs the stock falling', sb.entry_filter('AAA', dict(data, direction='PE'), q(990, 10, 'PE'), now)
      == 'stock_not_moving_5m')
check('PE with -0.3% passes', sb.entry_filter('AAA', dict(data, direction='PE', return_5m_pct=-0.3),
                                              q(990, 10, 'PE'), now) is None)
check('missing 5-min return blocked', sb.entry_filter('AAA', dict(data, return_5m_pct=None), q(1010, 10), now)
      == 'stock_not_moving_5m')

for _ in range(3):
    sb.note_trade('BBB', now)
check('trade count persisted', sb.trades_today('BBB', now) == 3)
check('4th trade on the same stock blocked', sb.entry_filter('BBB', data, q(1010, 10), now) == 'stock_trade_cap_reached')
check('count resets next day', sb.trades_today('BBB', now + timedelta(days=1)) == 0)

# no-chase: option up >= 10% over ~3 min from the minute record
c = price_history.connect(price_history.database_path())
key = price_history.identity('NFO', 'T1010CE', 'X1010CE')
m3 = int((now.replace(second=0) - timedelta(minutes=3)).timestamp() * 1000)
c.execute('INSERT INTO price_minutes (day,key,minute,last) VALUES (?,?,?,?)', ('2026-10-27', key, m3, 8.0))
c.commit(); c.close()
check('option move 3m computed (8 -> 10 = +25%)', abs(sb.option_move_3m('NFO', 'T1010CE', 'X1010CE', 10.0, now) - 25) < 1e-6)
check('chasing option blocked', sb.entry_filter('CCC', data, q(1010, 10), now) == 'option_chase_3m')
check('unknown history does not block', sb.entry_filter('CCC', data, q(1020, 10), now) is None)

import json
json.dump({'mode': 'off'}, open(os.path.join(sb.DATA_DIR, 'stock_blast.json'), 'w'))
sb._cfg.update(at=0.0, value=None)
check('mode off -> filters off', sb.entry_filter('AAA', dict(data, return_5m_pct=0.0), q(1010, 10), now) is None)

print('\n%d failure(s)' % len(fails))
sys.exit(1 if fails else 0)
