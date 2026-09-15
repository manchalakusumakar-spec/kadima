import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

path=Path(__file__).resolve().parents[1]/'executor_fix/install.py'
spec=importlib.util.spec_from_file_location('installer_under_test',path)
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallerDiagnostics(unittest.TestCase):
    def test_engine_checkout_and_entrypoint_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=root/'repo';repo.mkdir();process=root/'proc';process.mkdir()
            (process/'cwd').symlink_to(repo,target_is_directory=True)
            command=process/'cmdline'
            command.write_bytes(b'/usr/bin/python3\0-u\0run.py\0')
            self.assertEqual(installer.engine_interpreter(process,repo),'/usr/bin/python3')
            command.write_bytes(b'/usr/bin/python3\0other.py\0')
            with self.assertRaisesRegex(SystemExit,'entrypoint'):installer.engine_interpreter(process,repo)
            command.write_bytes(b'/usr/bin/python3\0run.py\0')
            (process/'cwd').unlink();(process/'cwd').symlink_to(root,target_is_directory=True)
            with self.assertRaisesRegex(SystemExit,'different checkout'):installer.engine_interpreter(process,repo)

    def test_flat_preflight_requires_explicit_valid_net_quantity(self):
        for row in ({},{'netqty':None},{'netqty':''},{'netqty':False},{'netqty':'NaN'},{'netqty':'1'},{'netqty':'0'}):
            with self.subTest(row=row):
                package=types.ModuleType('kadima');package.config=types.SimpleNamespace()
                connection=Mock();connection.execute.return_value.fetchone.return_value=None
                package.storage=types.SimpleNamespace(conn=lambda:connection)
                session=types.ModuleType('kadima.session')
                api=Mock()
                api.position.return_value={'status':True,'data':[row]}
                api.orderBook.return_value={'status':True,'data':[]}
                session.SESSION=types.SimpleNamespace(ensure=Mock(),api=api)
                modules={'kadima':package,'kadima.session':session,
                         'SmartApi':types.ModuleType('SmartApi'),'pyotp':types.ModuleType('pyotp')}
                output=io.StringIO()
                with patch.dict(sys.modules,modules),contextlib.redirect_stdout(output):
                    if row=={'netqty':'0'}:
                        exec(compile(installer.PREFLIGHT,'preflight','exec'),{})
                        self.assertIn('KADIMA_PREFLIGHT_OK',output.getvalue())
                    else:
                        with self.assertRaises(SystemExit):exec(compile(installer.PREFLIGHT,'preflight','exec'),{})
                        self.assertNotIn('KADIMA_PREFLIGHT_OK',output.getvalue())
                        api.orderBook.assert_not_called()

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
