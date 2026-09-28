"""Read a stopped engine's supported service context without starting Python.

This deliberately supports a strict subset of systemd configuration, not a
general environment emulator. No shell evaluation, database, broker, service
mutation, or runtime-package import occurs. Credentials stay in private fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess


TARGET = Path('/home/ubuntu/kadima-py')
UNIT = 'kadima.service'
PROPERTIES = ('LoadState', 'ActiveState', 'MainPID', 'User', 'Group',
    'WorkingDirectory', 'ExecStart', 'ExecCondition', 'ExecStartPre',
    'ExecStartPost', 'ExecStop', 'ExecStopPost', 'Environment', 'EnvironmentFiles',
    'PassEnvironment', 'UnsetEnvironment', 'PAMName', 'RootDirectory', 'RootImage',
    'BindPaths', 'BindReadOnlyPaths', 'TemporaryFileSystem', 'ExtensionImages',
    'ExtensionDirectories', 'DynamicUser', 'FragmentPath', 'DropInPaths', 'NeedDaemonReload')
EMPTY_PROPERTIES = ('ExecCondition', 'ExecStartPre', 'ExecStartPost', 'ExecStop',
    'ExecStopPost', 'PassEnvironment', 'PAMName', 'RootDirectory', 'RootImage',
    'BindPaths', 'BindReadOnlyPaths', 'TemporaryFileSystem', 'ExtensionImages',
    'ExtensionDirectories')
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
# systemctl's custom printers omit empty arrays of structs even with --all.
# A missing line is admitted only after D-Bus independently returns typed zero.
EMPTY_STRUCT_ARRAYS = {
    **{name: 'a(sasbttttuii)' for name in
       ('ExecCondition', 'ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost')},
    'EnvironmentFiles': 'a(sb)', 'BindPaths': 'a(ssbt)',
    'BindReadOnlyPaths': 'a(ssbt)', 'TemporaryFileSystem': 'a(ss)',
    'ExtensionImages': 'a(sba(ss))',
}
DIAGNOSTIC_PROPERTIES = frozenset(PROPERTIES) | {
    name + 'Ex' for name in PROPERTIES if name.startswith('Exec')}


class Blocked(RuntimeError):
    def __init__(self, reason, summary=None):
        super().__init__(reason)
        self.summary = summary or {}


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def invoke(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=15,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    require(result.returncode == 0 and len(result.stdout) <= 250_000,
            'service_context_unavailable')
    return result.stdout


def signature(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class FileEvidence:
    path: Path
    raw: bytes = field(repr=False)
    metadata: tuple


def read_regular(path, *, missing=False):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=False) == path,
            'context_file_path_unreviewed')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if missing:
            return None
        raise Blocked('context_file_missing') from None
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                'context_regular_file_required')
        raw = stream.read(250_001)
        require(len(raw) <= 250_000 and signature(os.fstat(stream.fileno())) == signature(before),
                'context_file_changed_or_oversized')
    return FileEvidence(path, raw, signature(before))


def words(raw):
    """Only unescaped words or whole quoted words; never shell expansion."""
    require(isinstance(raw, str) and not any(c in raw for c in '\\\n\r\x00'),
            'environment_shape_unsupported')
    result = []
    pattern = re.compile(r'''\s*(?:"([^"]*)"|'([^']*)'|([^\s"']+))''')
    position = 0
    while position < len(raw):
        if not raw[position:].strip():
            break
        match = pattern.match(raw, position)
        require(match is not None, 'environment_shape_unsupported')
        result.append(next(v for v in match.groups() if v is not None))
        position = match.end()
        require(position == len(raw) or raw[position].isspace(), 'environment_shape_unsupported')
    return result


def assignment(value):
    require('=' in value, 'environment_shape_unsupported')
    name, content = value.split('=', 1)
    require(NAME.fullmatch(name) is not None and '\x00' not in content,
            'environment_shape_unsupported')
    return name, content


def parse_environment(raw):
    result = {}
    for value in words(raw):
        name, content = assignment(value)
        require('%' not in content, 'environment_specifier_unsupported')
        result[name] = content
    return result


def parse_environment_file(raw):
    """Literal single-line assignments only; complex systemd quoting refuses."""
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise Blocked('environment_file_encoding_unsupported') from None
    require('\ufeff' not in text and not any(ord(c) < 32 and c not in '\n\r\t' for c in text)
            and not any(c in text for c in '\x7f\x85\u2028\u2029'), 'environment_shape_unsupported')
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        name, content = assignment(line)
        content = content.strip()
        require('\\' not in content, 'environment_shape_unsupported')
        if content.startswith(('"', "'")):
            quote = content[0]
            require(len(content) >= 2 and content[-1] == quote and
                    quote not in content[1:-1], 'environment_shape_unsupported')
            content = content[1:-1]
        else:
            require('"' not in content and "'" not in content, 'environment_shape_unsupported')
        result[name] = content
    return result


def parse_environment_files(raw):
    require(not any(c in raw for c in '\\\n\r\x00%*?[]"\''), 'environment_files_shape_unsupported')
    result, position = [], 0
    pattern = re.compile(r'\s*(/[^\s()]+) \(ignore_errors=(yes|no)\)')
    while position < len(raw):
        if not raw[position:].strip():
            break
        match = pattern.match(raw, position)
        require(match is not None, 'environment_files_shape_unsupported')
        result.append((Path(match.group(1)), match.group(2) == 'yes'))
        position = match.end()
        require(position == len(raw) or raw[position].isspace(), 'environment_files_shape_unsupported')
    require(len(result) <= 16, 'too_many_environment_files')
    return result


def parse_exec_start(raw, target=TARGET):
    match = re.fullmatch(r'\{ path=([^;{}]+) ; argv\[\]=([^;{}]+) ; ([^{}]+) \}', raw)
    require(match is not None, 'engine_launch_shape_unsupported')
    executable, argv = match.group(1).strip(), words(match.group(2).strip())
    require(argv and argv[0] == executable and Path(executable).is_absolute(),
            'engine_interpreter_unreviewed')
    operands = argv[1:]
    while operands and operands[0] in ('-u', '-B'):
        operands = operands[1:]
    require(len(operands) == 1 and operands[0] in ('run.py', str(target/'run.py')),
            'engine_entrypoint_unreviewed')
    # Standard show metadata is inert; no second command or ignore-error launch.
    require(re.search(r'(?:^| ; )ignore_errors=no(?: ; |$)', match.group(3)) is not None,
            'engine_launch_shape_unsupported')
    return Path(executable)


def safe_unit_summary(properties, target=TARGET):
    """Useful diagnostics without printing environment values or command text."""
    result = {'unit': UNIT, 'credentials_printed': False, 'runtime_imported': False}
    for name in ('User', 'Group', 'ActiveState', 'LoadState', 'MainPID', 'DynamicUser', 'NeedDaemonReload'):
        value = properties.get(name, '')
        if re.fullmatch(r'[A-Za-z0-9_.@-]{0,80}', value):
            result[name] = value
    for name in ('WorkingDirectory', 'FragmentPath'):
        value = properties.get(name, '')
        if re.fullmatch(r'/[A-Za-z0-9_./@-]{1,220}', value):
            result[name] = value
    result['unsupported_context_properties_present'] = [name for name in EMPTY_PROPERTIES if properties.get(name)]
    try:
        interpreter = str(parse_exec_start(properties.get('ExecStart', ''), target))
        if re.fullmatch(r'/[A-Za-z0-9_./@-]{1,220}', interpreter):
            result['interpreter'] = interpreter
        result['direct_python_entrypoint'] = True
    except Blocked:
        result['direct_python_entrypoint'] = False
    try:
        values = parse_environment(properties.get('Environment', ''))
        result['inline_environment_names'] = sorted(values)
        result['credential_variables_present'] = [name for name in
            ('ANGEL_API_KEY', 'ANGEL_CLIENT_ID', 'ANGEL_PASSWORD', 'ANGEL_TOTP_SECRET') if values.get(name)]
    except Blocked:
        result['inline_environment_shape_supported'] = False
    try:
        result['environment_files'] = [str(path) for path, _ in
                                       parse_environment_files(properties.get('EnvironmentFiles', ''))]
    except Blocked:
        result['environment_files_shape_supported'] = False
    return result


def service_properties(raw, invoke, summary, target=TARGET):
    """Parse documented repeated EnvironmentFiles; prove absent array empties.

    systemd/src/systemctl/systemctl-show.c prints EnvironmentFiles once per
    (path, optional) entry in array order. Other requested keys stay unique.
    Unknown output never becomes metadata: diagnostics use fixed names/counts.
    """
    rows = {}
    malformed = unknown = 0
    for line in raw.splitlines():
        if '=' not in line:
            malformed += 1
            continue
        name, value = line.split('=', 1)
        if name not in DIAGNOSTIC_PROPERTIES:
            unknown += 1
            continue
        rows.setdefault(name, []).append(value)
    missing = [name for name in PROPERTIES if name not in rows]
    summary['property_shape'] = {
        'line_count': len(raw.splitlines()),
        'property_line_counts': {name: len(values) for name, values in sorted(rows.items())},
        'missing_properties': missing,
        'unknown_property_line_count': unknown,
        'malformed_line_count': malformed,
    }
    diagnostic = {name: values[0] for name, values in rows.items() if len(values) == 1 and name in PROPERTIES}
    summary.update(safe_unit_summary(diagnostic, target))
    require(not malformed and not unknown and all(name in PROPERTIES for name in rows),
            'service_property_shape_unsupported')
    require(all(len(values) == 1 or (name == 'EnvironmentFiles' and all(values))
                for name, values in rows.items()), 'service_property_shape_unsupported')
    properties = {name: ' '.join(values) for name, values in rows.items()}
    # Never infer a missing scalar or an unknown D-Bus property as empty.
    require(all(name in EMPTY_STRUCT_ARRAYS for name in missing), 'service_property_missing')
    proofs = {}
    for name in missing:
        proof = invoke(['/usr/bin/busctl', '--system', 'get-property',
                        'org.freedesktop.systemd1',
                        '/org/freedesktop/systemd1/unit/kadima_2eservice',
                        'org.freedesktop.systemd1.Service', name])
        require(proof in (EMPTY_STRUCT_ARRAYS[name] + ' 0', EMPTY_STRUCT_ARRAYS[name] + ' 0\n'),
                'service_empty_property_unverified')
        proofs[name] = proof
        properties[name] = ''
    summary['property_shape']['typed_empty_properties_verified'] = sorted(proofs)
    summary.update(safe_unit_summary(properties, target))
    return properties, proofs


def verify_interpreter(python, target, user, read):
    """Keep the venv invocation path while verifying its system ELF target."""
    system = re.fullmatch(r'/usr/bin/python3(?:\.[0-9]+)?', str(python)) is not None
    virtual = any(python.parent == target/name/'bin' for name in ('.venv', 'venv'))
    require((system or virtual) and re.fullmatch(r'python(?:3(?:\.[0-9]+)?)?', python.name),
            'engine_interpreter_path_unreviewed')
    real = python.resolve(strict=True)
    require(re.fullmatch(r'/usr/bin/python3(?:\.[0-9]+)?', str(real)) is not None,
            'engine_interpreter_target_unreviewed')
    info = real.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
            not info.st_mode & 0o022 and info.st_mode & 0o111,
            'engine_interpreter_metadata_unreviewed')
    with real.open('rb') as stream:
        require(stream.read(4) == b'\x7fELF', 'engine_interpreter_not_system_binary')
    links = []
    current = python
    for _ in range(8):
        meta = current.lstat()
        require(meta.st_uid in (0, user.pw_uid), 'engine_interpreter_link_owner_unreviewed')
        links.append((str(current), signature(meta), os.readlink(current) if current.is_symlink() else None))
        if not current.is_symlink():
            break
        destination = Path(os.readlink(current))
        current = destination if destination.is_absolute() else current.parent/destination
    else:
        raise Blocked('engine_interpreter_link_chain_unsupported')
    config = None
    if virtual:
        config = read(python.parent.parent/'pyvenv.cfg')
        require(config.metadata[2] in (0, user.pw_uid) and not config.metadata[4] & 0o022,
                'virtual_environment_config_unreviewed')
    return dict(invocation=str(python), real=str(real), metadata=signature(info),
                links=tuple(links), venv_config=config)


@dataclass
class Context:
    python: Path
    db_path: Path
    summary: dict
    env: dict = field(repr=False)
    fingerprints: dict = field(repr=False)
    _signature: tuple = field(repr=False)
    _capture: object = field(repr=False)

    def guard(self):
        current = self._capture()
        require(current._signature == self._signature, 'stopped_service_context_changed')


def inspect_context(*, invoke=invoke, read=read_regular, user=None, target=TARGET,
                    interpreter_check=verify_interpreter):
    user = user or pwd.getpwnam('ubuntu')
    require(user.pw_name == 'ubuntu', 'reviewed_service_user_required')
    target = Path(target)
    summary = {'unit': UNIT, 'credentials_printed': False, 'runtime_imported': False}
    try:
        command = ['/usr/bin/systemctl', 'show', UNIT, '--all', '--no-pager', '--full']
        for property_name in PROPERTIES:
            command.extend(['-p', property_name])
        raw = invoke(command)
        require(isinstance(raw, str) and len(raw) <= 250_000, 'service_context_unavailable')
        properties, empty_proofs = service_properties(raw, invoke, summary, target)
        summary['engine_stopped'] = properties['ActiveState'] == 'inactive' and properties['MainPID'] == '0'
        require(properties['LoadState'] == 'loaded' and summary['engine_stopped'], 'engine_must_remain_stopped')
        require(properties['User'] == 'ubuntu' and properties['Group'] in ('', 'ubuntu'), 'service_owner_unreviewed')
        require(properties['WorkingDirectory'] == str(target), 'service_directory_unreviewed')
        require(all(properties[name] == '' for name in EMPTY_PROPERTIES) and
                properties['DynamicUser'] == 'no', 'service_context_layering_unsupported')
        require(properties['NeedDaemonReload'] == 'no', 'service_configuration_reload_pending')
        python = parse_exec_start(properties['ExecStart'], target)
        summary.update(user='ubuntu', working_directory=str(target), interpreter=str(python))
        fingerprints = {}
        unit_paths = [properties['FragmentPath'], *words(properties['DropInPaths'])]
        require(len(unit_paths) <= 20, 'too_many_unit_fragments')
        for name in unit_paths:
            path = Path(name)
            require(path.is_absolute() and any(path.is_relative_to(prefix) for prefix in
                    (Path('/etc/systemd/system'), Path('/run/systemd/system'), Path('/usr/lib/systemd/system'))),
                    'unit_fragment_path_unreviewed')
            item = read(path)
            require(item.metadata[2] == 0 and not item.metadata[4] & 0o022,
                    'unit_fragment_metadata_unreviewed')
            fingerprints[path] = item
        env = parse_environment(properties['Environment'])
        for path, optional in parse_environment_files(properties['EnvironmentFiles']):
            item = read(path, missing=optional)
            if item is not None:
                require(item.metadata[2] in (0, user.pw_uid) and not item.metadata[4] & 0o022,
                        'environment_file_metadata_unreviewed')
                env.update(parse_environment_file(item.raw))
            fingerprints[path] = item
        explicit_names = set(env)
        unsets = words(properties['UnsetEnvironment'])
        for entry in unsets:
            name = entry.split('=', 1)[0]
            require(NAME.fullmatch(name) is not None, 'unset_environment_shape_unsupported')
            if '=' not in entry or env.get(name) == entry.split('=', 1)[1]:
                env.pop(name, None)
        manager_raw = invoke(['/usr/bin/systemctl', 'show-environment'])
        unsafe = lambda name: (name.startswith('LD_') or
            (name.startswith('PYTHON') and name not in ('PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE')) or
            name.lower() in ('http_proxy', 'https_proxy', 'all_proxy'))
        relevant = lambda name: name.startswith('ANGEL_') or name == 'KADIMA_DATA_DIR' or unsafe(name)
        manager = {}
        for line in manager_raw.splitlines():
            if line.strip():
                # Irrelevant manager values need not fit our supported quoting
                # subset. They are neither credential nor interpreter inputs.
                candidate = line.split('=', 1)[0]
                if NAME.fullmatch(candidate) and not relevant(candidate):
                    continue
                values = words(line)
                require(len(values) == 1, 'manager_environment_shape_unsupported')
                name, content = assignment(values[0])
                manager[name] = content
        removed_manager = {entry for entry in unsets if '=' not in entry}
        removed_manager.update(entry.split('=', 1)[0] for entry in unsets if '=' in entry
                               and manager.get(entry.split('=', 1)[0]) == entry.split('=', 1)[1])
        require(not any(relevant(name) and name not in explicit_names and name not in removed_manager
                        for name in manager),
                'manager_only_runtime_environment_unsupported')
        require(not any(unsafe(name) and value for name, value in env.items()),
                'interpreter_or_proxy_environment_unsupported')
        # No SSH/login-shell environment is inherited as a source of credentials.
        data_dir = Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))
        require(data_dir.is_absolute() and data_dir.resolve(strict=False) == data_dir,
                'database_directory_unreviewed')
        interpreter = interpreter_check(python, target, user, read)
        summary.update(environment_names=sorted(env), environment_file_count=len(parse_environment_files(properties['EnvironmentFiles'])),
                       unit_fragment_count=len(unit_paths), data_directory=str(data_dir),
                       interpretation='supported_service_explicit_environment_only')
        options = dict(invoke=invoke, read=read, user=user, target=target,
                       interpreter_check=interpreter_check)
        return Context(python, data_dir/'kadima.db', summary, env, fingerprints,
                       (raw, empty_proofs, manager_raw, fingerprints, interpreter), lambda: inspect_context(**options))
    except Blocked as exc:
        exc.summary = summary
        raise
    except Exception:
        raise Blocked('stopped_service_context_unavailable', summary) from None
