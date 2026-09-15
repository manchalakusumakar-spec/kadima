"""Regression coverage for partial entry exits, Shield 65 and release completeness."""
import ast
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_execution_lifecycle as f
from kadima import trade_policy


class PartialEntryProtection(unittest.TestCase):
    setUp = f.Lifecycle.setUp
    tearDown = f.Lifecycle.tearDown
    fill = f.Lifecycle.fill

    def begin(self, filled=4):
        self.pos.entry_price = 0
        self.pos.remaining_qty = 0
        self.pos.status = 'entry_pending'
        self.pos.structural_stop = 90
        f.storage.upsert_position(self.pos)
        f.ledger.submit(self.pos, 'BUY', 10, 100)
        self.fill(filled, 100 if filled else None)
        self.place.reset_mock()
        self.cancel.reset_mock()

    def test_stop_monitors_partial_and_latches_before_cancel(self):
        self.begin()
        with patch.object(f.executor, '_poll_ltp', return_value={'T':80}) as quote:
            self.ex.check_exits()
        quote.assert_called_once()
        self.cancel.assert_called_once_with('O1')
        self.place.assert_not_called()
        self.assertEqual(self.pos.exit_reason, 'structural_stop_breached')
        self.assertEqual(f.storage.load_open_positions()[0]['exit_reason'], self.pos.exit_reason)

    def test_manual_exit_accepts_partial_without_duplicate_sell(self):
        self.begin()
        for _ in range(2):
            result = self.ex.manual_exit(1)
            self.assertTrue(result['ok'])
            self.assertEqual(result['reason'], 'exit_queued_awaiting_entry_terminal')
        self.place.assert_not_called()
        self.assertEqual(len(f.ledger.pending(1)), 1)

    def test_late_fill_and_rebound_exit_final_quantity_once(self):
        self.begin()
        self.ex.manual_exit(1)
        self.fill(7, 101, 'cancelled')  # More fills arrived while cancel was in flight.
        with patch.object(f.executor, '_poll_ltp', return_value={'T':110}):
            self.ex.check_exits()
            self.ex.check_exits()
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.kwargs['transactiontype'], 'SELL')
        self.assertEqual(self.place.call_args.kwargs['quantity'], 7)

    def test_rejected_cancel_keeps_latch_and_retries_without_sell(self):
        self.begin()
        self.cancel.return_value = f.orders.OrderResult(False, 'O1', 'rejected')
        self.ex.manual_exit(1)
        self.fill(4, 100)
        self.assertEqual(self.pos.status, 'entry_pending')
        self.assertGreaterEqual(self.cancel.call_count, 2)
        self.place.assert_not_called()
        self.assertEqual(self.pos.exit_reason, 'manual_exit_dashboard')

    def test_restart_keeps_exit_request_and_confirmed_quantity(self):
        self.begin()
        self.ex.manual_exit(1)
        restored = f.executor.AutoExecutor()
        self.book.return_value = [f.orders.OrderStatus('O1','cancelled',6,100,{})]
        with patch.object(f.executor, 'fetch_broker_positions', return_value=[
                dict(exchange='NFO',symboltoken='T',producttype='INTRADAY',netqty='6')]):
            restored.reconcile_on_startup()
        self.assertFalse(restored._reconcile_blocked)
        with patch.object(f.executor, '_poll_ltp', return_value={'T':110}):
            restored.check_exits()
        self.assertEqual(self.place.call_args.kwargs['quantity'], 6)

    def test_zero_fill_cancel_never_sells(self):
        self.begin(0)
        self.assertTrue(self.ex.manual_exit(1)['ok'])
        self.fill(0, None, 'cancelled')
        self.ex.check_exits()
        self.place.assert_not_called()
        self.assertNotIn('1', self.ex.state.positions)

    def test_missing_book_keeps_pending_and_does_not_guess(self):
        self.begin()
        self.ex.manual_exit(1)
        self.book.return_value = None
        self.ex.refresh_orders()
        self.assertEqual(self.pos.remaining_qty, 4)
        self.assertEqual(self.pos.status, 'entry_pending')
        self.place.assert_not_called()


class ShieldThreshold(unittest.TestCase):
    def test_both_directions_65_boundary_and_other_gates(self):
        for direction, spot, snap in [
            ('CE',101,SimpleNamespace(vwap=100,ema_fast=102,ema_slow=101,rsi=60)),
            ('PE',99,SimpleNamespace(vwap=100,ema_fast=98,ema_slow=99,rsi=40))]:
            for kind in ('bull_shield','bear_shield'):
                for score in (65,70,80,100):
                    self.assertTrue(trade_policy.eligible(kind,'CONFIRMED',score,direction,snap,spot))
                for score in (64.99,0,float('nan'),float('inf'),101):
                    self.assertFalse(trade_policy.eligible(kind,'CONFIRMED',score,direction,snap,spot))
                self.assertFalse(trade_policy.eligible(kind,'ACTIVE',65,direction,snap,spot))
                self.assertFalse(trade_policy.eligible(kind,'CONFIRMED',65,direction,None,spot))
            self.assertFalse(trade_policy.eligible('gamma_blast','CONFIRMED',65,direction,snap,spot))
            self.assertTrue(trade_policy.eligible('gamma_blast','CONFIRMED',70,direction,snap,spot))

    def test_actual_engine_scores_both_directions_at_65(self):
        tree = ast.parse((f.ROOT/'kadima/engine.py').read_text())
        cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Engine')
        fn = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_score')
        constants = [n for n in tree.body if isinstance(n,ast.Assign) and
                     any(isinstance(t,ast.Name) and (t.id=='WEIGHTS' or t.id.startswith('TIER_')) for t in n.targets)]
        ns = dict(SHIELD_MIN_SCORE=trade_policy.SHIELD_MIN_SCORE,IndexState=object,
                  now_ist=lambda:f.NOW,audit=SimpleNamespace(log=lambda *a,**k:None))
        exec(compile(ast.Module(body=constants+[fn],type_ignores=[]),'engine-score','exec'),ns)
        for bullish,direction in [(True,'CE'),(False,'PE')]:
            snap=SimpleNamespace(above_vwap=bullish,ema_bull=bullish,rsi=60 if bullish else 40,
                 avg_vol=0,vol=0,failed_breakdown=False,failed_breakout=False)
            state=SimpleNamespace(name='TEST',snap=snap,chain=None,tier='NONE')
            ns['_score'](None,state)
            self.assertEqual((state.score,state.tier,state.direction),(65,'CONFIRMED',direction))


class ReleaseInstaller(unittest.TestCase):
    def installer(self):
        spec=importlib.util.spec_from_file_location('release_installer',f.ROOT/'executor_fix/install.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return mod

    def test_resulting_package_resolves_all_backend_imports(self):
        installer=self.installer()
        manifest=json.loads((f.ROOT/'executor_fix/manifest.json').read_text())
        self.assertNotIn('kadima/config.py',manifest)
        with tempfile.TemporaryDirectory() as folder:
            baseline=Path(folder)
            # Existing modules only: added modules are absent from the AWS baseline.
            for source in (f.ROOT/'kadima').glob('*.py'):
                path='kadima/'+source.name
                if path not in manifest or None not in manifest[path]:
                    dest=baseline/path;dest.parent.mkdir(parents=True,exist_ok=True)
                    dest.write_text(source.read_text())
            installer.validate_dependencies(f.ROOT,baseline,manifest)
            incomplete=dict(manifest);incomplete.pop('kadima/trade_policy.py')
            with self.assertRaisesRegex(SystemExit,'trade_policy.py'):
                installer.validate_dependencies(f.ROOT,baseline,incomplete)

    def test_source_hashes_preserve_unknown_local_versions_gate(self):
        installer=self.installer()
        manifest=json.loads((f.ROOT/'executor_fix/manifest.json').read_text())
        self.assertNotIn(installer.blob(b'local unreviewed edits'),manifest['kadima/engine.py'])
        required={'kadima/'+name+'.py' for name in ('celestial','celestial_scanner','celestial_adoption',
                  'celestial_universe','news_catalysts','scrip_master')}
        self.assertTrue(required.issubset(manifest))
        self.assertEqual(len(manifest),29)
