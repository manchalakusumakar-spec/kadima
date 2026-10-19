"""19-Oct protection features: CF entry basis, near-stop checks, re-entry, rejection watchdog."""
import json, os, sys, tempfile
os.environ['KADIMA_DATA_DIR'] = os.environ.get('KADIMA_DATA_DIR') or tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from kadima import protection as P, trend_rules as T, storage

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 19, 13, 0, tzinfo=IST)


def features(**kw):
    T._cfg.update(value=None)
    with open(os.path.join(os.environ['KADIMA_DATA_DIR'], 'trend_rules.json'), 'w') as f:
        json.dump(kw, f)
    T._cfg.update(value=None)


def test_entry_basis():
    cf = {'tradingsymbol': 'NIFTY19OCT2622900CE', 'producttype': 'CARRYFORWARD',
          'buyavgprice': '0', 'totalbuyavgprice': '0', 'cfbuyavgprice': '38.77'}
    features(cf_protect='shadow')
    assert P.entry_basis(cf) == 0                       # shadow keeps old behaviour
    features(cf_protect='live')
    assert P.entry_basis(cf) == 38.77
    assert P.entry_basis({'buyavgprice': '23.40', 'cfbuyavgprice': '30'}) == 23.40
    assert P.entry_basis({'buyavgprice': 'x'}) == 0


def test_monitor_interval():
    class Tracker:
        def __init__(self, ltp): self.ltp = ltp; self._lock = __import__('threading').Lock(); self._hist = {}
        def latest(self, token, age): return self.ltp
    pos = SimpleNamespace(signal_id=1, status='open', remaining_qty=65, kind='gamma_blast',
                          token='t', effective_stop=95.0)
    ex = SimpleNamespace(state=SimpleNamespace(positions={'1': pos}, premium_tracker=Tracker(100.0)))
    features(monitor='live')
    assert P.monitor_interval(ex, NOW) == P.FAST_SEC       # 5% away, 2x3% noise = 6%
    ex.state.premium_tracker.ltp = 120.0
    assert P.monitor_interval(ex, NOW) == P.SLOW_SEC
    ex.state.premium_tracker.ltp = 100.0
    features(monitor='shadow')
    assert P.monitor_interval(ex, NOW) == P.SLOW_SEC


def _position(sid, entry, exit_, closed_at):
    c = storage.conn()
    with c:
        c.execute('INSERT OR REPLACE INTO positions (signal_id, kind, index_name, direction, symbol, '
                  'token, exchange, entry_price, entry_ts, lots, lot_size, remaining_qty, status, '
                  'exit_price, exit_ts, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (sid, 'gamma_blast', 'NIFTY', 'PE', 'S', 't', 'NFO', entry, closed_at.isoformat(),
                   1, 65, 0, 'closed', exit_, closed_at.isoformat(), 'structural_stop_breached'))


def test_reentry_flow():
    sent = set()
    seen = lambda sid: sid in sent
    origin = storage.insert_signal('NIFTY', 'CONFIRMED', 80, 'PE', '{}', kind='gamma_blast')
    sent.add(origin)
    _position(origin, 45.28, 35.52, NOW - timedelta(minutes=10))
    features(mode='live', reentry='shadow')
    assert P.reentry_signal('gamma_blast', 'NIFTY', 'PE', origin, 'CONFIRMED', 80, seen, NOW) is None
    features(mode='shadow', reentry='live')               # needs trend rules live too
    assert P.reentry_signal('gamma_blast', 'NIFTY', 'PE', origin, 'CONFIRMED', 80, seen, NOW) is None
    features(mode='live', reentry='live')
    assert P.reentry_signal('fast_momentum', 'NIFTY', 'PE', origin, 'X', 80, seen, NOW) is None
    first = P.reentry_signal('gamma_blast', 'NIFTY', 'PE', origin, 'CONFIRMED', 80, seen, NOW)
    assert first and first != origin
    # minted but rejected by a later gate -> same id reused, no new mint
    assert P.reentry_signal('gamma_blast', 'NIFTY', 'PE', origin, 'CONFIRMED', 80, seen, NOW) == first
    sent.add(first)
    _position(first, 40.0, 36.0, NOW - timedelta(minutes=2))       # cooldown not over
    assert P.reentry_signal('gamma_blast', 'NIFTY', 'PE', first, 'CONFIRMED', 80, seen, NOW) is None
    _position(first, 40.0, 36.0, NOW - timedelta(minutes=6))
    second = P.reentry_signal('gamma_blast', 'NIFTY', 'PE', first, 'CONFIRMED', 80, seen, NOW)
    assert second and second not in (origin, first)
    sent.add(second)
    _position(second, 40.0, 30.0, NOW - timedelta(minutes=30))
    assert P.reentry_signal('gamma_blast', 'NIFTY', 'PE', origin, 'CONFIRMED', 80, seen, NOW) is None  # max 2
    # a profitable exit never triggers a re-entry
    o2 = storage.insert_signal('NIFTY', 'CONFIRMED', 80, 'PE', '{}', kind='gamma_blast')
    sent.add(o2)
    _position(o2, 40.0, 60.0, NOW - timedelta(minutes=30))
    assert P.reentry_signal('gamma_blast', 'NIFTY', 'PE', o2, 'CONFIRMED', 80, seen, NOW) is None


def test_watch_rejections():
    features(watchdog='shadow')
    book = [SimpleNamespace(order_id='A1', status='rejected', raw={
                'orderid': 'A1', 'ordertag': '', 'tradingsymbol': 'NIFTY19OCT2622900CE',
                'transactiontype': 'SELL', 'producttype': 'CARRYFORWARD', 'quantity': '650',
                'price': '21.95', 'text': 'RMS: margin exceeds'}),
            SimpleNamespace(order_id='A2', status='rejected', raw={
                'orderid': 'A2', 'ordertag': 'K0123456789abcde', 'text': 'price band'}),
            SimpleNamespace(order_id='A3', status='complete', raw={'orderid': 'A3'})]
    new = P.watch_rejections(fetch=lambda: book, now=NOW, force=True)
    assert [(n['order_id'], n['source']) for n in new] == [('A1', 'MANUAL'), ('A2', 'KADIMA')]
    assert new[0]['reason'] == 'RMS: margin exceeds'
    assert P.watch_rejections(fetch=lambda: book, now=NOW, force=True) == []   # once only


def test_broker_stop_plan_shadow():
    class Tracker:
        _lock = __import__('threading').Lock(); _hist = {}
    pos = SimpleNamespace(signal_id=9, status='open', effective_stop=80.0, entry_price=100.0,
                          token='t', symbol='S')
    plan = P.broker_stop_plan(pos, 100.0, Tracker(), NOW)
    assert plan['trigger'] == 77.6 and plan['limit'] == 77.5    # 80 - 3% noise width
    pos.effective_stop = 60.0                                     # never below the 35% cap
    P._last_plan.clear()
    assert P.broker_stop_plan(pos, 70.0, Tracker(), NOW)['trigger'] == 65.0


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
    print('protection tests passed')
