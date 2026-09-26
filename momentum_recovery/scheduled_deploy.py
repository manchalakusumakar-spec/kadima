"""One guarded deployment attempt at 09:15 IST on 28 September 2026.

Setup copies pinned sources; run never fetches code or enables trading. The
existing repair/installers retain their own source, exposure and restart gates.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


SCHEDULED_AT = datetime(2026, 9, 28, 3, 45, tzinfo=timezone.utc)
BUNDLE = Path('/opt/kadima-deploy-20260928')
STATE = Path('/var/lib/kadima-deploy-20260928')
UNIT = 'kadima-deploy-20260928'
SYSTEMD = Path('/etc/systemd/system')
TIMER = 'kadima-session-auto.timer'
SERVICE = 'kadima-session-auto.service'
TARGET = Path('/home/ubuntu/kadima-py')
RELEASES = {
    'repair': {'commit': 'e4277654951164e6b59fcdcec4670a44eafe0cf1',
               'tree': '0016362f38413481da6a253c0cdd6dfbac6fd2a8'},
    'momentum': {'commit': 'bd294a7048749258fbaa2cc786f20a441a2a8073',
                 'tree': '1afbeca8a8658039b031817be65a11e47567e4c5'},
    'audit': {'commit': 'd584fe0eba27a8f23d2dfd2bac8fb68cb48501ce',
              'tree': 'ac55cb5636746b7572f62608b43964df35ce4ddb'},
}


class Blocked(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def utc_now():
    return datetime.now(timezone.utc)


def admit_setup(now):
    require(now.tzinfo is not None and now < SCHEDULED_AT, 'schedule_has_passed')


def admit_run(now):
    require(now.tzinfo is not None and SCHEDULED_AT <= now < SCHEDULED_AT+timedelta(seconds=60),
            'outside_single_start_window')


def canonical(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=False) == path, 'noncanonical_or_linked_path')
    return path


def private_path(path, *, directory=False):
    path = canonical(path)
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o077, 'root_private_path_required')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            'unexpected_path_type')
    require(directory or info.st_nlink == 1, 'linked_file_refused')
    return path


def root_parents(path):
    path = canonical(path)
    for parent in path.parents:
        info = parent.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'unsafe_parent_directory')


def read_regular(path, limit=4_000_000):
    path = canonical(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), 'regular_file_required')
        raw = handle.read(limit+1)
    require(len(raw) <= limit, 'file_too_large')
    return raw


def blob_hash(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_private(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(Path(path).parent)


def claim_attempt(state_dir, now):
    private_path(state_dir, directory=True)
    try:
        create_private(Path(state_dir)/'attempted.json', json.dumps({'started_at': now.isoformat()}).encode())
    except FileExistsError:
        raise Blocked('attempt_already_recorded_no_retry') from None


def record_status(state_dir, value):
    private_path(state_dir, directory=True)
    value = dict(value, observed_at=utc_now().isoformat())
    fd, temporary = tempfile.mkstemp(prefix='.status-', dir=state_dir)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        destination = Path(state_dir)/'status.json'
        if destination.exists():
            private_path(destination)
        os.replace(temporary, destination)
        sync_directory(state_dir)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def observe_unit(name):
    result = subprocess.run(['/usr/bin/systemctl', 'show', name, '-p', 'LoadState',
                             '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'MainPID',
                             '-p', 'FragmentPath', '-p', 'DropInPaths'],
                            capture_output=True, text=True, timeout=15)
    data = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    require(result.returncode == 0 or data.get('LoadState') == 'not-found', 'unit_state_unavailable')
    return data


def session_auto_admission(observed):
    for name in (TIMER, SERVICE):
        data = observed.get(name, {})
        require(data.get('ActiveState') == 'inactive', 'session_auto_must_be_inactive')
        if data.get('LoadState') == 'not-found':
            continue
        require(name == TIMER or data.get('MainPID') == '0', 'session_auto_process_not_stopped')
        require(data.get('LoadState') in ('loaded', 'masked'), 'session_auto_unit_state_unknown')
        allowed = ('disabled', 'masked', 'masked-runtime') + (('static',) if name == SERVICE else ())
        require(data.get('UnitFileState') in allowed,
                'session_auto_must_be_disabled')


def check_session_auto():
    # Release this lock before invoking repair, which owns the same lock itself.
    fd = os.open('/run/lock/kadima-session-auto-run.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('session_auto_check_running') from None
        session_auto_admission({name: observe_unit(name) for name in (TIMER, SERVICE)})


def safe_relative(name):
    path = PurePosixPath(name)
    require(bool(name) and not path.is_absolute() and str(path) == name
            and all(part not in ('', '.', '..', '.git') for part in path.parts), 'unsafe_release_path')
    return name


def git(history, args):
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}
    result = subprocess.run(['/usr/bin/git', '--no-replace-objects', '-C', str(history),
                             '-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null',
                             '-c', 'safe.directory='+str(canonical(history)),
                             *args], env=env, capture_output=True, timeout=60)
    require(result.returncode == 0, 'pinned_git_object_unavailable')
    return result.stdout


def manifest_tree(files):
    """Bind the full path/blob manifest to the pinned Git tree object itself."""
    nodes = {}
    for relative, entry in files.items():
        safe_relative(relative)
        parts = relative.split('/')
        current = nodes
        for part in parts[:-1]:
            child = current.setdefault(part, {})
            require(isinstance(child, dict), 'manifest_path_collision')
            current = child
        require(parts[-1] not in current, 'manifest_path_collision')
        require(entry.get('mode') in ('100644', '100755') and
                isinstance(entry.get('blob'), str) and len(entry['blob']) == 40 and
                all(char in '0123456789abcdef' for char in entry['blob']), 'manifest_blob_invalid')
        current[parts[-1]] = (entry['mode'], entry['blob'])
    def calculate(node):
        raw = bytearray()
        for name, value in sorted(node.items(), key=lambda item: (item[0]+('/' if isinstance(item[1], dict) else '')).encode()):
            mode, digest = ('40000', calculate(value)) if isinstance(value, dict) else value
            raw.extend(mode.encode()+b' '+name.encode()+b'\0'+bytes.fromhex(digest))
        return hashlib.sha1(b'tree '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    return calculate(nodes)


def stage_release(history, destination, pin):
    require(git(history, ['rev-parse', '--verify', pin['commit']+'^{commit}']).decode().strip() == pin['commit'],
            'release_commit_mismatch')
    require(git(history, ['rev-parse', '--verify', pin['commit']+'^{tree}']).decode().strip() == pin['tree'],
            'release_tree_mismatch')
    files = {}
    for item in git(history, ['ls-tree', '-rz', pin['tree']]).split(b'\0'):
        if not item:
            continue
        metadata, name = item.split(b'\t', 1)
        mode, kind, digest = metadata.decode().split()
        require(kind == 'blob' and mode in ('100644', '100755'), 'release_links_or_submodules_refused')
        name = safe_relative(name.decode())
        files[name] = {'blob': digest, 'mode': mode}
    require(bool(files), 'empty_release_refused')
    require(manifest_tree(files) == pin['tree'], 'release_manifest_tree_mismatch')
    destination.mkdir(mode=0o700)
    seen = set()
    archive = git(history, ['archive', '--format=tar', pin['commit']])
    require(len(archive) <= 32_000_000, 'release_archive_too_large')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
        for member in source:
            if member.isdir():
                safe_relative(member.name.rstrip('/'))
                continue
            name = safe_relative(member.name)
            require(member.isfile() and name in files and name not in seen, 'unexpected_archive_entry')
            require(member.size <= 4_000_000, 'release_file_too_large')
            raw = source.extractfile(member).read()
            require(blob_hash(raw) == files[name]['blob'], 'archive_blob_mismatch')
            target = destination/name
            parent = destination
            for part in Path(name).parts[:-1]:
                parent = parent/part
                parent.mkdir(mode=0o700, exist_ok=True)
                private_path(parent, directory=True)
            create_private(target, raw)
            seen.add(name)
    require(seen == set(files), 'archive_missing_release_files')
    return dict(pin, files=files)


def verify_bundle(bundle):
    bundle = private_path(bundle, directory=True)
    root_parents(bundle)
    manifest = json.loads(read_regular(private_path(bundle/'manifest.json')))
    require(manifest.get('version') == 1 and manifest.get('scheduled_at') == SCHEDULED_AT.isoformat(),
            'bundle_manifest_invalid')
    require(set(manifest.get('releases', {})) == set(RELEASES), 'bundle_releases_invalid')
    expected = {'manifest.json', 'scheduled_deploy.py', 'evidence.json'}
    for name, pin in RELEASES.items():
        release = manifest['releases'][name]
        require(all(release.get(key) == value for key, value in pin.items()), 'bundle_release_pin_changed')
        require(isinstance(release.get('files'), dict) and bool(release['files']), 'bundle_files_missing')
        require(manifest_tree(release['files']) == pin['tree'], 'bundle_manifest_tree_changed')
        for relative, entry in release['files'].items():
            safe_relative(relative)
            require(entry.get('mode') in ('100644', '100755'), 'bundle_file_mode_invalid')
            path = bundle/'releases'/name/relative
            require(blob_hash(read_regular(private_path(path))) == entry.get('blob'), 'bundle_source_changed')
            expected.add(path.relative_to(bundle).as_posix())
    for name, key in (('evidence.json', 'evidence_sha256'), ('scheduled_deploy.py', 'runner_sha256')):
        require(hashlib.sha256(read_regular(private_path(bundle/name))).hexdigest() == manifest.get(key),
                'bundle_evidence_or_runner_changed')
    observed = set()
    for parent, directories, files in os.walk(bundle, followlinks=False):
        private_path(parent, directory=True)
        for name in directories:
            private_path(Path(parent)/name, directory=True)
        for name in files:
            path = private_path(Path(parent)/name)
            observed.add(path.relative_to(bundle).as_posix())
    require(observed == expected, 'unexpected_bundle_files')
    return manifest


def reviewed_repair(bundle):
    verify_bundle(bundle)
    # This pinned module contains only stdlib helpers at import time. Its main
    # and all broker/repair operations remain uncalled here.
    sys.dont_write_bytecode = True
    path = Path(bundle)/'releases/repair/momentum_recovery/reconcile_contract_note.py'
    spec = importlib.util.spec_from_file_location('_scheduled_reviewed_repair', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_api(path, token):
    require(path in ('top20', 'stock-fast', 'audit?limit=1', 'prices?limit=1'), 'unknown_health_route')
    request = urllib.request.Request('http://127.0.0.1:8081/api/kadima/'+path,
                                     headers={'Authorization': 'Bearer '+token}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=8) as response:
        raw = response.read(2_000_001)
    require(len(raw) <= 2_000_000, 'health_response_too_large')
    return json.loads(raw)


def check_controls(*, final=False):
    token = json.loads(read_regular('/etc/kadima-dashboard/config.json'))['api_token']
    require(isinstance(token, str) and 0 < len(token) < 8192, 'control_token_unavailable')
    data = local_api('top20', token)
    stamp = datetime.fromisoformat(data['ts'])
    require(stamp.tzinfo is not None and 0 <= (utc_now()-stamp).total_seconds() <= 30, 'stale_control_state')
    cfg = data.get('config', {})
    require(cfg.get('auto_trade_enabled') is False and cfg.get('kill_latched') is False,
            'global_auto_must_remain_paused')
    stock = local_api('stock-fast', token)
    stock_stamp = datetime.fromisoformat(stock['ts'])
    require(stock_stamp.tzinfo is not None and 0 <= (utc_now()-stock_stamp).total_seconds() <= 30,
            'stale_stock_control_state')
    require(stock.get('controls', {}).get('live_enabled') is False, 'stock_live_intent_must_be_off')
    if not final:
        return
    require(data.get('positions') == [], 'final_api_exposure_or_unknown')
    for route in ('audit?limit=1', 'prices?limit=1'):
        result = local_api(route, token)
        require(isinstance(result.get('rows'), list) and type(result.get('total')) is int,
                'final_history_api_invalid')
        if route.startswith('prices'):
            require(result.get('capture_enabled') is True, 'final_price_capture_disabled')


def final_database_check(bundle=BUNDLE):
    # Match the actual running process's directory/owner and reviewed config
    # assignments before opening its database. Never import the live package.
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'kadima.service', '-p', 'MainPID'],
                            capture_output=True, text=True, check=True, timeout=15)
    pid = result.stdout.strip().removeprefix('MainPID=')
    require(pid.isdigit() and int(pid) > 0, 'engine_pid_unknown')
    proc = Path('/proc')/pid
    require(proc.stat().st_uid == pwd.getpwnam('ubuntu').pw_uid and (proc/'cwd').resolve() == TARGET,
            'engine_identity_changed')
    helper = reviewed_repair(bundle)
    identity = helper.process_identity(pid)
    env_raw = (proc/'environ').read_bytes()
    argv_raw = (proc/'cmdline').read_bytes()
    env = dict(os.fsdecode(item).split('=', 1) for item in env_raw.split(b'\0') if b'=' in item)
    helper.account_matches(json.loads(read_regular(Path(bundle)/'evidence.json')), env)
    config_raw = read_regular(TARGET/'kadima/config.py')
    source = ast.parse(config_raw)
    os_bindings = helper.identifier_bindings(source, 'os')
    require(len(os_bindings) == 1 and os_bindings[0] in source.body and
            ast.dump(os_bindings[0], include_attributes=False) ==
            ast.dump(ast.parse('import os').body[0], include_attributes=False), 'runtime_environment_binding_unreviewed')
    for name, statement in (
            ('ANGEL_CLIENT_ID', 'ANGEL_CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID", "")'),
            ('DATA_DIR', 'DATA_DIR = os.environ.get("KADIMA_DATA_DIR", "/var/lib/kadima")'),
            ('DB_PATH', 'DB_PATH = os.path.join(DATA_DIR, "kadima.db")')):
        assignments = [node for node in source.body if isinstance(node, ast.Assign)
                       and ast.dump(node, include_attributes=False) ==
                       ast.dump(ast.parse(statement).body[0], include_attributes=False)]
        bindings = helper.identifier_bindings(source, name)
        require(len(assignments) == len(bindings) == 1, 'runtime_database_config_unreviewed')
    db = canonical(Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db')
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=2) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        require(connection.execute("SELECT 1 FROM positions WHERE status IS NULL OR status!='closed' LIMIT 1").fetchone() is None,
                'final_local_position_unresolved')
        require(connection.execute("SELECT 1 FROM execution_orders WHERE state IS NULL OR state!='terminal' LIMIT 1").fetchone() is None,
                'final_local_order_unresolved')
        for table, field in (('celestial_config', 'auto_trade_enabled'), ('stock_fast_controls', 'live_enabled')):
            row = connection.execute('SELECT payload FROM '+table+' WHERE id=1').fetchone()
            require(row is not None and json.loads(row[0]).get(field) is False, 'final_persistent_controls_not_paused')
    require(helper.process_identity(pid) == identity and (proc/'cwd').resolve() == TARGET and
            (proc/'environ').read_bytes() == env_raw and (proc/'cmdline').read_bytes() == argv_raw and
            read_regular(TARGET/'kadima/config.py') == config_raw, 'engine_changed_during_final_check')
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'kadima.service', '-p', 'MainPID'],
                            capture_output=True, text=True, check=True, timeout=15)
    require(result.stdout.strip() == 'MainPID='+pid, 'engine_changed_during_final_check')


def stage_commands(bundle):
    bundle = Path(bundle)
    prefix = ['/usr/bin/python3', '-I', '-B']
    return [
        ('repair', prefix+[str(bundle/'releases/repair/momentum_recovery/reconcile_contract_note.py'),
                           '--evidence', str(bundle/'evidence.json'), '--apply']),
        ('momentum', prefix+[str(bundle/'releases/momentum/momentum_recovery/install.py'),
                             '--broker-check', '--apply']),
        ('audit', prefix+[str(bundle/'releases/audit/trade_audit_update/install.py'),
                          '--with-dashboard', '--broker-check', '--apply']),
    ]


def run_stages(stages, invoke, record, pre_stage=lambda: None):
    for name, argv in stages:
        record({'state': 'checking', 'stage': name})
        pre_stage()
        record({'state': 'running', 'stage': name})
        result = invoke(argv)
        record({'state': 'stage_complete' if result.returncode == 0 else 'blocked',
                'stage': name, 'child_exit': result.returncode})
        require(result.returncode == 0, 'stage_failed:'+name)
    return 0


def render_service():
    return f'''[Unit]
Description=One guarded KADIMA source deployment on 28 September 2026
After=network-online.target time-sync.target
[Service]
Type=oneshot
User=root
Group=root
UMask=0077
ExecStart=/usr/bin/python3 -I -B {BUNDLE}/scheduled_deploy.py run
TimeoutStartSec=infinity
TimeoutStopSec=infinity
KillMode=control-group
KillSignal=SIGINT
Restart=no
StandardOutput=journal
StandardError=journal
'''


def render_timer():
    return f'''[Unit]
Description=Attempt KADIMA deployment once at 09:15 IST on 28 September 2026
[Timer]
OnCalendar=2026-09-28 03:45:00 UTC
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit={UNIT}.service
[Install]
WantedBy=timers.target
'''


def check_own_units(*, absent=False):
    for suffix, rendered in (('service', render_service()), ('timer', render_timer())):
        name = UNIT+'.'+suffix
        path = SYSTEMD/name
        require(not Path(str(path)+'.d').exists(), 'deployment_unit_override_refused')
        data = observe_unit(name)
        require(not data.get('DropInPaths'), 'deployment_unit_override_refused')
        if absent:
            require(data.get('LoadState') == 'not-found' and data.get('ActiveState') == 'inactive',
                    'deployment_unit_already_loaded')
        else:
            require(data.get('LoadState') == 'loaded' and data.get('FragmentPath') == str(path)
                    and read_regular(private_path(path)) == rendered.encode(), 'deployment_unit_changed')


def setup(history, evidence):
    require(os.geteuid() == 0, 'sudo_required')
    admit_setup(utc_now())
    check_session_auto()
    check_controls()
    check_own_units(absent=True)
    for path in (BUNDLE, STATE, SYSTEMD/(UNIT+'.service'), SYSTEMD/(UNIT+'.timer')):
        root_parents(path)
        require(not path.exists(), 'schedule_path_already_exists_review_required')
    history = canonical(history)
    evidence = canonical(evidence)
    info = evidence.lstat()
    require(info.st_uid in (0, pwd.getpwnam('ubuntu').pw_uid) and not info.st_mode & 0o077
            and stat.S_ISREG(info.st_mode), 'private_evidence_file_required')
    raw_evidence = read_regular(evidence, 100_000)
    require(isinstance(json.loads(raw_evidence), dict), 'evidence_object_required')
    temporary = Path(tempfile.mkdtemp(prefix='.kadima-deploy-', dir=BUNDLE.parent))
    try:
        (temporary/'releases').mkdir(mode=0o700)
        manifests = {name: stage_release(history, temporary/'releases'/name, pin)
                     for name, pin in RELEASES.items()}
        raw_runner = read_regular(Path(__file__).resolve())
        create_private(temporary/'scheduled_deploy.py', raw_runner)
        create_private(temporary/'evidence.json', raw_evidence)
        manifest = dict(version=1, scheduled_at=SCHEDULED_AT.isoformat(), releases=manifests,
                        runner_sha256=hashlib.sha256(raw_runner).hexdigest(),
                        evidence_sha256=hashlib.sha256(raw_evidence).hexdigest())
        create_private(temporary/'manifest.json', json.dumps(manifest, sort_keys=True).encode())
        verify_bundle(temporary)
        reviewed_repair(temporary).evidence(json.loads(raw_evidence), utc_now())
        # Monday's live account, source and restored-position admission remains
        # in the pinned repair. Setup validates only the pure evidence schema.
        check_session_auto()
        check_controls()
        admit_setup(utc_now())
        os.rename(temporary, BUNDLE)
        sync_directory(BUNDLE.parent)
        STATE.mkdir(mode=0o700)
        create_private(SYSTEMD/(UNIT+'.service'), render_service().encode())
        create_private(SYSTEMD/(UNIT+'.timer'), render_timer().encode())
        subprocess.run(['/usr/bin/systemd-analyze', 'verify', str(SYSTEMD/(UNIT+'.service')),
                        str(SYSTEMD/(UNIT+'.timer'))], check=True, capture_output=True, timeout=30)
        subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True, timeout=30)
        check_own_units()
        record_status(STATE, {'state': 'arming_timer', 'scheduled_at': SCHEDULED_AT.isoformat()})
        try:
            admit_setup(utc_now())
            subprocess.run(['/usr/bin/systemctl', 'enable', '--now', UNIT+'.timer'], check=True, timeout=30)
            check_own_units()
            require(observe_unit(UNIT+'.timer').get('ActiveState') == 'active', 'deployment_timer_not_active')
            record_status(STATE, {'state': 'scheduled', 'scheduled_at': SCHEDULED_AT.isoformat()})
        except Exception:
            print('SCHEDULE MAY BE ARMED: inspect '+UNIT+'.timer and the saved status before any retry.')
            raise
        print('SCHEDULED: 28 September 2026 at 09:15 IST; one guarded attempt, no retry or activation.')
        print('STATUS: '+str(STATE/'status.json'))
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def run():
    require(os.geteuid() == 0, 'sudo_required')
    private_path(STATE, directory=True)
    root_parents(STATE)
    fd = os.open(STATE/'runner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('scheduled_attempt_running') from None
        now = utc_now()
        admit_run(now)
        claim_attempt(STATE, now)
        latest = {'state': 'starting', 'stage': 'admission'}
        def record(value):
            latest.clear()
            latest.update(value)
            record_status(STATE, value)
        try:
            def pre_stage():
                verify_bundle(BUNDLE)
                check_own_units()
                check_session_auto()
                check_controls()
            run_stages(stage_commands(BUNDLE),
                       lambda argv: subprocess.run(argv, cwd=BUNDLE, check=False), record, pre_stage)
            record({'state': 'running', 'stage': 'final_verification'})
            pre_stage()
            for unit in ('kadima.service', 'kadima-universe-scanner.service', 'kadima-dashboard.service'):
                require(observe_unit(unit).get('ActiveState') == 'active', 'final_service_not_active')
            check_controls(final=True)
            final_database_check()
            record({'state': 'verified', 'stage': 'complete', 'activation': False})
            print('DEPLOYMENT VERIFIED: repair, momentum and audit/history installed; auto remains paused.')
        except BaseException as exc:
            value = dict(latest, state='blocked', reason=str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
            record_status(STATE, value)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    configure = commands.add_parser('setup')
    configure.add_argument('--history', type=Path, required=True)
    configure.add_argument('--evidence', type=Path, required=True)
    commands.add_parser('run')
    args = parser.parse_args()
    if args.command == 'setup':
        setup(args.history, args.evidence)
    else:
        run()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:', str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        raise SystemExit(1)
