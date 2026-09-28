from datetime import datetime
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from kadima import owned_watchdog as wd, eod_watchdog as runner, execution_owner
from kadima import auto_executor as ae, execution_ledger as ledger, angel_orders, storage
from kadima.config import IST

NOW = datetime(2026, 9, 28, 15, 25, tzinfo=IST)


def position(sid=1, exchange='NFO', kind='gamma_blast'):
    return ae.Position(sid, kind, 'NIFTY', 'CE', 'NIFTYOPTIONCE', str(sid), exchange,
                       100, NOW, 1, 65, 65, status='open', structural_stop=95, effective_stop=95)


def order(sid=1, filled=65, side='BUY', **kw):
    return dict(dict(signal_id=sid, filled=filled, side=side, state='terminal',
                     dry_run=0, order_id='B'+str(sid)), **kw)


def broker(p, qty=None, product='INTRADAY'):
    return dict(exchange=p.exchange, symboltoken=p.token, tradingsymbol=p.symbol,
                producttype=product, netqty=p.remaining_qty if qty is None else qty)


class Ownership(unittest.TestCase):
    def test_manual_carryforward_and_manual_adopted_are_excluded(self):
        owned = position(); manual = position(2, kind='MANUAL_ADOPTED')
        observed = position(3)
        result = wd.owned_positions({'1':owned, '2':manual}, [order()],
            [broker(owned), broker(manual), broker(observed, product='CARRYFORWARD')], [], NOW)
        self.assertEqual(result, [owned])

    def test_no_dry_run_fill_or_ledger_quantity_mismatch(self):
        p = position()
        for rows in ([order(dry_run=1)], [order(filled=64)], [], [order(side='SELL')]):
            self.assertEqual(wd.owned_positions({'1':p}, rows, [broker(p)], [], NOW), [])

    def test_manual_same_contract_quantity_or_product_blocks(self):
        p = position()
        for row in (broker(p, qty=130), broker(p, product='CARRYFORWARD'), broker(p, qty=-65)):
            with self.assertRaisesRegex(ValueError, 'quantity_or_product'):
                wd.owned_positions({'1':p}, [order()], [row], [], NOW)

    def test_pending_manual_and_local_orders_block(self):
        p = position()
        with self.assertRaisesRegex(ValueError, 'broker_pending'):
            wd.owned_positions({'1':p}, [order()], [broker(p)], [NS(status='open')], NOW)
        with self.assertRaisesRegex(ValueError, 'local_pending'):
            wd.owned_positions({'1':p}, [order(state='unknown')], [broker(p)], [], NOW)

    def test_partial_sell_ownership_and_mcx_exit_clock(self):
        p = position(); p.remaining_qty = 40
        self.assertEqual(wd.owned_positions({'1':p}, [order(), order(side='SELL', filled=25)],
                         [broker(p)], [], NOW), [p])
        p.exchange = 'MCX'
        self.assertEqual(wd.owned_positions({'1':p}, [order(filled=40)], [broker(p)], [], NOW), [])
        self.assertEqual(wd.owned_positions({'1':p}, [order(filled=40)], [broker(p)], [],
                         NOW.replace(hour=22, minute=40)), [p])

    def test_duplicate_contracts_and_unreadable_books_block(self):
        p = position()
        for books in (None, [broker(p), broker(p)]):
            with self.assertRaises(ValueError): wd.owned_positions({'1':p}, [order()], books, [], NOW)


class RealLedger(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [patch.object(storage, 'DATA_DIR', self.temp.name),
            patch.object(storage, 'DB_PATH', str(Path(self.temp.name)/'db.sqlite')),
            patch.object(storage, '_local', threading.local()), patch.object(ae.audit, 'log'),
            patch.object(ae.alerts, 'enqueue'), patch.object(wd, 'now_ist', return_value=NOW),
            patch.object(angel_orders, 'fetch_order_book', return_value=[]),
            patch.object(ae.SESSION, 'ensure', side_effect=AssertionError('Network forbidden'))]
        for p in self.patches: p.start()
        self.addCleanup(self.cleanup)
        self.ex = ae.AutoExecutor(); self.pos = position()
        self.ex.state.positions['1'] = self.pos
        con = ledger.connection()
        with con:
            con.execute("INSERT INTO execution_orders VALUES ('Kbuy',1,'BUY',65,'B1','terminal',65,6500,?,0)", (NOW.isoformat(),))
            storage.upsert_position(self.pos, _connection=con)

    def cleanup(self):
        c = getattr(storage._local, 'conn', None)
        if c: c.close()
        for p in reversed(self.patches): p.stop()

    def test_acknowledged_exit_stays_pending_and_is_not_repeated(self):
        from kadima import cas_orders
        with patch.object(ae, 'fetch_broker_positions', return_value=[broker(self.pos)]), \
             patch.object(cas_orders, 'fetch_quote', return_value=NS(bid=99,quote_at=NOW.isoformat())), \
             patch.object(angel_orders, 'place_limit_order', return_value=angel_orders.OrderResult(True,'S1','ok')) as place:
            result = wd.cycle(self.ex)
            self.assertFalse(result['closure_verified'])
            self.assertEqual(self.pos.remaining_qty, 65)
            self.assertEqual(self.pos.status, 'exit_pending')
            self.assertEqual(place.call_args.kwargs['quantity'], 65)
            with self.assertRaises(ValueError): wd.cycle(self.ex)
            self.assertEqual(place.call_count, 1)

    def test_stale_bid_never_uses_entry_or_peak(self):
        from kadima import cas_orders
        with patch.object(ae, 'fetch_broker_positions', return_value=[broker(self.pos)]), \
             patch.object(cas_orders, 'fetch_quote', return_value=None), \
             patch.object(angel_orders, 'place_limit_order') as place:
            result = wd.cycle(self.ex)
            self.assertFalse(result['ok']); place.assert_not_called()

    def test_restore_does_not_adopt_manual_positions_or_start_workers(self):
        other = ae.AutoExecutor()
        with patch.object(other, 'reconcile_on_startup') as adopt, patch.object(other, 'start_monitor_thread') as thread:
            wd.restore(other)
            self.assertEqual(list(other.state.positions), ['1'])
            adopt.assert_not_called(); thread.assert_not_called()


class SingleWriter(unittest.TestCase):
    def test_lifetime_lock_excludes_second_owner(self):
        if os.getuid() != 0:
            self.skipTest('root-owned directory fixture')
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name); parent.chmod(0o755)
            lock = parent/'owner.lock'; lock.touch(mode=0o600)
            with patch.object(execution_owner, 'LOCK', lock), execution_owner.acquire():
                with self.assertRaises(BlockingIOError):
                    with execution_owner.acquire(): self.fail('second owner admitted')

    def test_busy_engine_requests_guarded_api_without_fallback(self):
        with patch.object(execution_owner, 'acquire', side_effect=BlockingIOError), \
             patch.object(runner, 'request_engine', return_value=dict(ok=True,due_owned_positions=0)) as api, \
             patch.object(wd, 'restore') as restore, patch.object(runner.audit, 'log'):
            self.assertEqual(runner.main(), 0)
            api.assert_called_once(); restore.assert_not_called()

    def test_unresponsive_engine_is_alerted_never_raced(self):
        with patch.object(execution_owner, 'acquire', side_effect=BlockingIOError), \
             patch.object(runner, 'request_engine', side_effect=TimeoutError), \
             patch.object(wd, 'restore') as restore, patch.object(runner.alerts, 'send') as alert, \
             patch.object(runner.audit, 'log'):
            self.assertEqual(runner.main(), 1)
            restore.assert_not_called(); alert.assert_called_once()


if __name__ == '__main__': unittest.main()
