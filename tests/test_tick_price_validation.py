"""Exercise the actual tick callback without an SDK connection or credentials."""
import ast
from datetime import datetime, timezone
import math
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class TickPriceValidationTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'kadima' / 'feed.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TickFeed')
        handler = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_handle_tick')
        self.audit = SimpleNamespace(log=Mock())
        self.clock = SimpleNamespace(monotonic=Mock(return_value=200.0))
        namespace = dict(math=math, queue=queue, time=self.clock, datetime=datetime,
                         IST=timezone.utc, audit=self.audit)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'actual_tick_handler', 'exec'), namespace)
        self.handle = namespace['_handle_tick']
        self.callback = Mock()
        self.feed = SimpleNamespace(_last_tick_at=123.0, _writer_q=queue.Queue(maxsize=2),
                                    on_tick=self.callback)

    def test_invalid_prices_never_reach_engine_or_refresh_feed(self):
        for price in (None, 0, -100, float('nan'), float('inf'), -float('inf'),
                      True, False, '', 'nan', 'inf', 'bad', [], {}):
            with self.subTest(price=repr(price)):
                self.handle(self.feed, None, {'token':'99926000', 'last_traded_price':price})
                self.callback.assert_not_called()
                self.assertTrue(self.feed._writer_q.empty())
                self.assertEqual(self.feed._last_tick_at, 123.0)
                self.clock.monotonic.assert_not_called()
                self.assertEqual(self.audit.log.call_args.kwargs['reason'], 'invalid_price')

    def test_missing_token_is_rejected(self):
        for token in (None, '', '   ', True, [], {}):
            self.handle(self.feed, None, {'token':token, 'last_traded_price':12345})
        self.callback.assert_not_called()
        self.assertTrue(self.feed._writer_q.empty())
        self.assertEqual(self.feed._last_tick_at, 123.0)

    def test_valid_tick_preserves_units_volume_and_depth(self):
        self.handle(self.feed, None, dict(token=' 99926000 ', last_traded_price=2345678,
            volume_trade_for_the_day=1000, best_5_buy_data=[{'price':2345600}],
            best_5_sell_data=[{'price':2345700}]))
        tick = self.callback.call_args.args[0]
        self.assertEqual((tick['token'], tick['ltp'], tick['volume'], tick['bid'], tick['ask']),
                         ('99926000', 23456.78, 1000, 23456.0, 23457.0))
        self.assertEqual(self.feed._writer_q.get_nowait()[3], 23456.78)
        self.assertEqual(self.feed._last_tick_at, 200.0)
        self.audit.log.assert_not_called()

    def test_rejected_tick_cannot_replace_last_valid_quote(self):
        state = {'spot':None, 'updates':0}
        def update(tick):
            state['spot'] = tick['ltp']
            state['updates'] += 1
        self.feed.on_tick = update
        self.handle(self.feed, None, {'token':'99926000', 'last_traded_price':2340000})
        last = self.feed._last_tick_at
        self.handle(self.feed, None, {'token':'99926000', 'last_traded_price':None})
        self.assertEqual(state, {'spot':23400, 'updates':1})
        self.assertEqual(self.feed._last_tick_at, last)
        self.handle(self.feed, None, {'token':'99926000', 'last_traded_price':2340100})
        self.assertEqual(state, {'spot':23401, 'updates':2})

    def test_full_storage_queue_keeps_valid_live_callback(self):
        self.feed._writer_q.put('first')
        self.feed._writer_q.put('second')
        self.handle(self.feed, None, {'token':'99926000', 'last_traded_price':12345})
        self.callback.assert_called_once()
        self.audit.log.assert_called_once_with('tick_queue_full')


if __name__ == '__main__':
    unittest.main()
