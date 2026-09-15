import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

path=Path(__file__).resolve().parents[1]/'executor_fix/install.py'
spec=importlib.util.spec_from_file_location('installer_under_test',path)
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallerDiagnostics(unittest.TestCase):
    def test_raw_child_output_is_never_displayed(self):
        exc=subprocess.CalledProcessError(1,['python'],output='SECRET_SENTINEL',stderr='SECRET_SENTINEL')
        self.assertNotIn('SECRET_SENTINEL',installer.failure_summary(exc,'broker_preflight'))

    def test_structured_failure_is_reported(self):
        d=dict(stage='broker_positions',reason='broker_positions_not_flat',error='RuntimeError')
        exc=subprocess.CalledProcessError(1,['python'],output='KADIMA_DIAGNOSTIC '+json.dumps(d))
        self.assertEqual(installer.failure_summary(exc,'broker_preflight'),
                         'broker_positions: broker_positions_not_flat (RuntimeError)')

    def test_unrecognized_diagnostic_fields_are_not_echoed(self):
        exc=subprocess.CalledProcessError(1,['python'],output='KADIMA_DIAGNOSTIC '+json.dumps(
            dict(stage='SECRET_SENTINEL',reason='exception',error='RuntimeError')))
        self.assertNotIn('SECRET_SENTINEL',installer.failure_summary(exc,'broker_preflight'))

    def test_timeout_identifies_operation(self):
        self.assertEqual(installer.failure_summary(subprocess.TimeoutExpired(['python'],60),
            'broker_preflight'),'broker_preflight: timed out')

    def test_login_exception_does_not_expose_message(self):
        package=types.ModuleType('kadima')
        package.config=types.SimpleNamespace()
        connection=Mock()
        connection.execute.return_value.fetchone.return_value=None
        package.storage=types.SimpleNamespace(conn=lambda:connection)
        session=types.ModuleType('kadima.session')
        session.SESSION=types.SimpleNamespace(ensure=Mock(side_effect=RuntimeError('SECRET_SENTINEL')))
        modules={'kadima':package,'kadima.session':session,
                 'SmartApi':types.ModuleType('SmartApi'),'pyotp':types.ModuleType('pyotp')}
        output=io.StringIO()
        with patch.dict(sys.modules,modules), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit):
                exec(compile(installer.PREFLIGHT,'preflight','exec'),{})
        self.assertIn('broker_login',output.getvalue())
        self.assertNotIn('SECRET_SENTINEL',output.getvalue())

if __name__=='__main__':unittest.main()
