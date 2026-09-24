"""Focused deployment and activation checks, all offline."""
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentTests(unittest.TestCase):
    def test_focused_plan_preserves_composed_executor_and_refuses_unknown_dependency(self):
        installer = load('recovery_plan_test', 'momentum_recovery/install.py')
        base = installer.base
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve()
            shutil.copytree(ROOT/'kadima', target/'kadima')
            shutil.copy(ROOT/'run.py', target/'run.py')
            executor = target/'kadima/auto_executor.py'
            shutil.copy(ROOT/'mcx_live_update/auto_executor.py', executor)
            for name, previous in base.FILES.items():
                if previous is None:
                    (target/name).unlink()
            original = executor.read_bytes()
            changes, pins = base.backend_plan(ROOT, target, (os.getuid(), os.getgid()))
            expected = {name for name, previous in base.FILES.items() if previous is None}
            self.assertEqual({str(c.path.relative_to(target)) for c in changes}, expected)
            self.assertNotIn(executor, [c.path for c in changes])
            self.assertEqual(executor.read_bytes(), original)
            self.assertIn(executor, pins)
            executor.write_bytes(original+b'\n# unknown overlay\n')
            with self.assertRaisesRegex(base.Blocked, 'unreviewed_dependency:kadima/auto_executor.py'):
                base.backend_plan(ROOT, target, (os.getuid(), os.getgid()))

    def test_move_flag_preserves_existing_mcx_capital(self):
        activation = load('recovery_activation_test', 'tools/activate_execution.py')
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(activation, 'ENVFILE', Path(temporary)/'env'), \
             patch.object(activation, 'SYSTEMD', Path(temporary)/'systemd'):
            activation.ENVFILE.write_text('KADIMA_CELESTIAL_ENABLED=1\nKADIMA_MCX_LIVE=1\nKADIMA_MCX_CAPITAL_INR=15000\n')
            files = activation.planned_files(False, False, move=True)
            lines = files[activation.ENVFILE].splitlines()
            for line in ('KADIMA_MCX_CAPITAL_INR=15000', 'KADIMA_MCX_LIVE=1',
                         'KADIMA_MOVE_CAPTURE_LIVE=1', 'KADIMA_FAST_MOMENTUM_LIVE=1'):
                self.assertIn(line, lines)

    def test_arm_requires_fresh_feed_and_uses_revision_checked_control(self):
        activation = load('recovery_arm_test', 'tools/activate_execution.py')
        reporter = Mock()
        reporter.local_json.return_value = {'controls': {'live_enabled': False, 'revision': 3}}
        with patch.object(activation.urllib.request, 'build_opener') as build:
            with self.assertRaisesRegex(activation.Blocked, 'not_ready_auto_remains_paused'):
                activation.arm_stock_intent('TOKEN', reporter)
            build.assert_not_called()
            state = reporter.local_json.return_value
            for key in ('move_capture_feed', 'move_capture_worker'):
                state[key] = dict(ok=True, ts=datetime.now(timezone.utc).isoformat())
            build.return_value.open.return_value = io.BytesIO(b'{"live_enabled":true}')
            activation.arm_stock_intent('TOKEN', reporter)
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(req.data), {'patch': {'live_enabled': True}, 'expected_revision': 3})


if __name__ == '__main__':
    unittest.main()
