"""Offline tests for Move Capture live handoff and guarded dispatch."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import ast
import time
import unittest
from unittest.mock import patch

from kadima.config import IST
from kadima import move_capture_execution as live
from kadima import move_capture_handoff as handoff

BASE = Path(__file__).resolve().parents[1]


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def evaluate_and_enter(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        context = kwargs["fast_context"]()
        if not context:
            return {"ok": False, "reason": "revalidation_failed"}
        return {"ok": True, "reason": "test_dispatch"}


def candidate(now=None):
    now = now or datetime(2026, 9, 21, 10, 30, tzinfo=IST)
    return dict(
        underlying="AAA", exchange="NSE", token="1", asset_scope="STOCK",
        strategy="move_capture", direction="CE", score=82.0, price=102.0,
        observed_at=now.isoformat(), episode="move:AAA:CE:episode",
        spot_stop=101.0, impulse_anchor=100.0,
    )


class ConfirmationTests(unittest.TestCase):
    def test_two_option_samples_confirm_rising_liquid_premium(self):
        worker = live.MoveCaptureExecutionWorker(FakeExecutor(), SimpleNamespace())
        data = candidate()
        q1 = SimpleNamespace(token="11", symbol="AAAOPTCE", ltp=10.0,
                             bid=9.9, ask=10.1, volume=1000)
        result, reason = worker._confirm(data, q1)
        self.assertIsNone(result)
        self.assertEqual(reason, "move_capture_option_confirmation_warmup")
        worker.first_samples["AAA"]["at"] = time.monotonic() - 3
        q2 = SimpleNamespace(token="11", symbol="AAAOPTCE", ltp=10.2,
                             bid=10.0, ask=10.2, volume=1100)
        result, reason = worker._confirm(data, q2)
        self.assertIsNone(reason)
        self.assertGreaterEqual(result["premium_move_pct"], live.MIN_PREMIUM_MOVE_PCT)
        self.assertEqual(result["option_volume_delta"], 100)

    def test_falling_book_does_not_confirm(self):
        worker = live.MoveCaptureExecutionWorker(FakeExecutor(), SimpleNamespace())
        data = candidate()
        q1 = SimpleNamespace(token="11", symbol="AAAOPTCE", ltp=10.0,
                             bid=10.0, ask=10.1, volume=1000)
        worker._confirm(data, q1)
        worker.first_samples["AAA"]["at"] = time.monotonic() - 3
        q2 = SimpleNamespace(token="11", symbol="AAAOPTCE", ltp=10.2,
                             bid=9.8, ask=9.9, volume=1100)
        result, reason = worker._confirm(data, q2)
        self.assertIsNone(result)
        self.assertEqual(reason, "move_capture_option_book_not_confirmed")


class DispatchTests(unittest.TestCase):
    def test_live_disabled_never_polls_options_or_dispatches(self):
        ex = FakeExecutor()
        worker = live.MoveCaptureExecutionWorker(ex, SimpleNamespace())
        row = dict(name="AAA", updated_at=candidate()["observed_at"],
                   episode="move:AAA:CE:episode", direction="CE", data=candidate())
        with patch.object(live, "live_requested", return_value=False), \
             patch.object(worker, "_chain", side_effect=AssertionError("must not poll")):
            worker.process(row)
        self.assertEqual(ex.calls, [])
        self.assertEqual(worker.rows["AAA"]["stage"], "LIVE_DISABLED")

    def test_confirmed_candidate_reuses_guarded_fast_executor_contract(self):
        ex = FakeExecutor()
        worker = live.MoveCaptureExecutionWorker(ex, SimpleNamespace())
        data = candidate(datetime.now(IST))
        row = dict(name="AAA", updated_at=data["observed_at"],
                   episode=data["episode"], direction="CE", data=data)
        chain = SimpleNamespace(_entry_guard=None)
        quote = SimpleNamespace(token="11", symbol="AAAOPTCE", ltp=10.0,
                                bid=9.9, ask=10.1, volume=10000)
        current = dict(row)
        current["data"] = dict(data)
        with patch.object(live, "live_requested", return_value=True), \
             patch.object(live.handoff, "fresh", return_value=True), \
             patch.object(live.handoff, "signal_id", return_value=101), \
             patch.object(live.handoff, "current", return_value=current), \
             patch.object(live.handoff, "still_current", return_value=True), \
             patch.object(live.fast_momentum, "live_blocks", return_value=[]), \
             patch.object(live.audit, "log"), \
             patch.object(worker, "_chain", return_value=(chain, quote, 2.0)), \
             patch.object(worker, "_confirm",
                          return_value=({"premium_move_pct": 1.0,
                                         "option_volume_delta": 100,
                                         "option_confirm_seconds": 3.0}, None)):
            worker.process(row)

        self.assertEqual(len(ex.calls), 1)
        args, kwargs = ex.calls[0]
        self.assertEqual(args[0], "AAA")
        self.assertEqual(args[1], "CE")
        self.assertEqual(args[6], "fast_momentum")
        self.assertEqual(args[7], 101)
        ctx = kwargs["fast_context"]()
        self.assertEqual(ctx["source_strategy"], "move_capture")
        self.assertEqual(ctx["strategy"], "fast_momentum")
        self.assertTrue(ctx["eligible"])
        self.assertTrue(callable(chain._entry_guard))

    def test_service_process_has_no_direct_order_transport(self):
        source = (BASE / "kadima/move_capture_service.py").read_text()
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = " ".join(imports)
        for forbidden in ("auto_executor", "angel_orders", "execution_ledger"):
            self.assertNotIn(forbidden, joined)
        for forbidden in ("placeOrder", "cancelOrder", "modifyOrder"):
            self.assertNotIn(forbidden, source)

    def test_execution_worker_has_no_direct_broker_write_calls(self):
        source = (BASE / "kadima/move_capture_execution.py").read_text()
        for forbidden in ("placeOrder", "cancelOrder", "modifyOrder", "place_limit_order"):
            self.assertNotIn(forbidden, source)
        self.assertIn("evaluate_and_enter", source)


class HandoffTests(unittest.TestCase):
    def test_handoff_module_has_no_broker_imports(self):
        source = (BASE / "kadima/move_capture_handoff.py").read_text()
        for forbidden in ("SmartApi", "angel_orders", "auto_executor"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
