#!/usr/bin/env python3
"""Offline tests for STOCK BLAST v1.1. Run: python3 tools/test_stock_blast_v11_20261027.py"""
import os, sys, tempfile, types, time, json, sqlite3
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())

from kadima import stock_blast as sb, candle_pacer, preparation_diagnostics, move_capture   # noqa: E402
from kadima.option_chain import OptionQuote                                                 # noqa: E402

fails = []


def check(name, cond):
    print(('ok   ' if cond else 'FAIL ') + name)
    if not cond:
        fails.append(name)


def q(strike, ltp, bid, ask, oi=100000, vol=500000, typ='CE'):
    return OptionQuote(token=f'T{strike}{typ}', symbol=f'ATHER{strike}{typ}', strike=strike,
                       opt_type=typ, ltp=ltp, bid=bid, ask=ask, oi=oi, volume=vol)


def chain(quotes):
    return types.SimpleNamespace(index=types.SimpleNamespace(name='ATHERENERG', opt_exchange='NFO'),
                                 quotes={x.token: x for x in quotes},
                                 walls=lambda: {'CE': [], 'PE': []})


# 26-Oct 14:26 ATHER spot 1414, expiry next day; far strikes cheap
ladder = [q(1400, 24.0, 23.5, 24.6), q(1420, 14.0, 13.6, 14.4), q(1460, 4.6, 4.4, 4.8),
          q(1540, 1.10, 1.05, 1.15), q(1580, 0.80, 0.75, 0.85), q(1600, 0.65, 0.60, 0.70),
          q(1640, 0.40, 0.35, 0.45)]
spot, days = 1414.2, 1.05

check('score 89.5 -> lottery gates', sb.gates(89.5) is sb.LOTTERY)
check('score 85 -> blast gates', sb.gates(85) is sb.BLAST)
check('score 70 -> normal gates', sb.gates(70) is sb.NORMAL)
check('ladder 10 each side on >= 88, else 4', sb.ladder_width(90) == 10 and sb.ladder_width(80) == 4)

r = sb.rank(chain([OptionQuote(**vars(x)) for x in ladder]), 'CE', spot, days, 89.5)
ok = [x for x in r if not x.rejected]
lot = [x for x in ok if 'lottery' in x.reasons]
print('     ranked', [(x.quote.strike, x.rejected or x.reasons[-1]) for x in r])
check('primary pick is not a lottery strike', ok and 'lottery' not in ok[0].reasons)
check('far strikes 1540-1600 are lottery candidates', {x.quote.strike for x in lot} >= {1540, 1580, 1600})
check('Rs 0.40 strike stays below the Rs 0.50 floor', all(x.quote.strike != 1640 for x in lot))

r85 = sb.rank(chain([OptionQuote(**vars(x)) for x in ladder]), 'CE', spot, days, 85)
check('score 85 never allows lottery strikes', not any('lottery' in x.reasons for x in r85 if not x.rejected))

check('is_lottery: 1600 CE yes, 1420 CE no', sb.is_lottery(ladder[5], spot) and not sb.is_lottery(ladder[1], spot))
check('lottery sizing 20% of the confidence size',
      abs(sb.allocation(10000, 89.5, {'lottery': True}) - 2000) < 1e-6)
check('primary sizing unchanged', abs(sb.allocation(10000, 89.5, {'lottery': False}) - 10000) < 1e-6)
check('risk gates at 89.5 allow 15% distance', sb.risk_gates(89.5)['max_distance_pct'] == 15.0)

# Hot list default
check('Move Capture hot list default is 20', move_capture.MoveCaptureDetector().max_hot == 20)

# Candle pacer: spacing, retry on a rate-limit reply, errors still raised
calls = []


class Api:
    def __init__(self, replies): self.replies = list(replies)
    def getCandleData(self, p):
        calls.append(time.monotonic())
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


candle_pacer.RETRY_WAIT_SEC = 0.05
api = Api([{'status': True, 'data': []}] * 3)
for _ in range(3):
    candle_pacer.get(api, {})
gaps = [b - a for a, b in zip(calls, calls[1:])]
check('pacer spaces calls >= 0.4 s', all(g >= 0.39 for g in gaps))
calls.clear()
api = Api([Exception('Too many requests. URL: https://x'), {'status': True, 'data': [1]}])
check('pacer retries once after "Too many requests"', candle_pacer.get(api, {})['data'] == [1])
api = Api([{'status': False, 'message': 'Access denied because of exceeding access rate'},
           {'status': True, 'data': [2]}])
check('pacer retries once on a rate-limit status reply', candle_pacer.get(api, {})['data'] == [2])
api = Api([KeyError('boom')])
try:
    candle_pacer.get(api, {}); raised = False
except KeyError:
    raised = True
check('other errors still raised', raised)

# Diagnostics name the real error, without URLs or long tokens
class DataException(Exception):
    pass


d = preparation_diagnostics.describe(
    DataException('Too many requests. URL: https://apiconnect.angelone.in/x token=eyJhbGciOiJIUzUxMiJ9abcdefghijk'),
    'history', 'stock_fast_preparation_error')
print('     diag', d)
check('error_class is the real class', d['error_class'] == 'DataException' and d['error_type'] == 'OtherError')
check('hint keeps the message, drops URL and token',
      'Too many requests' in d['error_hint'] and 'https' not in d['error_hint'] and 'eyJ' not in d['error_hint'])

# Missed-blasts report on a tiny synthetic day
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import missed_blasts as mb                                                              # noqa: E402
data = tempfile.mkdtemp()
os.makedirs(os.path.join(data, 'audit'))
c = sqlite3.connect(os.path.join(data, 'price_history.db'))
c.execute('CREATE TABLE price_days (day TEXT, key TEXT, exchange TEXT, token TEXT, symbol TEXT, '
          'underlying TEXT, asset TEXT, payload TEXT)')
c.execute('CREATE TABLE price_minutes (day TEXT, key TEXT, minute INTEGER, first_at INTEGER, '
          'last_at INTEGER, first REAL, last REAL, high REAL, high_at INTEGER, low REAL, low_at INTEGER, '
          'first_volume REAL, last_volume REAL, volume_reset INTEGER, samples INTEGER, sources INTEGER, '
          'max_gap_seconds REAL)')
day = '2026-10-26'
t0 = int(datetime(2026, 10, 26, 14, 26, tzinfo=mb.IST).timestamp() * 1000)
c.execute("INSERT INTO price_days VALUES (?,?,?,?,?,?,?,?)", (day, 'k1', 'NFO', '1', 'ATHER1400CE', 'ATHERENERG', 'OPTION', '{}'))
c.execute("INSERT INTO price_days VALUES (?,?,?,?,?,?,?,?)", (day, 'k2', 'NFO', '2', 'X100CE', 'QUIETCO', 'OPTION', '{}'))
for i, (lo, hi) in enumerate([(11, 12), (12, 30), (28, 90), (85, 215), (150, 160)]):
    m = t0 + i * 300000
    c.execute('INSERT INTO price_minutes (day,key,minute,low,low_at,high,high_at,first) VALUES (?,?,?,?,?,?,?,?)',
              (day, 'k1', m, lo, m, hi, m, lo))
    c.execute('INSERT INTO price_minutes (day,key,minute,low,low_at,high,high_at,first) VALUES (?,?,?,?,?,?,?,?)',
              (day, 'k2', m, 10, m, 11, m, 10))
c.commit(); c.close()
with open(os.path.join(data, 'audit', day + '.jsonl'), 'w') as f:
    f.write(json.dumps(dict(ts='2026-10-26T14:26:37+05:30', event='MOVE_CAPTURE_TRIGGER',
                            underlying='ATHERENERG', score=89.5, direction='CE')) + '\n')
    f.write(json.dumps(dict(ts='2026-10-26T14:26:44+05:30', event='MOVE_CAPTURE_PREPARATION_REJECTED',
                            index='ATHERENERG', reason='move_capture_no_liquid_option')) + '\n')
rep = mb.build(data, day)
row = rep['rows'][0] if rep['rows'] else {}
check('report finds the ATHER blast only', rep['blasts'] == 1 and row.get('underlying') == 'ATHERENERG')
check('report multiple 215/11 ~ 19.5x', abs(row.get('multiple', 0) - 19.5) < 0.1)
check('report verdict and reason', row.get('verdict') == 'TRIGGERED_NOT_TRADED'
      and row['top_reasons'][0][0] == 'move_capture_no_liquid_option')
check('report times', row.get('low_at') == '14:26' and row.get('high_at') == '14:41')
html_out = mb.page(rep)
check('report page renders', 'ATHERENERG' in html_out and '19.5x' in html_out)

print('\n%d failure(s)' % len(fails))
sys.exit(1 if fails else 0)
