"""Regression checks for the exact AWS feed preserved in the stock-fast release.

No production package import, SDK initialization, socket connection or order.
Feed dependencies are isolated stubs. Installer checks use temporary source trees.
"""
import hashlib
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
AWS_FEED_BLOB = '8a9f8ea0aa5a708739c9297583badabc85fc2c2a'
FIFO_BLOB = '9886d951be6997e0ddf8fdda640529f5f751a25c'


def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PreservedFeedTests(unittest.TestCase):
    def setUp(self):
        name = '_kadima_aws_feed_regression'
        parent = types.ModuleType(name)
        parent.__path__ = []
        modules = {name: parent}
        for child in ('audit', 'storage', 'config', 'session', 'utils'):
            module = types.ModuleType(name + '.' + child)
            modules[module.__name__] = module
            setattr(parent, child, module)
        self.log = parent.audit.log = Mock()
        self.persist = parent.storage.insert_ticks = Mock()
        self.session = parent.session.SESSION = Mock()
        parent.utils.in_market_hours = lambda: False
        for key, value in dict(ANGEL_API_KEY='fixture', ANGEL_CLIENT_ID='fixture',
                IST=ZoneInfo('Asia/Kolkata'), WS_HEARTBEAT_SEC=30, WS_MAX_RETRIES=0,
                WS_RECONNECT_BASE_SEC=1, WS_RECONNECT_MAX_SEC=30).items():
            setattr(parent.config, key, value)
        self.registry = patch.dict(sys.modules, modules)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.module = load_file(name + '.feed', ROOT / 'kadima/feed.py')
        self.callback = Mock()
        self.feed = self.module.TickFeed(self.callback)
        self.feed._last_tick_at = 11.0
        self.clock = patch.object(self.module.time, 'monotonic', return_value=77.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def message(self, **changes):
        result = dict(token='26000', last_traded_price=128400,
            volume_trade_for_the_day=120000,
            best_5_buy_data=[{'price': 128390}], best_5_sell_data=[{'price': 128410}])
        result.update(changes)
        return result

    def rejected(self, message, reason):
        self.feed._handle_tick(None, message)
        self.callback.assert_not_called()
        self.assertTrue(self.feed._writer_q.empty())
        self.assertEqual(self.feed._last_tick_at, 11.0)
        self.session.ensure.assert_not_called()
        self.persist.assert_not_called()
        self.assertEqual(self.log.call_args.args[0], 'tick_rejected')
        self.assertEqual(self.log.call_args.kwargs['reason'], reason)

    def test_source_matches_observed_aws_blob_exactly(self):
        self.assertEqual(blob((ROOT / 'kadima/feed.py').read_bytes()), AWS_FEED_BLOB)

    def test_valid_tick_preserves_schema_scaling_and_cumulative_volume(self):
        self.feed._handle_tick(None, self.message())
        self.callback.assert_called_once()
        tick = self.callback.call_args.args[0]
        self.assertEqual(set(tick), {'ts', 'token', 'ltp', 'volume', 'bid', 'ask'})
        self.assertEqual(tick['token'], '26000')
        self.assertEqual(tick['ltp'], 1284)
        self.assertEqual(tick['volume'], 120000)
        self.assertAlmostEqual(tick['bid'], 1283.9)
        self.assertAlmostEqual(tick['ask'], 1284.1)
        self.assertEqual(str(tick['ts'].tzinfo), 'Asia/Kolkata')
        self.assertEqual(self.feed._last_tick_at, 77.0)
        self.assertEqual(self.feed._writer_q.get_nowait(),
            (tick['ts'].isoformat(), '26000', None, 1284, 120000, 1283.9, 1284.1))
        self.persist.assert_not_called()
        self.session.ensure.assert_not_called()

    def test_integer_token_is_preserved_as_string(self):
        self.feed._handle_tick(None, self.message(token=26000))
        self.assertEqual(self.callback.call_args.args[0]['token'], '26000')

    def test_string_token_is_stripped(self):
        self.feed._handle_tick(None, self.message(token=' 26000 '))
        self.assertEqual(self.callback.call_args.args[0]['token'], '26000')

    def test_missing_token_is_rejected_before_heartbeat(self):
        message = self.message(); del message['token']
        self.rejected(message, 'missing_token')

    def test_invalid_token_types_are_rejected(self):
        for token in (None, True, False, {}, [], 26.0, '', '   '):
            with self.subTest(token=repr(token)):
                self.rejected(self.message(token=token), 'missing_token')

    def test_missing_price_is_rejected(self):
        message = self.message(); del message['last_traded_price']
        self.rejected(message, 'invalid_price')

    def test_boolean_price_is_rejected(self):
        for price in (True, False):
            with self.subTest(price=price):
                self.rejected(self.message(last_traded_price=price), 'invalid_price')

    def test_zero_negative_and_underflow_prices_are_rejected(self):
        for price in (0, -1, '-50', 5e-324):
            with self.subTest(price=price):
                self.rejected(self.message(last_traded_price=price), 'invalid_price')

    def test_nonfinite_prices_are_rejected(self):
        for price in (float('nan'), float('inf'), float('-inf'), 'NaN', 'inf', '1e9999'):
            with self.subTest(price=repr(price)):
                self.rejected(self.message(last_traded_price=price), 'invalid_price')

    def test_unparseable_and_overflow_prices_are_rejected(self):
        for price in (None, '', 'not-a-price', {}, [], 10**1000):
            with self.subTest(price_type=type(price).__name__):
                self.rejected(self.message(last_traded_price=price), 'invalid_price')

    def test_numeric_string_price_remains_supported(self):
        self.feed._handle_tick(None, self.message(last_traded_price='128400'))
        self.assertEqual(self.callback.call_args.args[0]['ltp'], 1284)

    def test_bad_tick_after_valid_tick_does_not_refresh_observation(self):
        self.feed._handle_tick(None, self.message())
        self.feed._writer_q.get_nowait()
        with patch.object(self.module.time, 'monotonic', return_value=88):
            self.feed._handle_tick(None, self.message(last_traded_price=float('nan')))
        self.callback.assert_called_once()
        self.assertEqual(self.feed._last_tick_at, 77)
        self.assertTrue(self.feed._writer_q.empty())

    def test_full_queue_keeps_existing_nonblocking_callback_behavior(self):
        self.feed._writer_q = queue.Queue(maxsize=1)
        self.feed._writer_q.put_nowait(('already-full',))
        self.feed._handle_tick(None, self.message())
        self.callback.assert_called_once()
        self.assertEqual(self.feed._last_tick_at, 77)
        self.log.assert_called_with('tick_queue_full')
        self.assertEqual(self.feed._writer_q.get_nowait(), ('already-full',))

    def test_malformed_depth_does_not_refresh_heartbeat(self):
        self.feed._handle_tick(None, self.message(best_5_buy_data=['malformed']))
        self.callback.assert_not_called()
        self.assertEqual(self.feed._last_tick_at, 11)
        self.assertTrue(self.feed._writer_q.empty())
        self.assertEqual(self.log.call_args.args[0], 'tick_parse_error')

    def test_valid_tick_after_invalid_tick_recovers(self):
        self.rejected(self.message(last_traded_price=0), 'invalid_price')
        self.feed._handle_tick(None, self.message())
        self.callback.assert_called_once()
        self.assertEqual(self.feed._last_tick_at, 77)

    def test_subscription_api_is_unchanged_and_does_not_connect(self):
        self.feed.subscribe(1, ['26000'])
        self.assertEqual(self.feed._token_list(), [{'exchangeType': 1, 'tokens': ['26000']}])
        self.assertIsNone(self.feed._ws)
        self.session.ensure.assert_not_called()


class PreservedFeedInstallerTests(unittest.TestCase):
    def setUp(self):
        name = '_kadima_aws_feed_installer'
        self.registry = patch.dict(sys.modules, {})
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.installer = load_file(name, ROOT / 'stock_fast_update/install.py')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.release, self.target = root/'release', root/'live'
        fixture = {
            'run.py': b'from kadima import feed, rate_limiter\n',
            'kadima/full_universe_scanner.py': b'# fixture scanner\n',
            'kadima/stock_fast.py': b'# fixture stock module\n',
            'kadima/stock_fast_api.py': b'# fixture API module\n',
            'kadima/__init__.py': b'',
            'kadima/feed.py': (ROOT/'kadima/feed.py').read_bytes(),
            'kadima/rate_limiter.py': (ROOT/'kadima/rate_limiter.py').read_bytes(),
        }
        for name, raw in fixture.items():
            for folder in (self.release, self.target):
                path = folder/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
        for name in ('kadima/stock_fast.py', 'kadima/stock_fast_api.py'):
            (self.target/name).unlink()

    def test_matching_preserved_feed_is_checked_but_never_replaced(self):
        changes, fingerprints = self.installer.backend_plan(self.release, self.target, (0, 0))
        self.assertNotIn('kadima/feed.py', self.installer.FILES)
        self.assertEqual(blob(fingerprints[self.target/'kadima/feed.py']), AWS_FEED_BLOB)
        self.assertNotIn(self.target/'kadima/feed.py', [c.path for c in changes])
        self.assertEqual({c.path.name for c in changes}, {'stock_fast.py', 'stock_fast_api.py'})

    def test_any_further_feed_change_still_blocks_installation(self):
        path = self.target/'kadima/feed.py'
        path.write_bytes(path.read_bytes() + b'\n# unreviewed change\n')
        with self.assertRaisesRegex(self.installer.Blocked, 'unreviewed_dependency:kadima/feed.py'):
            self.installer.backend_plan(self.release, self.target, (0, 0))

    def test_feed_change_after_planning_still_blocks(self):
        _, fingerprints = self.installer.backend_plan(self.release, self.target, (0, 0))
        path = self.target/'kadima/feed.py'
        path.write_bytes(path.read_bytes() + b'\n# changed after plan\n')
        with self.assertRaisesRegex(self.installer.Blocked, 'dependency_changed_since_plan:feed.py'):
            self.installer.check_fingerprints(fingerprints)

    def test_fifo_limiter_remains_exact_and_untouched(self):
        changes, fingerprints = self.installer.backend_plan(self.release, self.target, (0, 0))
        path = self.target/'kadima/rate_limiter.py'
        self.assertEqual(blob(fingerprints[path]), FIFO_BLOB)
        self.assertNotIn(path, [c.path for c in changes])


if __name__ == '__main__':
    unittest.main()
