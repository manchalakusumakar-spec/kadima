"""Deployment boundaries: unknown versions stop before mutation; inventory is read-only."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


class DeploymentTools(unittest.TestCase):
    def test_backend_restart_delay_then_ready(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',side_effect=[
                urllib.error.URLError('connection refused'),io.BytesIO(b'{"dashboard_limit":20}')]) as fetch, \
                patch.object(upgrade.time,'sleep'):
            self.assertEqual(upgrade.wait_for_backend('request')['dashboard_limit'],20)
            self.assertEqual(fetch.call_count,2)

    def test_backend_auth_failure_is_not_retried(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',side_effect=
                urllib.error.HTTPError('local',401,'unauthorized',{},None)) as fetch:
            with self.assertRaises(urllib.error.HTTPError):upgrade.wait_for_backend('request')
            self.assertEqual(fetch.call_count,1)

    def test_backend_restart_wait_is_bounded(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',side_effect=urllib.error.URLError('refused')), \
                patch.object(upgrade.time,'monotonic',side_effect=[0,31]),patch.object(upgrade.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'ready'):upgrade.wait_for_backend('request')
            sleep.assert_not_called()

    def test_wrong_backend_schema_refused(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with patch.object(upgrade.urllib.request,'urlopen',return_value=io.BytesIO(b'{"ok":true}')):
            with self.assertRaisesRegex(ValueError,'backend required'):upgrade.wait_for_backend('request')

    def test_unknown_dashboard_source_refused_without_overwrite(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'gateway.py';target.write_text('unreviewed live edit')
            with self.assertRaisesRegex(ValueError,'Unreviewed'):
                upgrade.check_versions({'gateway.py':target},{'gateway.py':[]})
            self.assertEqual(target.read_text(),'unreviewed live edit')

    def test_reviewed_dashboard_hash_is_accepted(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'gateway.py';target.write_text('reviewed old source')
            upgrade.check_versions({'gateway.py':target},{'gateway.py':[upgrade.blob(target)]})

    def test_dashboard_symlink_refused(self):
        upgrade=module('dashboard_upgrade','dashboard_live/upgrade.py')
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'actual';source.write_text('old source')
            target=Path(tmp)/'gateway.py';target.symlink_to(source)
            with self.assertRaisesRegex(ValueError,'regular'):
                upgrade.check_versions({'gateway.py':target},{'gateway.py':[upgrade.blob(source)]})

    def test_inventory_does_not_create_missing_live_checkout_or_echo_raw_output(self):
        report=module('deployment_inventory','tools/deployment_report.py')
        with tempfile.TemporaryDirectory() as tmp, patch.object(report,'run',return_value='DO_NOT_PRINT_SECRET\nActiveState=inactive\nMainPID=0\n'):
            target=Path(tmp)/'nonexistent'
            output=report.report(target)
            self.assertFalse(target.exists())
            self.assertTrue(output['read_only'])
            self.assertNotIn('DO_NOT_PRINT_SECRET',json.dumps(output))
            self.assertTrue(all(v['installed'] is None for v in output['source'].values()))


if __name__=='__main__':unittest.main()
