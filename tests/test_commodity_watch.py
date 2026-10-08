from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kadima import commodity_watch as c
from kadima.config import IST

NOW = datetime(2026, 9, 16, 22, 0, tzinfo=IST)


def master():
    rows = [dict(name='CRUDEOIL', instrumenttype='FUTCOM', token='1',
                 symbol='CRUDEOIL21SEP26FUT', strike='0')]
    rows += [dict(name='CRUDEOIL', instrumenttype='OPTFUT', token=str(i+2),
                  symbol='CRUDEOIL21SEP26'+str(strike)+side, strike=str(strike*100))
             for i, (strike, side) in enumerate([(9800, 'CE'), (9800, 'PE'), (10000, 'CE')])]
    return [dict(row, exch_seg='MCX', expiry='21SEP2026', lotsize='100') for row in rows]


def response(tokens):
    return dict(status=True, data=dict(fetched=[dict(symbolToken=token, exchange='MCX',
                ltp=9815 if token == '1' else 50, exchFeedTime='16-Sep-2026 22:00:00',
                exchTradeTime='16-Sep-2026 22:00:00') for token in tokens]))


class Monitor(unittest.TestCase):
    def setUp(self):
        # Fixed historical quotes and snapshots share one deterministic clock.
        # Receipt-delay/future/stale behavior is covered by the production suite.
        clock = patch.object(c.monotonic_clock, 'monotonic', return_value=0.0)
        clock.start()
        self.addCleanup(clock.stop)

    def test_evening_window_and_user_cap_do_not_change_equity_hours(self):
        from kadima.utils import in_market_hours
        self.assertTrue(c.monitoring_window(NOW))
        self.assertFalse(in_market_hours(NOW))
        for stamp in (NOW.replace(hour=8), NOW.replace(hour=23, minute=30),
                      NOW+timedelta(days=3)):
            self.assertFalse(c.monitoring_window(stamp))

    def test_both_future_and_option_quotes_use_read_only_batches(self):
        fetch = Mock(side_effect=response)
        monitor = c.CommodityWatch()
        monitor.poll(master(), fetch, NOW)
        result = monitor.snapshot(NOW)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([row['token'] for row in result['contracts']], ['1', '2', '3'])
        self.assertEqual(result['status'], 'observing')
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(result['context_monitoring'], '24/7')

    def test_missing_trade_time_stale_trade_and_wrong_exchange_are_not_fresh(self):
        contracts = c.select_futures(c.catalog(master(), NOW.date()))
        for field, value in [('exchTradeTime', '01-Jan-1970 05:30:00'),
                             ('exchTradeTime', '08-Sep-2026 22:00:00'),
                             ('exchFeedTime', '16-Sep-2026 22:01:00'),
                             ('exchange', 'NFO'), ('ltp', float('nan'))]:
            with self.subTest(field=field, value=value):
                data = response(['1'])
                data['data']['fetched'][0][field] = value
                rows = c.quote_rows(contracts, data, NOW)
                self.assertFalse(rows[0]['fresh'])
                self.assertEqual(c.select_options(c.catalog(master(), NOW.date()), rows), [])

    def test_retry_failure_never_refreshes_old_quotes(self):
        monitor = c.CommodityWatch()
        monitor.poll(master(), response, NOW)
        monitor.poll(master(), Mock(side_effect=RuntimeError('SECRET')), NOW+timedelta(seconds=30))
        result = monitor.snapshot(NOW+timedelta(seconds=30))
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(any(row['fresh'] for row in result['contracts']))
        self.assertNotIn('SECRET', json.dumps(result))

    def test_closed_session_never_calls_broker(self):
        monitor = c.CommodityWatch()
        fetch = Mock()
        monitor.poll(master(), fetch, NOW.replace(hour=23, minute=31))
        fetch.assert_not_called()

    def test_old_tokens_expiry_and_disallowed_master_rows_filtered(self):
        rows = master()
        rows[0]['expiry'] = '15SEP2026'
        rows[1]['exch_seg'] = 'NFO'
        rows[2]['permitted_to_trade'] = False
        self.assertEqual([row['token'] for row in c.catalog(rows, NOW.date())], ['4'])

    def test_api_rechecks_age_and_keeps_bad_price_not_fresh(self):
        monitor = c.CommodityWatch()
        monitor.poll(master(), response, NOW)
        monitor.rows[0]['fresh'] = False
        monitor.rows[0]['ltp'] = None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'macro.json'
            path.write_text(json.dumps(dict(commodities=monitor.snapshot(NOW))))
            result = c.read_snapshot(path, NOW)
            self.assertFalse(result['contracts'][0]['fresh'])
            result = c.read_snapshot(path, NOW+timedelta(minutes=5))
            self.assertEqual(result['status'], 'stale')
            self.assertFalse(any(row['fresh'] for row in result['contracts']))

    def test_gamma_watch_requires_full_window_volume_oi_and_spread(self):
        monitor = c.CommodityWatch()
        for i in range(11):
            stamp = NOW+timedelta(seconds=30*i)
            def fetch(tokens):
                data = response(tokens)
                for row in data['data']['fetched']:
                    row.update(exchFeedTime=stamp.strftime('%d-%b-%Y %H:%M:%S'),
                               exchTradeTime=stamp.strftime('%d-%b-%Y %H:%M:%S'))
                    if row['symbolToken'] != '1':
                        price = 50+i*.5
                        row.update(ltp=price, tradeVolume=1000+min(i,8)*10+max(0,i-8)*50,
                                   opnInterest=1000+i, depth=dict(buy=[dict(price=price-.1)],
                                                               sell=[dict(price=price+.1)]))
                return data
            monitor.poll(master(), fetch, stamp)
            state = monitor.snapshot(stamp)
            options = [row for row in state['contracts'] if row['instrumenttype'] == 'OPTFUT']
            if i < 10:
                self.assertTrue(all(row['gamma_watch']['status'] == 'warming_up' for row in options))
        self.assertEqual(options[0]['gamma_watch']['status'], 'premium_blast_watch')
        self.assertAlmostEqual(options[0]['gamma_watch']['premium_move_pct'], 10)
        self.assertFalse(options[0]['gamma_watch']['eligible_for_execution'])
        self.assertIsNone(options[0]['gamma_watch']['gamma_score'])
        self.assertFalse(options[0]['gamma_watch']['directional_oi_confirmed'])
        self.assertEqual(monitor.snapshot(stamp+timedelta(seconds=91))['contracts'][1]['gamma_watch']['status'], 'stale')
        # A failed poll resets the observation window; an old burst cannot carry over.
        monitor.poll(master(), Mock(side_effect=RuntimeError()), stamp+timedelta(seconds=30))
        self.assertEqual(monitor.windows, {})

    def test_missing_or_decreasing_volume_never_invents_gamma_activity(self):
        monitor = c.CommodityWatch()
        monitor.poll(master(), response, NOW)
        self.assertEqual(monitor.snapshot(NOW)['contracts'][1]['gamma_watch']['status'], 'data_unavailable')


if __name__ == '__main__':
    unittest.main()
