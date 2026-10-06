"""Full-source CAS regressions. Missing runtime source is a failure, never a skip.

Run: python3 -I -B tools/test_cas_semantics_integration.py
No timing_recorder/celestial/source substitutes. Broker transport is mocked.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

DATA = tempfile.TemporaryDirectory(prefix="cas-semantics-integration-")
os.environ["KADIMA_DATA_DIR"] = DATA.name
os.environ["KADIMA_CAS_EXECUTION"] = "0"
os.environ["KADIMA_CAS_LIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Import failure must remain visible. Never fabricate missing deployed source.
from kadima import auto_executor as ae, cas_engine as cas, cas_orders, engine
from kadima import angel_orders as orders, cas_config as cfg, cas_executor as legacy
from kadima import celestial, execution_ledger as ledger, killswitch, market_session_gate, storage
from kadima.config import IST


class Integration(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, 14, 30, tzinfo=IST)
        self.ex = ae.AutoExecutor()
        self.ex._positions_verified = True
        self.ex._reconcile_blocked = False
        self.control = dict(auto_trade_enabled=True, kill_latched=False)
        self.api = NS(placeOrder=Mock(return_value="FAKE-ORDER"))
        for p in (
            patch.dict(os.environ, {"KADIMA_CAS_LIVE": "1"}),
            patch.object(cfg, "CAS_EXECUTION_ENABLED", False),
            patch.object(celestial, "enabled", return_value=True),
            patch.object(celestial, "read_config", return_value=self.control),
            patch.object(cas_orders, "now_ist", return_value=self.now),
            patch.object(orders, "SESSION", NS(ensure=Mock(), api=self.api)),
            patch.object(orders, "ORDER_PLACEMENT_ENABLED", True),
            patch.object(orders, "ORDER_WRITE", NS(allow=lambda: True)),
            patch.object(ae, "ORDER_PLACEMENT_ENABLED", True),
            patch.object(ae, "in_market_hours", return_value=True),
            patch.object(ae, "past_signal_cutoff", return_value=False),
            patch.object(ae.exit_manager, "realized_pnl_today", return_value=0),
            patch.object(market_session_gate, "entry_block", return_value=None),
            patch.object(orders.audit, "log"),
            patch.object(ledger.alerts, "enqueue"),
            patch.object(killswitch, "KILL_SWITCH_FILE", str(Path(DATA.name) / "KILL")),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            p.start()
            self.addCleanup(p.stop)
        Path(killswitch.KILL_SWITCH_FILE).unlink(missing_ok=True)
        cas.connection()
        ledger.connection()
        with storage.conn() as c:
            for table in ("cas_runs", "cas_locks", "execution_fills", "execution_orders", "positions", "signals"):
                c.execute("DELETE FROM " + table)
        self.row = dict(episode="test-episode", ts=self.now.isoformat(), underlying="NIFTY",
                        direction="CE", token="1", symbol="TESTCE", exchange="NFO", strike=23000,
                        expiry=self.now.date().isoformat(), entry=7.45, ask=7.45, ltp=7.45,
                        quote_at=self.now.isoformat(), eligible=True, stage="CAS ENTRY", blocked_reasons=[])

    def position(self, sid=1, kind="cas"):
        return ae.Position(sid, kind, "NIFTY", "CE", "TESTCE", "1", "NFO", 7.45,
                           self.now, 1, 1, 1, status="open")

    def test_reservation_and_restore_retain_new_cas_kind_and_product(self):
        sid = cas.reserve(self.row, 10000, 2)
        saved = storage.conn().execute("SELECT kind FROM signals WHERE id=?", (sid,)).fetchone()
        self.assertEqual(saved["kind"], "cas")
        pos = self.position(sid, saved["kind"])
        storage.upsert_position(pos)
        restored = NS(**storage.load_open_positions()[0])
        self.assertEqual(restored.kind, "cas")
        self.assertEqual(orders.product_for(restored), "CARRYFORWARD")

    def test_persisted_new_lane_entry_reaches_transport_as_carryforward(self):
        sid = cas.reserve(self.row, 10000, 2)
        pos = self.position(sid)
        pos.remaining_qty = 0
        result = ledger.submit(pos, "BUY", 1, 7.45)
        self.assertTrue(result["pending"])
        payload = self.api.placeOrder.call_args.args[0]
        self.assertEqual((payload["producttype"], payload["price"]), ("CARRYFORWARD", "7.45"))
        self.assertEqual(pos.remaining_qty, 0)  # acknowledgement is not a fill

    def test_new_lane_dispatch_does_not_coarsen_valid_ask(self):
        row = self.row
        chain = NS(index=NS(), atm=23000, scrips=NS(
            strikes_around=lambda *args: [dict(token="1", symbol="TESTCE")],
            nearest_expiry=lambda *args: self.now.strftime('%d%b%Y').upper()))
        lane = NS(scan=lambda *args, **kwargs: [row])
        quote = NS(ask=7.45, bid=7.4, ltp=7.45, option_vwap=7.0, bid_qty=100, ask_qty=100,
                   quote_at=row['quote_at'], symbol="TESTCE", token="1")
        with patch.object(cas_orders, "account_block", return_value=None), \
                patch.object(cas_orders, "fetch_quote", return_value=quote), \
                patch.object(ae, "get_lot_size", return_value=1), \
                patch.object(ae.capital_guard, "get_capital_for_signal", return_value=10000), \
                patch.object(ae.capital_guard, "enforce", return_value=(1, "ok")), \
                patch.object(ledger, "submit", return_value={"ok": True}) as submit:
            result = cas_orders.dispatch(self.ex, NS(name="NIFTY", chain=chain), lane, row)
            self.assertTrue(result["ok"], result)
            self.assertEqual(submit.call_args.args[3], 7.45)
            self.assertEqual(orders.product_for(submit.call_args.args[0]), "CARRYFORWARD")

    def test_new_lane_default_off_and_global_kill_still_block(self):
        with patch.dict(os.environ, {"KADIMA_CAS_LIVE": "0"}):
            self.assertIn("cas_observation_only", cas.live_blocks(self.now))
        self.control["kill_latched"] = True
        self.assertIn("kill_latched", cas.live_blocks(self.now))
        self.control["kill_latched"] = False
        self.control["auto_trade_enabled"] = False
        self.assertIn("auto_paused", cas.live_blocks(self.now))
        self.control["auto_trade_enabled"] = True
        Path(killswitch.KILL_SWITCH_FILE).touch()
        self.assertEqual(cas_orders.account_block(self.ex, self.now), "killswitch_engaged")

    def test_engine_stale_eligible_row_cannot_compete_with_legacy_lane(self):
        lane = NS(scan=lambda *args, **kwargs: [self.row])
        host = NS(cas_lane=lane, _cas_reported={})
        with patch.object(cfg, "CAS_EXECUTION_ENABLED", True), \
                patch.object(ae, "EXECUTOR", self.ex), \
                patch.object(legacy.threading, "Thread") as thread:
            self.assertIn("cas_entry_lane_conflict", cas.live_blocks(self.now))
            # Exercise Engine -> evaluate_cas -> account gate even with a stale
            # eligible row, rather than relying only on scanner eligibility.
            engine.Engine._scan_cas(host, NS(name="NIFTY"))
            self.assertEqual(self.row["dispatch"]["reason"], "cas_entry_lane_conflict")
            legacy.submit(NS(symbol="TESTCE", index="NIFTY"), None)
            thread.assert_not_called()
            self.api.placeOrder.assert_not_called()
            self.assertEqual(self.ex.state.positions, {})

    def test_cas_exit_price_and_product_survive_flags_off_and_kill(self):
        Path(killswitch.KILL_SWITCH_FILE).touch()
        with patch.dict(os.environ, {"KADIMA_CAS_LIVE": "0"}), \
                patch.object(ledger, "submit", return_value={"ok": True, "pending": True}) as submit:
            for kind in ("cas", "cas_blast"):
                pos = self.position(kind=kind)
                self.ex._close_position(pos, .05, "test_exit")
                self.assertEqual(submit.call_args.args[1:], ("SELL", 1, .05))
                self.assertEqual(orders.product_for(pos), "CARRYFORWARD")
            pos = self.position(kind="gamma_blast")
            self.ex._close_position(pos, 7.9, "test_exit")
            self.assertEqual(submit.call_args.args[3], round(7.9 * .975, 1))

    def test_legacy_entry_rechecks_lane_after_blocking_quote_read(self):
        now = self.now.replace(hour=15, minute=20)
        candidate = NS(symbol="TESTCE", token="1", exchange="NFO", index="NIFTY",
                       ltp=7.5, detected_at=now)
        def quote(*args):
            os.environ["KADIMA_CAS_LIVE"] = "1"
            return dict(bid=7.45, ask=7.55, ltp=7.5)
        with patch.dict(os.environ, {"KADIMA_CAS_LIVE": "0"}), \
                patch.object(cfg, "CAS_EXECUTION_ENABLED", True), \
                patch.object(legacy, "_executor", return_value=self.ex), \
                patch.object(legacy, "now_ist", return_value=now), \
                patch.object(legacy, "get_lot_size", return_value=1), \
                patch.object(legacy, "cas_trades_today", return_value=0), \
                patch.object(legacy.capital_guard, "broker_available_funds", return_value=10000), \
                patch.object(legacy, "fetch_quote", side_effect=quote):
            self.assertEqual(legacy.enter(candidate)["reason"], "cas_entry_lane_conflict")
            self.assertEqual(self.ex.state.positions, {})
            self.assertEqual(storage.conn().execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
            self.api.placeOrder.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
