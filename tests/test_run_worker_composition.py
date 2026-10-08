"""Guard the production run.py worker composition.

The AWS host currently carries Stock Fast + Gap-and-Go + MCX. Move Capture must
be additive: future edits cannot silently remove any existing worker.
"""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RunWorkerCompositionTests(unittest.TestCase):
    def test_run_keeps_all_live_workers(self):
        source = (ROOT / "run.py").read_text()
        tree = ast.parse(source)
        required_names = {
            "stock_fast_worker",
            "gap_worker",
            "mcx_worker",
            "move_capture_worker",
        }
        assigned = set()
        starts = set()
        stops = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                assigned.add(node.target.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    if node.func.attr == "start":
                        starts.add(node.func.value.id)
                    elif node.func.attr == "stop":
                        stops.add(node.func.value.id)

        self.assertTrue(required_names.issubset(assigned))
        self.assertTrue(required_names.issubset(starts))
        self.assertTrue(required_names.issubset(stops))

    def test_mcx_extended_session_gate_is_preserved(self):
        source = (ROOT / "run.py").read_text()
        self.assertIn("mcx_live.live_requested()", source)
        self.assertIn("commodity_watch.monitoring_window(now_ist())", source)

    def test_move_capture_does_not_replace_existing_worker_imports(self):
        source = (ROOT / "run.py").read_text()
        for name in ("stock_fast", "gap_live", "mcx_live", "commodity_watch",
                     "move_capture_execution"):
            self.assertIn(name, source)


if __name__ == "__main__":
    unittest.main()
