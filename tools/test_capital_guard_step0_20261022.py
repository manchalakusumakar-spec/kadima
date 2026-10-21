"""Step 0 (22-Oct-2026): cached Angel One balance reads and single-count headroom."""
import os, sys, tempfile
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from types import SimpleNamespace
from unittest.mock import patch

from kadima import capital_guard as G

RATE = Exception("Couldn't parse the JSON response received from the server: "
                 "b'Access denied because of exceeding access rate'")


class FakeApi:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def rmsLimit(self):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.last
        self.last = item
        if isinstance(item, Exception):
            raise item
        return {'status': True, 'data': {'availablecash': str(item)}}


class Clock:
    def __init__(self): self.t = 1000.0
    def monotonic(self): return self.t
    def sleep(self, s): self.t += s


def run(script, fn):
    api, clock = FakeApi(script), Clock()
    G._rms.update(value=None, at=0.0, reserved=0.0)
    with patch.object(G, 'SESSION', SimpleNamespace(ensure=lambda: None, api=api)), \
            patch.object(G, 'time', clock), patch.object(G.audit, 'log', lambda *a, **k: None):
        return fn(api, clock)


def test_fresh_read_is_reused_for_5s():
    def body(api, clock):
        assert G.broker_available_funds() == 21000.0
        clock.t += 4.9
        assert G.broker_available_funds() == 21000.0
        assert api.calls == 1
        clock.t += 0.2
        G.broker_available_funds()
        assert api.calls == 2
    run([21000, 20000], body)


def test_rate_limit_uses_last_good_within_60s():
    def body(api, clock):
        G.broker_available_funds()
        clock.t += 30
        assert G.broker_available_funds() == 21000.0      # failed read, cached fallback, no sleep
        assert clock.t == 1030.0
    run([21000, RATE], body)


def test_rate_limit_with_no_cache_retries_once():
    def body(api, clock):
        assert G.broker_available_funds() == 19500.0
        assert api.calls == 2 and clock.t == 1001.0
    run([RATE, 19500], body)


def test_two_failures_and_no_cache_fail_closed():
    run([RATE, RATE], lambda api, clock: G.broker_available_funds() is None or (_ for _ in ()).throw(AssertionError))


def test_stale_beyond_60s_is_not_used():
    def body(api, clock):
        G.broker_available_funds()
        clock.t += 61
        assert G.broker_available_funds() is None
    run([21000, RATE, RATE], body)


def test_reservation_reduces_cached_cash_until_fresh_read():
    def body(api, clock):
        G.broker_available_funds()
        G.reserve_funds(7000)
        assert G.broker_available_funds() == 14000.0
        clock.t += 6
        assert G.broker_available_funds() == 14100.0      # fresh read replaces the reservation
    run([21000, 14100], body)


def _lots(cash, price, lot, positions, requested=1):
    def body(api, clock):
        with patch.object(G, 'get_lot_size', lambda s, t: lot):
            return G.max_affordable_lots(price, 'X', '1', positions, requested)
    return run([cash] * 5, body)


def test_open_positions_not_counted_twice():
    # 21-Oct 09:52: Rs 8,478 free with Rs 12,275 already deployed; SENSEX PE 366.4 x 20 = 7,328.
    pos = {'a': SimpleNamespace(symbol='NIFTY27OCT2623100PE', token='51357', entry_price=188.85, remaining_qty=65)}
    allowed, why = _lots(8478, 366.4, 20, pos)
    assert (allowed, why) == (1, 'ok'), (allowed, why)


def test_still_refuses_when_cash_too_small():
    allowed, why = _lots(5000, 366.4, 20, {})
    assert allowed == 0 and why.startswith('single_lot_cost_7328_exceeds_headroom'), why


def test_two_lots_size_down_to_one():
    allowed, why = _lots(10000, 339.15, 20, {}, requested=2)
    assert (allowed, why) == (1, 'sized_down_from_2_to_1'), (allowed, why)


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            try:
                fn(); print('PASS', name)
            except Exception as exc:  # noqa: BLE001
                failed += 1; print('FAIL', name, repr(exc))
    sys.exit(1 if failed else 0)
