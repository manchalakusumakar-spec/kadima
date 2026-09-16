"""24/7 deployment tests: service/broker calls are mocked, files are temporary."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ('executor_fix', 'connection_update', 'forecaster_update', 'policy_update')


def load(path):
    spec = importlib.util.spec_from_file_location('anytime_installer', ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AnytimeInstallers(unittest.TestCase):
    def exercise(self, folder, scenario='ok', hour=12):
        mod = load(folder + '/install.py')
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            repo = root / 'repo'; repo.mkdir()
            package = root / 'package'; (package / folder).mkdir(parents=True)
            old, new = b'VALUE = 1\n', b'VALUE = 2\n'
            (repo / 'sample.py').write_bytes(old)
            (package / 'sample.py').write_bytes(new)
            (package / folder / 'manifest.json').write_text(json.dumps({'sample.py': [mod.blob(old)]}))
            process = root / 'proc' / '123'; process.mkdir(parents=True)
            (process / 'cwd').symlink_to(repo, target_is_directory=True)
            (process / 'cmdline').write_bytes(b'/usr/bin/python3\0run.py\0')
            (process / 'environ').write_bytes(('KADIMA_DATA_DIR=' + str(root)).encode())
            with mod.sqlite3.connect(root / 'kadima.db') as c:
                c.execute('CREATE TABLE positions(status TEXT)')
                c.execute('CREATE TABLE execution_orders(state TEXT)')
                if scenario == 'local_open': c.execute("INSERT INTO positions VALUES ('open')")
                if scenario == 'pending': c.execute("INSERT INTO execution_orders VALUES ('pending')")
            backup = root / 'backup'; backup.mkdir()
            calls, preflights = [], 0

            def local_path(*args):
                p = Path(*args)
                return root / p.relative_to('/') if str(p).startswith('/proc') else p

            def run(args, **kwargs):
                nonlocal preflights
                if args[:2] == ['systemctl', 'show']:
                    return types.SimpleNamespace(stdout='123')
                if args[0] == '/usr/bin/python3':
                    if args[2] == mod.PREFLIGHT:
                        preflights += 1; calls.append('preflight')
                        if (scenario == 'broker_before' and preflights == 1 or
                            scenario == 'broker_after' and preflights == 2):
                            raise subprocess.CalledProcessError(1, args)
                        return types.SimpleNamespace(stdout='KADIMA_PREFLIGHT_OK')
                    if scenario == 'import_failure':
                        raise subprocess.CalledProcessError(1, args)
                else:
                    calls.append(args[1])
                    if (scenario == 'restart_failure' and args[1] == 'start' or
                        scenario == 'health_failure' and args[1] == 'is-active'):
                        raise subprocess.CalledProcessError(1, args)
                return types.SimpleNamespace(stdout='', returncode=0)

            stack.enter_context(patch.object(mod, 'REPO', repo))
            stack.enter_context(patch.object(mod, 'PACKAGE', package))
            stack.enter_context(patch.object(mod, 'Path', side_effect=local_path))
            stack.enter_context(patch.object(mod.os, 'geteuid', return_value=0))
            stack.enter_context(patch.object(mod.os, 'chown'))
            stack.enter_context(patch.object(mod.pwd, 'getpwnam', return_value=types.SimpleNamespace(
                pw_name='ubuntu', pw_uid=process.stat().st_uid, pw_gid=process.stat().st_gid)))
            # If a wall-clock gate returns, daytime cases must fail this regression.
            clock = stack.enter_context(patch.object(mod, 'datetime', create=True))
            clock.now.return_value.hour = hour
            stack.enter_context(patch.object(mod, 'ZoneInfo', create=True))
            stack.enter_context(patch.object(mod, 'run', side_effect=run))
            direct = stack.enter_context(patch.object(mod.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)))
            stack.enter_context(patch.object(mod.tempfile, 'mkdtemp', return_value=str(backup)))
            output = io.StringIO(); stack.enter_context(contextlib.redirect_stdout(output))
            if scenario == 'ok':
                mod.main()
            else:
                with self.assertRaises(SystemExit): mod.main()
            data = (repo / 'sample.py').read_bytes()
            if scenario == 'ok':
                self.assertEqual(data, new)
                self.assertEqual(calls[:3], ['preflight', 'stop', 'preflight'])
            elif scenario in ('restart_failure', 'health_failure'):
                self.assertEqual(data, new)
                self.assertEqual(calls.count('stop'), 1)
                self.assertIn('source retained', output.getvalue())
            else:
                self.assertEqual(data, old)
                if scenario in ('local_open', 'pending', 'broker_before'):
                    self.assertNotIn('stop', calls)
                    direct.assert_not_called()

    def test_updates_at_every_hour(self):
        for folder in FOLDERS:
            for hour in range(24):
                with self.subTest(folder=folder, hour=hour): self.exercise(folder, hour=hour)

    def test_exposure_still_blocks_all_installers(self):
        for folder in FOLDERS:
            for scenario in ('local_open', 'pending', 'broker_before', 'broker_after'):
                with self.subTest(folder=folder, scenario=scenario): self.exercise(folder, scenario)

    def test_restart_failures_never_revert_potentially_trading_source(self):
        for folder in FOLDERS:
            for scenario in ('restart_failure', 'health_failure'):
                with self.subTest(folder=folder, scenario=scenario): self.exercise(folder, scenario)

    def test_pre_restart_failure_restores_source(self):
        self.exercise('executor_fix', 'import_failure')


class PolicyOnlyUpdate(unittest.TestCase):
    def test_install_is_repeatable_and_preserves_application(self):
        mod = load('tools/install_update_policy.py')
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'; repo.mkdir()
            runtime = repo / 'run.py'; runtime.write_text('existing runtime')
            with patch.object(mod.os, 'chown'):
                self.assertIsNotNone(mod.install(ROOT, repo, os.getuid(), os.getgid()))
                self.assertIsNone(mod.install(ROOT, repo, os.getuid(), os.getgid()))
            self.assertEqual(runtime.read_text(), 'existing runtime')
            for folder in FOLDERS:
                self.assertEqual((repo / folder / 'install.py').read_bytes(),
                                 (ROOT / folder / 'install.py').read_bytes())

    def test_unknown_installer_stops_before_any_file_changes(self):
        mod = load('tools/install_update_policy.py')
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'; (repo / 'policy_update').mkdir(parents=True)
            target = repo / 'policy_update/install.py'; target.write_text('# unreviewed edit')
            with self.assertRaisesRegex(ValueError, 'Unreviewed'):
                mod.install(ROOT, repo, os.getuid(), os.getgid())
            self.assertFalse((repo / 'executor_fix').exists())
            self.assertEqual(target.read_text(), '# unreviewed edit')

    def test_write_failure_restores_original_installer(self):
        mod = load('tools/install_update_policy.py')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package = root / 'package'; repo = root / 'repo'
            for base in (package, repo): (base / 'executor_fix').mkdir(parents=True)
            (package / 'tools').mkdir()
            old, new = b'VALUE = 1\n', b'VALUE = 2\n'
            target = repo / 'executor_fix/install.py'; target.write_bytes(old)
            (package / 'executor_fix/install.py').write_bytes(new)
            (package / 'executor_fix/manifest.json').write_text('{}')
            (package / 'tools/update_policy_manifest.json').write_text(json.dumps(
                {'executor_fix/install.py': [mod.blob(old)]}))
            real_replace = os.replace
            def replace(src, dst):
                if Path(dst).name == 'manifest.json': raise OSError('disk failure')
                real_replace(src, dst)
            with patch.object(mod.os, 'replace', side_effect=replace), patch.object(mod.os, 'chown'):
                with self.assertRaises(OSError): mod.install(package, repo, os.getuid(), os.getgid())
            self.assertEqual(target.read_bytes(), old)
            self.assertFalse((repo / 'executor_fix/manifest.json').exists())


if __name__ == '__main__': unittest.main()
