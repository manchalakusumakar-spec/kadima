"""WINDOW EXIT v1 offline tests. Run: KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_window_exit.py"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kadima import window_exit as w  # noqa: E402
from kadima import scalp_v2, execution_ledger  # noqa: E402
from kadima.config import DATA_DIR  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
D = datetime(2026, 11, 10, tzinfo=IST)


def at(h, m, s=0):
    return D.replace(hour=h, minute=m, second=s)


@dataclass
class Pos:
    signal_id: int
    kind: str
    entry_price: float
    entry_ts: Optional[datetime]
    lots: int = 2
    lot_size: int = 65
    remaining_qty: int = 130
    status: str = 'open'
    peak_price: Optional[float] = None
    structural_stop: Optional[float] = None
    symbol: str = 'NIFTYTEST'


def set_mode(m):
    with open(os.path.join(DATA_DIR, 'window_exit.json'), 'w') as f:
        json.dump({'mode': m}, f)
    w._cfg['value'] = None


def check(cond, msg):
    if not cond:
        raise SystemExit('FAIL: ' + msg)
    print('ok  ', msg)


# ---- pure rules
check(w.in_window(at(9, 45)) and w.in_window(at(10, 9, 59)), 'window includes 09:45 and 10:09:59')
check(not w.in_window(at(9, 44, 59)) and not w.in_window(at(10, 10)), 'window excludes 09:44:59 and 10:10')
check(w.in_window(datetime(2026, 11, 10, 9, 50)), 'naive entry_ts read as IST')
check(w.time_up(at(13, 0)) and not w.time_up(at(12, 59, 59)), '13:00 time exit')
check(w.scalp_stop(100, 119.9, 93) == (93, False), 'scalp: engine stop below T2')
check(w.scalp_stop(100, 120, 93) == (100, True), 'scalp: stop to entry at T2')
check(w.scalp_stop(100, 105, 93, True) == (100, True), 'scalp: T2 hit stays hit after a pullback')
check(w.gamma_stop(100, 149, 65) == 65, 'gamma: engine stop below T1')
check(w.gamma_stop(100, 150, 65) == 150, 'gamma: stop to T1 at +50%')
check(w.gamma_stop(100, 200, 65) == 150, 'gamma: at T2 trail 25% = 150 (= T1)')
check(w.gamma_stop(100, 260, 65) == 195, 'gamma: trail 25% below peak 260 = 195')
check(w.gamma_stop(100, 200, 65, previous=195) == 195, 'gamma: stop never moves down')
check(w.half_quantity(130, 65) == 65 and w.half_quantity(65, 65) == 0 and w.half_quantity(195, 65) == 65,
      'half quantity: 2 lots -> 1, 1 lot -> 0, 3 lots -> 1')

# ---- hooks
scalp_v2._plan = lambda sid: {'entry': 100, 'stop_pct': 0.07}   # v2 plan present
subs = []
execution_ledger.pending = lambda sid: False
execution_ledger.submit = lambda pos, side, qty, price: subs.append((side, qty, price)) or {'ok': True}

set_mode('off')
p = Pos(1, 'fast_momentum', 100.0, at(9, 50))
check(w.scalp_managed(p, 121, 93, 121, at(10, 0)) is None and not w.holds(p), 'off: no effect')

set_mode('shadow')
p = Pos(2, 'fast_momentum', 100.0, at(9, 50))
check(w.scalp_managed(p, 121, 93, 121, at(10, 0)) is None and not w.holds(p), 'shadow: logs only, returns None')

set_mode('live')
p = Pos(3, 'fast_momentum', 100.0, at(10, 15))
check(w.scalp_managed(p, 121, 93, 121, at(10, 20)) is None and not w.holds(p), 'live: 10:15 entry untouched')
p = Pos(4, 'fast_momentum', 100.0, at(9, 50))
check(w.holds(p), 'live: 09:50 v2 entry is held')
check(w.scalp_managed(p, 110, 93, 110, at(10, 0)) == (93, 93, False, None), 'live: below T2 keeps engine stop')
check(w.scalp_managed(p, 121, 93, 121, at(10, 5)) == (93, 100, True, None), 'live: at T2 stop -> entry, 2 lots no exit')
w.scalp_partial(None, p, 121)
from kadima.auto_executor import LIMIT_ORDER_BUFFER_PCT as B
check(subs == [('SELL', 65, max(0.05, round(121 * (1 - B / 100.0), 1)))], 'live: half (1 lot) sold at T2 with the executor limit buffer')
p.remaining_qty = 65
w.scalp_partial(None, p, 125)
check(len(subs) == 1, 'live: half booked only once')
check(w.scalp_managed(p, 130, 93, 135, at(11, 0))[3] is None, 'live: runner holds (no 1-lot exit after partial)')
check(w.scalp_managed(p, 99.9, 93, 135, at(11, 30))[3] == 'window_exit_entry_stop', 'live: runner exits at entry')
check(w.scalp_managed(p, 130, 93, 135, at(13, 0))[3] == 'window_exit_1300', 'live: 13:00 exit')
p1 = Pos(5, 'fast_momentum', 100.0, at(9, 46), lots=1, remaining_qty=65)
check(w.scalp_managed(p1, 120, 93, 120, at(9, 55))[3] == 'window_exit_t2_one_lot', 'live: 1-lot exits whole at T2')

g = Pos(6, 'gamma_blast', 100.0, at(9, 58), structural_stop=65.0)
check(w.gamma_managed(g, 120, at(10, 5)) == (65.0, 65.0, False, None), 'gamma live: engine stop below T1')
check(w.gamma_managed(g, 150, at(10, 20))[1:] == (150, True, None), 'gamma live: T1 lock at +50%')
g.structural_stop = 140.0   # a later engine trail must not move the base
g.peak_price = 260
check(w.gamma_managed(g, 240, at(11, 0))[0] == 65.0 and w.gamma_managed(g, 240, at(11, 0))[1] == 195,
      'gamma live: base fixed at first engine stop; trail 25% below 260')
check(w.gamma_managed(g, 194, at(11, 10))[3] == 'window_exit_gamma_lock', 'gamma live: trail exit')
g2 = Pos(7, 'gamma_blast', 100.0, at(9, 58), structural_stop=65.0)
check(w.gamma_managed(g2, 110, at(13, 0))[3] == 'window_exit_1300', 'gamma live: 13:00 exit')
check(w.gamma_managed(Pos(8, 'gamma_blast', 100.0, at(10, 30)), 160, at(11, 0)) is None,
      'gamma live: 10:30 entry untouched')

# survives restart (state from the DB)
w._mem.clear()
check(w.scalp_managed(p, 105, 93, 105, at(12, 0))[1] == 100, 'state survives restart: stop stays at entry')
print('ALL WINDOW EXIT TESTS PASSED')
