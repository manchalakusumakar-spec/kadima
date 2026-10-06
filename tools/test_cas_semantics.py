"""Offline semantic regressions; real source, fake broker boundary only.

Run: python3 -I -B tools/test_cas_semantics.py
Does not substitute timing_recorder or any other missing runtime module.
The separate integration suite must still pass before this is releasable.
"""
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

DATA = tempfile.TemporaryDirectory(prefix="cas-semantics-")
os.environ["KADIMA_DATA_DIR"] = DATA.name
os.environ["KADIMA_CAS_EXECUTION"] = "0"
os.environ["KADIMA_CAS_LIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kadima import angel_orders as orders, cas_config as cfg, cas_executor as legacy
from kadima import execution_ledger as ledger, killswitch
from kadima.config import IST


class Semantics(unittest.TestCase):
    def setUp(self):
        self.api = NS(placeOrder=Mock(return_value="FAKE-ORDER"),
                      modifyOrder=Mock(return_value={"status": True}))
        self.session = NS(ensure=Mock(), api=self.api)
        for p in (
            patch.dict(os.environ, {"KADIMA_CAS_LIVE": "0"}),
            patch.object(cfg, "CAS_EXECUTION_ENABLED", False),
            patch.object(orders, "SESSION", self.session),
            patch.object(orders, "ORDER_PLACEMENT_ENABLED", True),
            patch.object(orders, "ORDER_WRITE", NS(allow=lambda: True)),
            patch.object(orders.audit, "log"),
            patch.object(ledger.alerts, "enqueue"),
            patch.object(killswitch, "KILL_SWITCH_FILE", str(Path(DATA.name) / "KILL")),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            p.start()
            self.addCleanup(p.stop)
        Path(killswitch.KILL_SWITCH_FILE).unlink(missing_ok=True)
        ledger._REPRICE.clear()

    def place(self, price, product="CARRYFORWARD", side="BUY"):
        return orders.place_limit_order(tradingsymbol="TESTCE", symboltoken="1",
            exchange="NFO", transactiontype=side, quantity=1, price=price,
            producttype=product)

    def test_product_identity_keeps_both_kinds_and_other_strategies(self):
        self.assertEqual(orders.CAS_KIND, "cas_blast")  # legacy exit policy identity
        for kind in ("cas", "cas_blast"):
            self.assertEqual(orders.product_for(NS(kind=kind)), "CARRYFORWARD")
        for kind in ("gamma_blast", "bull_shield", "fast_momentum", "mcx_momentum", None):
            self.assertEqual(orders.product_for(NS(kind=kind)), "INTRADAY")

    def test_carryforward_place_preserves_valid_half_tenth_prices(self):
        for kind in ("cas", "cas_blast"):
            for side in ("BUY", "SELL"):
                for price in (.05, .15, 7.45, 12.35, 80.05):
                    with self.subTest(kind=kind, side=side, price=price):
                        self.assertTrue(self.place(price, orders.product_for(NS(kind=kind)), side).ok)
                        payload = self.api.placeOrder.call_args.args[0]
                        self.assertEqual(Decimal(payload["price"]), Decimal(str(price)))
                        self.assertEqual((payload["ordertype"], payload["duration"]), ("LIMIT", "DAY"))
                        self.assertEqual(payload["producttype"], "CARRYFORWARD")

    def test_carryforward_modify_preserves_valid_half_tenth_prices(self):
        for price in (.05, .15, 7.45, 12.35, 80.05):
            with self.subTest(price=price):
                result = orders.modify_order("FAKE", price=price,
                    contract={"producttype": "CARRYFORWARD", "symboltoken": "1"})
                self.assertTrue(result.ok)
                payload = self.api.modifyOrder.call_args.args[0]
                self.assertEqual(Decimal(payload["price"]), Decimal(str(price)))
                self.assertEqual(payload["producttype"], "CARRYFORWARD")

    def test_intraday_and_unspecified_modify_rounding_unchanged(self):
        self.place(12.35, "INTRADAY")
        self.assertEqual(self.api.placeOrder.call_args.args[0]["price"], str(round(12.35, 1)))
        for contract in (None, {"producttype": "INTRADAY"}):
            orders.modify_order("FAKE", price=12.35, contract=contract)
            self.assertEqual(self.api.modifyOrder.call_args.args[0]["price"], str(round(12.35, 1)))

    def test_invalid_prices_never_reach_broker(self):
        for price in (0, -1, float("inf"), float("-inf"), float("nan")):
            self.assertFalse(self.place(price).ok)
            self.assertFalse(orders.modify_order("FAKE", price=price,
                contract={"producttype": "CARRYFORWARD"}).ok)
        self.session.ensure.assert_not_called()
        self.api.placeOrder.assert_not_called()
        self.api.modifyOrder.assert_not_called()

    def test_dry_run_preserves_price_without_session(self):
        with patch.object(orders, "ORDER_PLACEMENT_ENABLED", False):
            self.assertTrue(self.place(.05).dry_run)
            event = next(c.kwargs for c in orders.audit.log.call_args_list
                         if c.args[0] == "order_dry_run")
            self.assertEqual(event["price"], "0.05")
            self.assertTrue(orders.modify_order("FAKE", price=.05,
                contract={"producttype": "CARRYFORWARD"}).dry_run)
        self.session.ensure.assert_not_called()

    def test_caps_and_buy_prices_use_directional_tick_grid(self):
        for value in (.05, 7.45, 9.749, 9.75, 9.751):
            down, up = cfg.tick_price(value), cfg.tick_price(value, up=True)
            self.assertLessEqual(down, value)
            self.assertGreaterEqual(up, value)
            self.assertEqual(Decimal(str(down)) % Decimal(".05"), 0)
            self.assertEqual(Decimal(str(up)) % Decimal(".05"), 0)
        self.assertEqual(cfg.tick_price(7.45), 7.45)
        self.assertEqual(cfg.tick_price(7.45, up=True), 7.45)

    def test_legacy_ladder_preserves_bid_and_never_exceeds_cap(self):
        ladder = legacy.price_ladder(dict(bid=7.45, ltp=7.5, ask=7.55), 7.59)
        self.assertEqual(ladder[0], 7.45)
        self.assertLessEqual(max(ladder), 7.55)
        self.assertTrue(all(Decimal(str(p)) % Decimal(".05") == 0 for p in ladder))

    def test_lane_activation_truth_table(self):
        cases = [(False, "0", "cas_observation_only", "cas_execution_disabled"),
                 (True, "0", "cas_observation_only", None),
                 (False, "1", None, "cas_execution_disabled"),
                 (True, "1", "cas_entry_lane_conflict", "cas_entry_lane_conflict")]
        for old, new, new_block, old_block in cases:
            with self.subTest(legacy=old, live=new), patch.object(cfg, "CAS_EXECUTION_ENABLED", old), \
                    patch.dict(os.environ, {"KADIMA_CAS_LIVE": new}):
                self.assertEqual(cfg.entry_lane_block("cas"), new_block)
                self.assertEqual(cfg.entry_lane_block("cas_blast"), old_block)
        self.assertIsNotNone(cfg.entry_lane_block("unknown"))

    def test_fresh_config_with_unset_flags_is_default_off(self):
        with patch.dict(os.environ):
            os.environ.pop("KADIMA_CAS_EXECUTION", None)
            os.environ.pop("KADIMA_CAS_LIVE", None)
            fresh = runpy.run_path(str(Path(cfg.__file__)))
            self.assertFalse(fresh["CAS_EXECUTION_ENABLED"])
            self.assertEqual(fresh["entry_lane_block"]("cas"), "cas_observation_only")
            self.assertEqual(fresh["entry_lane_block"]("cas_blast"), "cas_execution_disabled")

    def test_legacy_submit_and_direct_gate_cannot_bypass_activation(self):
        candidate = NS(symbol="TEST", index="NIFTY")
        for old, new, reason in ((False, "0", "cas_execution_disabled"),
                                 (True, "1", "cas_entry_lane_conflict")):
            with patch.object(cfg, "CAS_EXECUTION_ENABLED", old), \
                    patch.dict(os.environ, {"KADIMA_CAS_LIVE": new}), \
                    patch.object(legacy.threading, "Thread") as thread:
                self.assertEqual(legacy.gate(candidate, NS(), datetime.now(IST)), reason)
                legacy.submit(candidate, None)
                thread.assert_not_called()

    def test_file_kill_blocks_legacy_even_when_selected(self):
        Path(killswitch.KILL_SWITCH_FILE).touch()
        with patch.object(cfg, "CAS_EXECUTION_ENABLED", True), \
                patch.object(legacy.threading, "Thread") as thread:
            candidate = NS(symbol="TEST", index="NIFTY")
            self.assertEqual(legacy.gate(candidate, NS(), datetime.now(IST)), "killswitch_engaged")
            legacy.submit(candidate, None)
            thread.assert_not_called()

    def test_selected_legacy_still_requires_session_permission(self):
        now = datetime(2026, 9, 29, 15, 20, tzinfo=IST)
        with patch.object(cfg, "CAS_EXECUTION_ENABLED", True), \
                patch.object(legacy, "cas_trades_today", return_value=0), \
                patch.object(legacy.exit_manager, "realized_pnl_today", return_value=0), \
                patch.object(ledger, "pending", return_value=[]), \
                patch.object(legacy.market_session_gate, "entry_block", return_value="permit_denied"):
            candidate = NS(detected_at=now, exchange="NFO", token="1", index="NIFTY")
            executor = NS(_reconcile_blocked=False, state=NS(positions={}))
            self.assertEqual(legacy.gate(candidate, executor, now), "permit_denied")

    def test_legacy_repricing_rechecks_conflict_after_quote_read(self):
        def quote(*args):
            os.environ["KADIMA_CAS_LIVE"] = "1"
            return dict(ltp=7.45, ask=7.5)
        with patch.object(cfg, "CAS_EXECUTION_ENABLED", True), \
                patch.object(legacy, "CAS_ENTRY_STEP_SEC", 0), \
                patch.object(legacy, "fetch_quote", side_effect=quote), \
                patch.object(orders, "get_order_status", return_value=NS(status="open", filled_qty=0)), \
                patch.object(orders, "cancel_order", return_value=orders.OrderResult(True, "FAKE", "ok")) as cancel:
            legacy._step_entry(NS(symbol="TEST", token="1", exchange="NFO", signal_id=1),
                               "FAKE", 1, 7.4, 8.0)
            self.api.modifyOrder.assert_not_called()
            cancel.assert_called_once_with("FAKE")

    def test_legacy_repricing_stops_on_kill_before_quote(self):
        Path(killswitch.KILL_SWITCH_FILE).touch()
        with patch.object(cfg, "CAS_EXECUTION_ENABLED", True), \
                patch.object(legacy, "CAS_ENTRY_STEP_SEC", 0), \
                patch.object(legacy, "fetch_quote") as quote, \
                patch.object(orders, "get_order_status", return_value=NS(status="open", filled_qty=0)), \
                patch.object(orders, "cancel_order", return_value=orders.OrderResult(True, "FAKE", "ok")):
            legacy._step_entry(NS(symbol="TEST", token="1", exchange="NFO", signal_id=1),
                               "FAKE", 1, 7.4, 8.0)
            quote.assert_not_called()
            self.api.modifyOrder.assert_not_called()

    def test_exit_reprice_keeps_tick_product_and_works_with_entries_off_and_kill(self):
        Path(killswitch.KILL_SWITCH_FILE).touch()
        for kind in ("cas", "cas_blast"):
            for bid, expected in ((.05, .05), (7.9, 7.7)):
                with self.subTest(kind=kind, bid=bid):
                    ledger._REPRICE.clear()
                    pos = NS(kind=kind, symbol="TEST", token="1", exchange="NFO", signal_id=1, remaining_qty=1)
                    ledger._reprice_exit(pos, {"intent": "TEST", "quantity": 1},
                        NS(order_id="FAKE", raw={"price": "8.0"}), 20, lambda *args: bid)
                    payload = self.api.modifyOrder.call_args.args[0]
                    self.assertEqual(float(payload["price"]), expected)
                    self.assertEqual(payload["producttype"], "CARRYFORWARD")

    def test_reconciliation_accepts_cas_product_and_rejects_intraday_fills(self):
        now = datetime.now(IST)
        for kind in ("cas", "cas_blast"):
            for side in ("BUY", "SELL"):
                for product in ("CARRYFORWARD", "INTRADAY"):
                    with self.subTest(kind=kind, side=side, product=product):
                        pos = NS(kind=kind, signal_id=1, token="1", exchange="NFO", status="open")
                        row = dict(signal_id=1, order_id="FAKE", intent="I", side=side, created_at=now.isoformat())
                        status = NS(order_id="FAKE", raw=dict(symboltoken="1", exchange="NFO",
                                    transactiontype=side, producttype=product))
                        with patch.object(ledger, "pending", return_value=[row]), \
                                patch.object(orders, "fetch_order_book", return_value=[status]), \
                                patch.object(ledger, "apply_fill") as fill:
                            self.assertEqual(ledger.refresh({"1": pos}), product == "CARRYFORWARD")
                            self.assertEqual(fill.call_count, int(product == "CARRYFORWARD"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
