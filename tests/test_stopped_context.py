"""Offline context capture: no service mutation, runtime import, or broker call."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('review_stopped_context',
                                             ROOT/'momentum_recovery/stopped_context.py')
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def evidence(path, raw, *, owner=0, mode=0o600, version=1):
    return m.FileEvidence(Path(path), raw, (1, 10, owner, 0, mode, 1, len(raw), version, version))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(pw_name='ubuntu', pw_uid=1000, pw_gid=1000,
                                    pw_dir='/home/ubuntu', pw_shell='/bin/bash')
        self.properties = {key: '' for key in m.PROPERTIES}
        self.properties.update(LoadState='loaded', ActiveState='inactive', MainPID='0',
            User='ubuntu', Group='', WorkingDirectory=str(m.TARGET), DynamicUser='no',
            NeedDaemonReload='no', FragmentPath='/etc/systemd/system/kadima.service',
            ExecStart='{ path=/home/ubuntu/kadima-py/.venv/bin/python ; '
                'argv[]=/home/ubuntu/kadima-py/.venv/bin/python -u -B run.py ; '
                'ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0 }',
            Environment='ANGEL_API_KEY=private-inline ANGEL_CLIENT_ID=private-account '
                'KADIMA_DATA_DIR=/var/lib/kadima PYTHONUNBUFFERED=1 UNRELATED=retained',
            EnvironmentFiles='/etc/kadima.env (ignore_errors=no)')
        self.files = {
            Path('/etc/systemd/system/kadima.service'): evidence('/etc/systemd/system/kadima.service', b'[Service]\n'),
            Path('/etc/kadima.env'): evidence('/etc/kadima.env', b'ANGEL_API_KEY=private-file\nANGEL_PASSWORD="private password"\n')}
        self.manager = 'LANG=C.UTF-8\nUNRELATED="arbitrary complex \\"value\\""\n'
        self.calls = []
        self.omit_struct_empties = False
        self.extra_output = ''
        self.bus_proofs = {}
        self.interpreter = dict(invocation='/home/ubuntu/kadima-py/.venv/bin/python',
                                real='/usr/bin/python3.13', metadata=(1, 2, 3))

    def invoke(self, command):
        self.calls.append(command)
        if command[0] == '/usr/bin/busctl':
            name = command[-1]
            self.assertIn(name, m.EMPTY_STRUCT_ARRAYS)
            self.assertEqual(command, ['/usr/bin/busctl', '--system', 'get-property',
                'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/kadima_2eservice',
                'org.freedesktop.systemd1.Service', name])
            return self.bus_proofs.get(name, m.EMPTY_STRUCT_ARRAYS[name] + ' 0\n')
        self.assertEqual(command[0], '/usr/bin/systemctl')
        if command[1:] == ['show-environment']:
            return self.manager
        expected = ['/usr/bin/systemctl', 'show', m.UNIT, '--all', '--no-pager', '--full']
        for name in m.PROPERTIES:
            expected.extend(['-p', name])
        self.assertEqual(command, expected, 'Context may only read systemctl properties')
        return ''.join(f'{key}={value}\n' for key, value in self.properties.items()
                       if not (self.omit_struct_empties and not value and key in m.EMPTY_STRUCT_ARRAYS)) + self.extra_output

    def read(self, path, *, missing=False):
        if missing and path not in self.files:
            return None
        self.assertIn(path, self.files, 'Only unit fragments and environment files are read')
        return self.files[path]

    def inspect(self):
        return m.inspect_context(invoke=self.invoke, read=self.read, user=self.user,
                                 interpreter_check=lambda *args: self.interpreter.copy())

    def test_file_order_final_unset_and_private_values(self):
        self.properties['EnvironmentFiles'] += ' /etc/kadima-later.env (ignore_errors=no)'
        self.files[Path('/etc/kadima-later.env')] = evidence('/etc/kadima-later.env',
            b'ANGEL_API_KEY=private-last\nKADIMA_DATA_DIR=/srv/kadima\nDROP=private-remove\n')
        self.properties['UnsetEnvironment'] = 'ANGEL_PASSWORD DROP=private-remove'
        context = self.inspect()
        self.assertEqual(context.env['ANGEL_API_KEY'], 'private-last')
        self.assertEqual(context.env['UNRELATED'], 'retained')
        self.assertNotIn('ANGEL_PASSWORD', context.env)
        self.assertNotIn('DROP', context.env)
        self.assertEqual(context.db_path, Path('/srv/kadima/kadima.db'))
        self.assertEqual(context.python, Path('/home/ubuntu/kadima-py/.venv/bin/python'))
        for secret in ('private-inline', 'private-file', 'private-last', 'private-account', 'private password'):
            self.assertNotIn(secret, repr(context))
            self.assertNotIn(secret, repr(context.summary))
        self.assertEqual(len(context.fingerprints), 3)
        context.guard()

    def test_optional_missing_file_is_fingerprinted_and_creation_blocks_guard(self):
        self.properties['EnvironmentFiles'] += ' /etc/optional.env (ignore_errors=yes)'
        context = self.inspect()
        self.assertIsNone(context.fingerprints[Path('/etc/optional.env')])
        self.files[Path('/etc/optional.env')] = evidence('/etc/optional.env', b'A=1\n')
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()

    def test_real_systemctl_repeated_environment_files_keep_override_order(self):
        self.extra_output = 'EnvironmentFiles=/etc/kadima-later.env (ignore_errors=no)\n'
        self.files[Path('/etc/kadima-later.env')] = evidence('/etc/kadima-later.env', b'ANGEL_API_KEY=private-last\n')
        self.omit_struct_empties = True
        context = self.inspect()
        self.assertEqual(context.env['ANGEL_API_KEY'], 'private-last')
        self.assertEqual(context.summary['property_shape']['property_line_counts']['EnvironmentFiles'], 2)
        self.assertEqual(context.summary['environment_files'], ['/etc/kadima.env', '/etc/kadima-later.env'])
        self.assertEqual(context.summary['property_shape']['typed_empty_properties_verified'],
                         sorted(set(m.EMPTY_STRUCT_ARRAYS) - {'EnvironmentFiles'}))
        context.guard()

    def test_empty_environment_files_requires_typed_zero(self):
        self.properties['EnvironmentFiles'] = ''
        self.omit_struct_empties = True
        context = self.inspect()
        self.assertIn('EnvironmentFiles', context.summary['property_shape']['typed_empty_properties_verified'])
        self.assertEqual(context.env['ANGEL_API_KEY'], 'private-inline')

    def test_missing_array_nonzero_wrong_type_or_unavailable_never_implies_empty(self):
        self.omit_struct_empties = True
        for proof in ('a(sasbttttuii) 1 private-command\n', 'as 0\n', '', 'a(sasbttttuii) 0\nprivate-secret\n'):
            self.bus_proofs['ExecCondition'] = proof
            with self.subTest(proof=proof), self.assertRaisesRegex(m.Blocked, 'empty_property_unverified') as caught:
                self.inspect()
            self.assertNotIn('private-', repr(caught.exception.summary))

    def test_unknown_duplicate_and_malformed_output_have_redacted_shape_diagnostics(self):
        for extra, count_key in [('private-secret-property=private-value\n', 'unknown_property_line_count'),
                                 ('private-secret-no-equals\n', 'malformed_line_count'),
                                 ('ExecStartEx=private-command\n', None),
                                 ('ExecStart=private-command\n', None),
                                 ('Environment=private-value\n', None),
                                 ('EnvironmentFiles=\n', None)]:
            self.extra_output = extra
            with self.subTest(extra=extra), self.assertRaisesRegex(m.Blocked, 'property_shape') as caught:
                self.inspect()
            self.assertNotIn('private-', repr(caught.exception.summary))
            self.assertEqual(caught.exception.summary['User'], 'ubuntu')
            shape = caught.exception.summary['property_shape']
            if count_key:
                self.assertEqual(shape[count_key], 1)
            elif extra.startswith('ExecStart='):
                self.assertEqual(shape['property_line_counts']['ExecStart'], 2)

    def test_missing_scalar_is_reported_and_never_defaulted_to_empty(self):
        del self.properties['Group']
        with self.assertRaisesRegex(m.Blocked, 'property_missing') as caught:
            self.inspect()
        self.assertIn('Group', caught.exception.summary['property_shape']['missing_properties'])
        self.assertFalse(any(call[0] == '/usr/bin/busctl' for call in self.calls))

    def test_empty_array_guard_rechecks_dbus_proof(self):
        self.omit_struct_empties = True
        context = self.inspect()
        self.bus_proofs['ExecStop'] = 'a(sasbttttuii) 1 private-command\n'
        with self.assertRaisesRegex(m.Blocked, 'empty_property_unverified'):
            context.guard()

    def test_guard_rechecks_bytes_metadata_raw_properties_and_interpreter(self):
        context = self.inspect()
        path = Path('/etc/kadima.env')
        original = self.files[path]
        self.files[path] = evidence(path, original.raw, version=2)
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()
        self.files[path] = evidence(path, original.raw + b'OTHER=1\n')
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()
        self.files[path] = original
        self.properties['ExecStart'] = self.properties['ExecStart'].replace('pid=0', 'pid=9')
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()
        self.properties['ExecStart'] = self.properties['ExecStart'].replace('pid=9', 'pid=0')
        self.interpreter['metadata'] = (4, 5, 6)
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()

    def test_guard_rechecks_manager_environment_even_irrelevant_values(self):
        context = self.inspect()
        self.manager += 'ANOTHER=value\n'
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()

    def test_nonstopped_or_different_identity_blocks_before_environment_reads(self):
        for key, value in [('ActiveState', 'active'), ('MainPID', '44'), ('LoadState', 'not-found'),
                           ('User', 'root'), ('Group', 'trader'), ('WorkingDirectory', '/tmp'),
                           ('NeedDaemonReload', 'yes'), ('DynamicUser', 'yes')]:
            original = self.properties[key]
            self.properties[key] = value
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                self.inspect()
            self.properties[key] = original

    def test_hooks_and_layering_refuse_with_safe_actionable_summary(self):
        for name in m.EMPTY_PROPERTIES:
            self.properties[name] = 'private-unreviewed-content'
            with self.subTest(name=name), self.assertRaisesRegex(m.Blocked, 'layering_unsupported') as caught:
                self.inspect()
            summary = caught.exception.summary
            self.assertIn(name, summary['unsupported_context_properties_present'])
            self.assertEqual(summary['User'], 'ubuntu')
            self.assertEqual(summary['environment_files'], ['/etc/kadima.env'])
            self.assertNotIn('private-unreviewed-content', repr(summary))
            self.properties[name] = ''

    def test_wrappers_flags_additional_commands_and_ignore_error_launch_reject(self):
        original = self.properties['ExecStart']
        candidates = [original.replace(' -u -B run.py', ' -c run.py'),
                      original.replace(' -u -B run.py', ' -u run.py extra'),
                      original.replace('ignore_errors=no', 'ignore_errors=yes'),
                      original + ' ; ' + original,
                      original.replace('/home/ubuntu/kadima-py/.venv/bin/python', '/bin/bash')
                              .replace(' -u -B run.py', ' -c run.py')]
        for candidate in candidates:
            self.properties['ExecStart'] = candidate
            with self.subTest(candidate=candidate), self.assertRaises(m.Blocked):
                self.inspect()

    def test_unsafe_runtime_environment_rejects_but_harmless_python_flags_work(self):
        original = self.properties['Environment']
        for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONWARNINGS',
                    'http_proxy', 'HTTPS_PROXY', 'All_Proxy'):
            self.properties['Environment'] = original + f' {key}=private-unsafe'
            with self.subTest(key=key), self.assertRaisesRegex(m.Blocked, 'environment_unsupported') as caught:
                self.inspect()
            self.assertNotIn('private-unsafe', repr(caught.exception.summary))
        self.properties['Environment'] = original + ' PYTHONDONTWRITEBYTECODE=1'
        self.assertEqual(self.inspect().env['PYTHONDONTWRITEBYTECODE'], '1')

    def test_manager_credentials_must_be_explicitly_overridden_or_removed(self):
        self.properties['Environment'] = self.properties['Environment'].replace(' KADIMA_DATA_DIR=/var/lib/kadima', '')
        for name in ('ANGEL_TOTP_SECRET', 'KADIMA_DATA_DIR', 'LD_PRELOAD'):
            self.manager = f'{name}=private-manager\n'
            with self.subTest(name=name), self.assertRaisesRegex(m.Blocked, 'manager_only_runtime'):
                self.inspect()
            self.properties['UnsetEnvironment'] = name
            self.inspect()
            self.properties['UnsetEnvironment'] = f'{name}=private-manager'
            self.inspect()
            self.properties['UnsetEnvironment'] = ''
        self.manager = 'ANGEL_API_KEY=private-manager\n'
        self.assertEqual(self.inspect().env['ANGEL_API_KEY'], 'private-file')

    def test_untrusted_file_owners_and_modes_reject(self):
        for path, owner, mode in [(Path('/etc/kadima.env'), 2000, 0o600),
                                  (Path('/etc/kadima.env'), 1000, 0o666),
                                  (Path('/etc/systemd/system/kadima.service'), 1000, 0o644),
                                  (Path('/etc/systemd/system/kadima.service'), 0, 0o664)]:
            old = self.files[path]
            self.files[path] = evidence(path, old.raw, owner=owner, mode=mode)
            with self.subTest(path=path, owner=owner, mode=mode), self.assertRaises(m.Blocked):
                self.inspect()
            self.files[path] = old

    def test_dropin_is_read_and_guarded(self):
        path = Path('/etc/systemd/system/kadima.service.d/override.conf')
        self.properties['DropInPaths'] = str(path)
        self.files[path] = evidence(path, b'[Service]\nEnvironment=OTHER=1\n', mode=0o644)
        context = self.inspect()
        self.assertIn(path, context.fingerprints)
        self.files[path] = evidence(path, b'[Service]\nEnvironment=OTHER=2\n', mode=0o644)
        with self.assertRaisesRegex(m.Blocked, 'context_changed'):
            context.guard()

    def test_unknown_error_message_cannot_leak_secret(self):
        def failing_read(*args, **kwargs):
            raise RuntimeError('private-secret-from-library')
        with self.assertRaises(m.Blocked) as caught:
            m.inspect_context(invoke=self.invoke, read=failing_read, user=self.user)
        self.assertEqual(str(caught.exception), 'stopped_service_context_unavailable')
        self.assertNotIn('private-secret', repr(caught.exception.summary))


class ParserAndFilesystemTests(unittest.TestCase):
    def test_literal_environment_quoting_never_expands(self):
        self.assertEqual(m.parse_environment('A=plain "B=two words" C=$HOME'),
                         {'A': 'plain', 'B': 'two words', 'C': '$HOME'})
        self.assertEqual(m.parse_environment_file(b'# comment\n; comment\nA="$HOME $(touch /tmp/no)"\n'
                                                 b"B='literal %i # comment'\nC=some spaces\n"),
                         {'A': '$HOME $(touch /tmp/no)', 'B': 'literal %i # comment', 'C': 'some spaces'})
        for value in ('A=%i', 'A="unsupported word"', 'A=back\\slash', 'A="unterminated', 'A=x\x00'):
            with self.subTest(value=value), self.assertRaises(m.Blocked):
                m.parse_environment(value)
        for value in (b'A="first\nsecond"\n', b'A=back\\slash\n', b'export A=1\n',
                      b'A=x\x00\n', b'A=one\x0bB=two\n', b'A="unterminated\n'):
            with self.subTest(value=value), self.assertRaises(m.Blocked):
                m.parse_environment_file(value)

    def test_environment_file_list_refuses_ambiguous_paths(self):
        self.assertEqual(m.parse_environment_files('/etc/a.env (ignore_errors=no) /etc/b.env (ignore_errors=yes)'),
                         [(Path('/etc/a.env'), False), (Path('/etc/b.env'), True)])
        for value in ('/etc/*.env (ignore_errors=no)', '/etc/a%u.env (ignore_errors=no)',
                      '"/etc/has spaces" (ignore_errors=no)', '/etc/a (ignore_errors=no)/etc/b (ignore_errors=no)',
                      '/etc/a (ignore_errors=maybe)', 'relative.env (ignore_errors=no)'):
            with self.subTest(value=value), self.assertRaises(m.Blocked):
                m.parse_environment_files(value)

    def test_real_file_snapshot_refuses_symlinks_hardlinks_and_tracks_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root/'environment'
            path.write_bytes(b'SECRET=private\n')
            first = m.read_regular(path)
            self.assertEqual(first.raw, b'SECRET=private\n')
            self.assertNotIn('private', repr(first))
            path.chmod(0o600)
            self.assertNotEqual(first.metadata, m.read_regular(path).metadata)
            alias = root/'alias'
            alias.symlink_to(path)
            with self.assertRaises(m.Blocked):
                m.read_regular(alias)
            alias.unlink()
            os.link(path, alias)
            with self.assertRaises(m.Blocked):
                m.read_regular(path)
            self.assertIsNone(m.read_regular(root/'missing', missing=True))

    def test_real_system_and_venv_interpreter_metadata_and_config(self):
        user = SimpleNamespace(pw_uid=os.getuid())
        system = Path('/usr/bin/python3')
        if not system.exists():
            self.skipTest('Linux system Python required')
        result = m.verify_interpreter(system, m.TARGET, user, m.read_regular)
        self.assertEqual(result['invocation'], str(system))
        self.assertTrue(result['real'].startswith('/usr/bin/python3'))
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve()
            python = target/'.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.symlink_to(system)
            config = python.parent.parent/'pyvenv.cfg'
            config.write_text('home = /usr/bin\ninclude-system-site-packages = false\n')
            config.chmod(0o600)
            result = m.verify_interpreter(python, target, user, m.read_regular)
            self.assertEqual(result['invocation'], str(python))
            self.assertEqual(result['venv_config'].path, config)
            config.chmod(0o666)
            with self.assertRaisesRegex(m.Blocked, 'config_unreviewed'):
                m.verify_interpreter(python, target, user, m.read_regular)


if __name__ == '__main__':
    unittest.main()
