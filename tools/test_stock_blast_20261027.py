#!/usr/bin/env python3
"""Offline tests for STOCK BLAST v1. Run: python3 tools/test_stock_blast_20261027.py

Quotes are the real end-of-day 26-Oct stock-option snapshots read from the live server's
stock_fast_snapshots table (ATHERENERG / COFORGE / POLICYBZR), plus the 14:26 ATHER case.
"""
import os, sys, tempfile, json, types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())

from kadima import stock_blast, risk_budget, strike_selector            # noqa: E402
from kadima.option_chain import OptionQuote                              # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
fails = []


def check(name, cond):
    print(('ok   ' if cond else 'FAIL ') + name)
    if not cond:
        fails.append(name)


def chain(name, quotes):
    c = types.SimpleNamespace()
    c.index = types.SimpleNamespace(name=name, opt_exchange='NFO')
    c.quotes = {str(i): q for i, q in enumerate(quotes)}
    c.walls = lambda: {'CE': [], 'PE': []}
    return c


def q(sym, strike, typ, ltp, bid, ask, oi, vol):
    return OptionQuote(token=sym, symbol=sym, strike=strike, opt_type=typ, ltp=ltp,
                       bid=bid, ask=ask, oi=oi, volume=vol)


# --- 1. 26-Oct 14:26 ATHER: spot 1414, 1 day to expiry, cheap CE ladder (step 20) ---------
ather = [q('ATHER1340CE', 1340, 'CE', 76.0, 75.0, 77.5, 60000, 300000),
         q('ATHER1380CE', 1380, 'CE', 40.0, 39.2, 40.8, 90000, 900000),
         q('ATHER1400CE', 1400, 'CE', 24.0, 23.5, 24.6, 150000, 1500000),
         q('ATHER1420CE', 1420, 'CE', 14.0, 13.6, 14.4, 140000, 1400000),
         q('ATHER1440CE', 1440, 'CE', 8.2, 7.9, 8.5, 120000, 1100000),
         q('ATHER1460CE', 1460, 'CE', 4.6, 4.4, 4.8, 90000, 700000),
         q('ATHER1500CE', 1500, 'CE', 1.6, 1.5, 1.7, 80000, 400000)]
days = 1.05
old = [r for r in strike_selector.rank(chain('ATHERENERG', [OptionQuote(**vars(x)) for x in ather]),
                                       'CE', 1414.2, days) if not r.rejected] \
    if hasattr(strike_selector, 'MIN_OI') else None
blast = stock_blast.rank(chain('ATHERENERG', ather), 'CE', 1414.2, days, score=89.52)
ok_blast = [r for r in blast if not r.rejected]
check('ATHER 14:26 blast trigger finds tradeable strikes', len(ok_blast) >= 3)
check('ATHER blast picks a cheap high-leverage strike (<= Rs 30)',
      ok_blast and ok_blast[0].quote.ask <= 30)
print('     chosen', ok_blast[0].quote.symbol if ok_blast else None,
      [(r.quote.strike, r.rejected) for r in blast])
check('Rs 1.6 strike is only a far-strike (lottery) candidate at 89.5 (v1.1)',
      any(r.quote.strike == 1500 and not r.rejected and 'lottery' in r.reasons for r in blast))
b85 = stock_blast.rank(chain('ATHERENERG', [OptionQuote(**vars(x)) for x in ather]), 'CE', 1414.2, days, score=85)
check('Rs 1.6 premium rejected below the lottery score', any(r.quote.strike == 1500 and r.rejected for r in b85))

normal = stock_blast.rank(chain('ATHERENERG', [OptionQuote(**vars(x)) for x in ather]),
                          'CE', 1414.2, days, score=70.0)
ok_norm = [r for r in normal if not r.rejected]
check('ATHER normal trigger (score 70) still finds a strike', len(ok_norm) >= 1)
check('normal mode never picks delta < 0.25', all(abs(r.quote.delta) >= 0.25 for r in ok_norm))

# --- 2. Real end-of-day snapshots (spreads 3-6%) pass; very wide books do not ------------------
coforge = [q('COFORGE2000CE', 2000, 'CE', 16.25, 16.4, 17.0, 664050, 8503450),
           q('COFORGE2020CE', 2020, 'CE', 9.7, 9.15, 9.7, 276450, 3981925)]
r = stock_blast.rank(chain('COFORGE', coforge), 'CE', 2003.0, 0.02, score=82)
check('COFORGE 2000CE (3.6% spread) passes stock gates',
      any(x.quote.strike == 2000 and not x.rejected for x in r))
pol = [q('POLICYBZR1020CE', 1020, 'CE', 12.0, 10.05, 13.85, 96250, 1646750)]
r = stock_blast.rank(chain('POLICYBZR', pol), 'CE', 1015.0, 0.5, score=90)
check('POLICYBZR 1020CE (32% spread) is rejected', all(x.rejected for x in r))
print('     reason', r[0].rejected)

# --- 3. Index chains are untouched -----------------------------------------------------------
check('NIFTY is an index (feature never applies)', stock_blast.is_index('NIFTY'))
check('applies() false for index', not stock_blast.applies(chain('NIFTY', []), {'score': 90}))
check('applies() true for stock', stock_blast.applies(chain('ATHERENERG', []), {'score': 90}))
check('applies() false without fast context', not stock_blast.applies(chain('ATHERENERG', []), None))

# --- 4. Time window: no 15-minute horizon bar; 15:10 cutoff ----------------------------------
mon = datetime(2026, 10, 26, tzinfo=IST)
check('14:26 allowed', stock_blast.entry_time_allowed(mon.replace(hour=14, minute=26)))
check('15:02 allowed (was blocked by the 15-min horizon)',
      stock_blast.entry_time_allowed(mon.replace(hour=15, minute=2)))
check('15:10 blocked', not stock_blast.entry_time_allowed(mon.replace(hour=15, minute=10)))
check('09:14 blocked', not stock_blast.entry_time_allowed(mon.replace(hour=9, minute=14)))

# --- 5. Sizing by confidence ----------------------------------------------------------------
check('score 89.5 -> 100%', stock_blast.size_fraction(89.5) == 1.0)
check('score 80 -> 70%', stock_blast.size_fraction(80) == 0.7)
check('score 70 -> 40%', stock_blast.size_fraction(70) == 0.4)

# --- 6. risk_budget: index gates unchanged, stock gates per call -----------------------------
c = risk_budget.StrikeCandidate('ATHER1420CE', 't', 1420, 'CE', 14.0, 13.6, 14.4, 0.42, 1400000, 140000)
check('index gates reject 5.7% spread', risk_budget._quality_fail_reason(c, 1414.2) is not None)
check('stock gates accept it', risk_budget._quality_fail_reason(
    c, 1414.2, stock_blast.risk_gates(89.5)) is None)
cheap = risk_budget.StrikeCandidate('ATHER1460CE', 't', 1460, 'CE', 4.6, 4.4, 4.8, 0.12, 700000, 90000)
check('blast gates accept cheap OTM delta 0.12', risk_budget._quality_fail_reason(
    cheap, 1414.2, stock_blast.risk_gates(89.5)) is None)
check('normal gates reject delta 0.12', risk_budget._quality_fail_reason(
    cheap, 1414.2, stock_blast.risk_gates(70)) is not None)

# --- 7. Exit: hard stop, lock, peak runner hand-off ------------------------------------------
from kadima import scalp_runner, config as kcfg                          # noqa: E402
data = os.environ['KADIMA_DATA_DIR']
json.dump({'mode': 'live'}, open(os.path.join(kcfg.DATA_DIR, 'scalp_runner.json'), 'w'))
scalp_runner._cfg.update(at=0.0, value=None)
pos = types.SimpleNamespace(kind='fast_momentum', index='ATHERENERG', signal_id=9001,
                            entry_price=20.0, peak_price=20.0)
base, eff, active, reason = stock_blast.managed_stop(pos, 19.0)
check('hard stop at -35%', abs(base - 13.0) < 1e-9 and reason is None)
base, eff, active, reason = stock_blast.managed_stop(pos, 12.9)
check('below -35% exits', reason == 'stock_blast_hard_stop_35pct')
pos.peak_price = 23.0
base, eff, active, reason = stock_blast.managed_stop(pos, 22.5)
check('+15% peak locks +3%', abs(eff - 20.6) < 1e-9 and active)
pos.peak_price = 60.0
t0 = 1000.0
import time as _t                                                       # noqa: E402
real = _t.monotonic
seq = [60, 59.5, 58.0, 56.0, 54.0, 52.0, 50.0]
out = None
for i, px in enumerate(seq):
    _t.monotonic = lambda i=i: t0 + i * 2
    out = stock_blast.managed_stop(pos, px)
    if out[3]:
        break
_t.monotonic = real
check('runner exits on peak forecast after a 3x run', out[3] is not None and 'stock_blast_scalp_runner' in out[3])
print('     runner exit', out[3], 'stop', round(out[1], 2))
check('runner stop never below +20% floor', out[1] >= 24.0)
idx = types.SimpleNamespace(kind='fast_momentum', index='NIFTY', signal_id=1, entry_price=100.0, peak_price=100)
check('index positions untouched by managed_stop', stock_blast.managed_stop(idx, 90.0) is None)
json.dump({'mode': 'off'}, open(os.path.join(kcfg.DATA_DIR, 'stock_blast.json'), 'w'))
stock_blast._cfg.update(at=0.0, value=None)
check('mode off -> managed_stop None', stock_blast.managed_stop(pos, 50.0) is None)
check('mode off -> applies False', not stock_blast.applies(chain('ATHERENERG', []), {'score': 90}))

print('\n%d failure(s)' % len(fails))
sys.exit(1 if fails else 0)
