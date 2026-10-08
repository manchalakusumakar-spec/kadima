"""Offline tests for the broker-free Move Capture detector/service."""
from datetime import datetime, timedelta
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kadima.config import IST
from kadima.move_capture import MoveCaptureDetector, SpotIdentity
from kadima import move_capture_service as svc

BASE = Path(__file__).resolve().parents[1]


def feed_impulse(detector, name, *, direction=1, start=None, base_volume=0):
    start = start or datetime(2026, 9, 21, 9, 15, tzinfo=IST)
    identity = SpotIdentity(name, "NSE", "T-"+name)
    volume = base_volume
    promoted = False
    candidate = None
    # Six quiet minutes: tiny alternating movement builds a symbol-specific sigma.
    for sec in range(360):
        volume += 10
        price = 100 + (0.015 if sec % 2 else -0.015)
        candidate, event = detector.ingest(identity, start + timedelta(seconds=sec),
                                           price, volume, 99.5)
        promoted = promoted or event
    # One-minute price/volume impulse.
    for sec in range(61):
        volume += 55
        price = 100 + direction * 2.0 * (sec / 60)
        candidate, event = detector.ingest(identity, start + timedelta(seconds=360+sec),
                                           price, volume, 99.5)
        promoted = promoted or event
    return candidate, promoted


class DetectorTests(unittest.TestCase):
    def test_up_impulse_promotes_ce(self):
        detector = MoveCaptureDetector()
        row, promoted = feed_impulse(detector, "UP")
        self.assertTrue(promoted)
        self.assertEqual(row["direction"], "CE")
        self.assertGreaterEqual(row["score"], detector.trigger_score)
        self.assertTrue(row["episode"].startswith("move:UP:CE:"))
        self.assertLess(row["spot_stop"], row["price"])
        self.assertEqual(detector.snapshot(datetime.fromisoformat(row["observed_at"]))["hot"][0]["underlying"], "UP")

    def test_down_impulse_promotes_pe(self):
        detector = MoveCaptureDetector()
        row, promoted = feed_impulse(detector, "DOWN", direction=-1)
        self.assertTrue(promoted)
        self.assertEqual(row["direction"], "PE")
        self.assertGreaterEqual(row["score"], detector.trigger_score)
        self.assertGreater(row["spot_stop"], row["price"])

    def test_hot_list_is_bounded(self):
        detector = MoveCaptureDetector(max_hot=3)
        now = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
        for i in range(6):
            feed_impulse(detector, "S"+str(i), start=now, base_volume=i*10000)
        snap = detector.snapshot(now + timedelta(seconds=420))
        self.assertLessEqual(len(snap["hot"]), 3)
        self.assertEqual(snap["hot"], sorted(snap["hot"],
                                             key=lambda x: (-x["score"], x["underlying"])))

    def test_invalid_ticks_do_not_create_state(self):
        detector = MoveCaptureDetector()
        identity = SpotIdentity("BAD", "NSE", "1")
        naive = datetime(2026, 9, 21, 10)
        detector.ingest(identity, naive, 100, 10)
        detector.ingest(identity, datetime(2026, 9, 21, 10, tzinfo=IST), -1, 10)
        self.assertEqual(detector.trackers, {})

    def test_detector_itself_never_claims_execution(self):
        detector = MoveCaptureDetector()
        feed_impulse(detector, "UP")
        snap = detector.snapshot(datetime(2026, 9, 21, 9, 22, tzinfo=IST))
        self.assertEqual(snap["mode"], "DETECTOR_ONLY")
        self.assertFalse(snap["execution_enabled"])
        self.assertIn("not a win probability", snap["score_note"])


class ServiceTests(unittest.TestCase):
    def test_universe_is_spot_only_and_deduplicated(self):
        rows = [
            dict(name="AAA", spot_exchange="NSE", spot_token="1", opt_exchange="NFO"),
            dict(name="AAA", spot_exchange="NSE", spot_token="1", opt_exchange="BFO"),
            dict(name="BBB", spot_exchange="NSE", spot_token="2", opt_exchange="NFO"),
            dict(name="BFOONLY", spot_exchange="NSE", spot_token="3", opt_exchange="BFO"),
        ]
        fake_indices = {
            "NIFTY": type("I", (), dict(exchange="NSE", token="999"))(),
            "BANKNIFTY": type("I", (), dict(exchange="NSE", token="998"))(),
        }
        with patch.object(svc, "stock_universe", return_value=rows), \
             patch.object(svc, "GAMMA_INDICES", fake_indices):
            result = svc.universe()
        stocks = [v for v in result.values() if v.kind == "STOCK"]
        self.assertEqual({x.name for x in stocks}, {"AAA", "BBB"})
        self.assertEqual(len(stocks), 2)
        self.assertTrue(all(x.exchange == "NSE" for x in stocks))

    def test_price_and_volume_parsing_fail_closed(self):
        self.assertIsNone(svc._price(None))
        self.assertIsNone(svc._price(-1))
        self.assertEqual(svc._price(12345), 123.45)
        self.assertIsNone(svc._volume(float("nan")))
        self.assertEqual(svc._volume(123), 123.0)

    def test_snapshot_write_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"snapshot.json"
            svc.atomic_json(path, {"ok": True, "rows": [1,2,3]})
            self.assertEqual(json.loads(path.read_text()), {"ok": True, "rows": [1,2,3]})
            self.assertEqual(list(path.parent.glob(".move-capture-*")), [])

    def test_service_source_has_no_order_or_executor_imports(self):
        tree = ast.parse((BASE/"kadima/move_capture_service.py").read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append((node.module or ""))
        joined = " ".join(imports)
        for forbidden in ("auto_executor", "angel_orders", "execution_ledger"):
            self.assertNotIn(forbidden, joined)

    def test_service_has_no_order_method_names(self):
        source = (BASE/"kadima/move_capture_service.py").read_text()
        for forbidden in ("placeOrder", "place_limit_order", "cancelOrder", "modifyOrder"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
