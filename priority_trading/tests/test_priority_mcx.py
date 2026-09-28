from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from kadima import mcx_live as mcx, auto_executor as ae, storage, execution_ledger as ledger
from kadima import angel_orders, market_session_gate as gate
from kadima.config import IST

NOW = datetime(2026, 9, 28, 19, 10, tzinfo=IST)


class MCX(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(storage, 'DATA_DIR', self.tmp.name),
            patch.object(storage, 'DB_PATH', str(Path(self.tmp.name)/'db.sqlite')),
            patch.object(storage, '_local', threading.local()),
            patch.dict(os.environ, KADIMA_MCX_LIVE='1', KADIMA_PRIORITY_TRADING='1', KADIMA_MCX_CAPITAL_INR='15000'),
            patch.object(mcx, 'now_ist', return_value=NOW),
            patch.object(mcx.capital_guard, 'available_equity', return_value=15000),
            patch.object(mcx.capital_guard, 'get_lot_size', return_value=10),
            patch.object(mcx.exit_manager, 'realized_pnl_today', return_value=0),
            patch.object(mcx.audit, 'log'), patch.object(ae.SESSION, 'ensure', side_effect=AssertionError('Network forbidden'))]
        for p in self.patches: p.start()
        self.addCleanup(self.cleanup)
        self.ex = ae.AutoExecutor(); self.worker = mcx.Worker(self.ex, NS(rows=[]))
        self.row = dict(root='CRUDEOIL', direction='CE', symbol='CRUDEOPTIONCE', token='100',
            lot_size=10, ask=100, stop=85, baseline_at=NOW.isoformat(), score=88,
            underlying_stop=7000, ts=NOW.isoformat(), quote_at=NOW.isoformat(),
            trade_at=NOW.isoformat(), future_quote_at=NOW.isoformat())

    def cleanup(self):
        con = getattr(storage._local, 'conn', None)
        if con: con.close()
        for p in reversed(self.patches): p.stop()

    def test_only_one_affordable_option_lot_and_ack_is_not_fill(self):
        with patch.object(mcx, 'global_blocks', return_value=[]), \
             patch.object(angel_orders, 'place_limit_order', return_value=angel_orders.OrderResult(True,'MCX1','ok')) as place:
            result = self.worker._enter(self.row, NOW)
            self.assertTrue(result['pending'])
            self.assertEqual(place.call_args.kwargs['quantity'], 10)
            self.assertEqual(place.call_args.kwargs['exchange'], 'MCX')
            self.assertEqual(next(iter(self.ex.state.positions.values())).remaining_qty, 0)
            self.assertEqual(self.worker._enter(self.row, NOW)['ok'], False)
            self.assertEqual(place.call_count, 1)

    def test_existing_equity_exposure_consumes_shared_headroom(self):
        p = ae.Position(90,'gamma_blast','NIFTY','CE','NIFTYCE','99','NFO',1400,NOW,1,10,10,
                        status='open',structural_stop=1300,effective_stop=1300)
        self.ex.state.positions['90'] = p
        with patch.object(mcx, 'global_blocks', return_value=[]), patch.object(ledger, 'submit') as submit:
            self.assertFalse(self.worker._enter(self.row, NOW)['ok']); submit.assert_not_called()

    def test_unaffordable_lot_cannot_be_fractional(self):
        self.row['ask'] = 2000; self.row['stop'] = 1800
        with patch.object(mcx, 'global_blocks', return_value=[]), patch.object(ledger, 'submit') as submit:
            out = self.worker._enter(self.row, NOW)
            self.assertEqual(out['reason'], 'mcx_one_lot_cost_exceeds_capital'); submit.assert_not_called()

    def test_pause_during_funds_call_blocks_before_intent(self):
        with patch.object(mcx, 'global_blocks', side_effect=[[], ['auto_paused']]), \
             patch.object(ledger, 'submit') as submit:
            self.assertEqual(self.worker._enter(self.row, NOW)['reason'], 'auto_paused')
            submit.assert_not_called(); self.assertEqual(self.ex.state.positions, {})

    def test_source_quote_ages_out_while_funds_are_checked(self):
        self.row['quote_at'] = (NOW-timedelta(seconds=91)).isoformat()
        with patch.object(mcx, 'global_blocks', return_value=[]), patch.object(ledger, 'submit') as submit:
            self.assertEqual(self.worker._enter(self.row, NOW)['reason'], 'mcx_quote_expired_before_submission')
            submit.assert_not_called()

    def test_holiday_cutoff_kill_and_manual_pause_still_apply(self):
        self.ex._positions_verified = True; self.ex._reconcile_blocked = False
        with patch.object(mcx, 'macro_clear', return_value=True), \
             patch.object(mcx.celestial, 'enabled', return_value=True), \
             patch.object(mcx.celestial, 'read_config', return_value=dict(auto_trade_enabled=True, kill_latched=False)) as cfg, \
             patch.object(mcx.config, 'ORDER_PLACEMENT_ENABLED', True), \
             patch.object(mcx.killswitch, 'engaged', return_value=False), \
             patch.object(gate, 'entry_block', return_value=None):
            self.assertEqual(mcx.global_blocks(self.ex, NOW), [])
            self.assertIn('outside_mcx_entry_window', mcx.global_blocks(self.ex, NOW.replace(hour=22,minute=35)))
            self.assertIn('mcx_non_trading_day', mcx.global_blocks(self.ex, NOW.replace(month=10,day=2)))
            cfg.return_value.update(auto_trade_enabled=False, kill_latched=True)
            self.assertIn('auto_paused', mcx.global_blocks(self.ex, NOW))
            self.assertIn('kill_latched', mcx.global_blocks(self.ex, NOW))


if __name__ == '__main__': unittest.main()
