"""Tighten exact release sources from engine-owned 0664 to 0644, then stop.

No source content, ownership, directory, service, broker or database change.
The normal installer runs separately and retains every admission requirement.
"""
from contextlib import ExitStack
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
ROOT = Path('/home/ubuntu/kadima-py')
INSTALL_BLOB = '02358b537c39b2c05682a9ad9b598b0386aa3099'


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def blob(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def load_install():
    path = PACKAGE/'install.py'
    require(blob(path.read_bytes()) == INSTALL_BLOB, 'priority_installer_changed')
    spec = importlib.util.spec_from_file_location('_priority_mode_installer', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink)


def verify(row, mode=None):
    path, fd = row['path'], row['fd']
    require(path.resolve(strict=False) == path, 'source_path_changed:'+row['relative'])
    current = os.fstat(fd)
    expected = list(row['identity'])
    if mode is not None:
        expected[4] = mode
    require(identity(current) == tuple(expected) and
            identity(path.lstat()) == tuple(expected), 'source_identity_changed:'+row['relative'])
    raw = os.pread(fd, 2_000_001, 0)
    require(len(raw) == current.st_size and blob(raw) == row['blob'],
            'source_bytes_changed:'+row['relative'])


def plan_sources(root, manifest, owner, stack):
    require(root.is_absolute() and root.resolve() == root, 'source_root_noncanonical')
    rows = []
    for relative, pin in manifest['runtime'].items():
        require(re.fullmatch(r'(run|kadima/[a-z_]+)\.py', relative) is not None,
                'source_manifest_path_unreviewed')
        path = root/relative
        require(path.resolve(strict=False) == path, 'source_path_noncanonical:'+relative)
        if pin['old'] is None:
            require(not path.exists(), 'unexpected_existing_module:'+relative)
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'regular_single_link_required:'+relative)
        fixable = (info.st_uid, info.st_gid, mode) == (*owner, 0o664)
        read_bit = 0o400 if info.st_uid == owner[0] else (0o040 if info.st_gid == owner[1] else 0o004)
        protected = info.st_uid in (0, owner[0]) and not mode & 0o7022 and bool(mode & read_bit)
        require(fixable or protected,
                f'source_mode_not_repairable:{relative}:uid={info.st_uid}:gid={info.st_gid}:mode={mode:04o}')
        row = dict(path=path, fd=fd, relative=relative, identity=identity(info),
                   blob=pin['old'], tighten=fixable)
        verify(row)
        rows.append(row)
    return rows


def repair_modes(rows, record, apply):
    # Validate the whole manifest before tightening any file. Recheck each open
    # descriptor, linked inode, owner/mode and exact source before and after chmod.
    for row in rows:
        verify(row)
    planned = [dict(path=r['relative'], blob=r['blob'], uid=r['identity'][2], gid=r['identity'][3],
                    old_mode='0664', new_mode='0644', device=r['identity'][0], inode=r['identity'][1])
               for r in rows if r['tighten']]
    report = dict(state='source_permission_plan', planned=planned, tightened=[],
                  mutation_attempted=False, content_changed=False, ownership_changed=False,
                  service_changes_requested=False, deployment_performed=False)
    if not apply:
        return report
    record(report)
    try:
        for row in rows:
            verify(row)
            if not row['tighten']:
                continue
            report.update(in_progress=row['relative'], mutation_attempted=True)
            record(report)
            verify(row)
            os.fchmod(row['fd'], 0o644)
            os.fsync(row['fd'])
            verify(row, mode=0o644)
            report['tightened'].append(row['relative'])
            report.pop('in_progress', None)
            record(report)
        report['state'] = 'source_permissions_verified'
        record(report)
        return report
    except BaseException as exc:
        report.update(state='source_permission_repair_incomplete', error_type=type(exc).__name__)
        exc.report = report
        try:
            record(report)
        except Exception:
            report['audit_update_failed'] = True
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    os.umask(0o077)
    report_path = None
    try:
        install = load_install()
        old = install.load_activation()
        p = old.load('momentum_recovery/start_paused_peripherals.py')
        ready = p.load_start().load_repair().load_readiness()
        helper = ready.load_reviewed('scheduled_deploy.py')
        source = ready.load_reviewed('install_stopped_audit.py')
        user = pwd.getpwnam('ubuntu')
        require((user.pw_uid, user.pw_gid) == (1000, 1000), 'observed_engine_identity_changed')
        with ExitStack() as stack:
            for name in source.LOCKS:
                old.maintenance_lock(stack, helper, Path('/run/lock')/(name+'.lock'))
            require(not install.STATE.exists(), 'priority_attempt_already_recorded_review_status')
            rows = plan_sources(ROOT, json.loads((PACKAGE/'manifest.json').read_bytes()),
                                (user.pw_uid, user.pw_gid), stack)
            if args.apply:
                helper.root_parents(Path('/var/lib/kadima-priority-mode-audit'))
                report_path = Path(tempfile.mkdtemp(prefix='kadima-priority-modes-', dir='/var/lib'))
            report = repair_modes(rows, lambda value: helper.record_status(report_path, value), args.apply)
    except Exception as exc:
        report = getattr(exc, 'report', dict(state='source_permission_check_blocked',
            mutation_attempted=False, deployment_performed=False, service_changes_requested=False))
        report['blocked'] = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        if report_path is not None:
            report['status_path'] = str(report_path/'status.json')
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)
    if report_path is not None:
        report['status_path'] = str(report_path/'status.json')
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
