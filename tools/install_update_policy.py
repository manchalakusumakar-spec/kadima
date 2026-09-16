"""Install 24/7 deployment tools only; no broker access or service restarts.

Run from a pinned release: sudo -n python3 tools/install_update_policy.py.
The running application, trading configuration and database are not modified.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import tempfile

PACKAGE = Path(__file__).resolve().parents[1]
REPO = Path('/home/ubuntu/kadima-py')


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def install(package, repo, uid, gid):
    allowed = json.loads((package / 'tools/update_policy_manifest.json').read_text())
    changes = []
    if repo.is_symlink() or not repo.is_dir():
        raise ValueError('Expected an existing regular checkout directory')
    for name, hashes in allowed.items():
        target, source = repo / name, package / name
        if target.parent.is_symlink() or target.is_symlink():
            raise ValueError('Unexpected symlink: ' + name)
        data = source.read_bytes()
        compile(data, str(source), 'exec')
        current = target.read_bytes() if target.exists() else None
        if current == data:
            continue
        if current is not None and blob(current) not in hashes:
            raise ValueError('Unreviewed installer version: ' + name)
        changes.append((name, data, current))
    # A checkout that never contained deployment tools also needs their manifests.
    # Existing manifests remain unchanged; version checks still apply on deployment.
    for name in allowed:
        relative = str(Path(name).with_name('manifest.json'))
        target = repo / relative
        if target.is_symlink():
            raise ValueError('Unexpected symlink: ' + relative)
        if not target.exists():
            data = (package / relative).read_bytes()
            json.loads(data)
            changes.append((relative, data, None))
    if not changes:
        return None
    backup = Path(tempfile.mkdtemp(prefix='kadima-update-policy-backup-', dir=repo.parent))
    written = []
    try:
        for name, data, previous in changes:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if previous is not None:
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                staged = Path(tmp.name)
                tmp.write(data)
            try:
                os.chown(staged, uid, gid)
                os.chmod(staged, 0o644)
                os.replace(staged, target)
                written.append((name, previous))
            finally:
                staged.unlink(missing_ok=True)
    except Exception:
        for name, previous in reversed(written):
            target = repo / name
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                shutil.copy2(backup / name, target)
        raise
    return backup


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo -n python3 ' + str(Path(__file__).resolve()))
    user = pwd.getpwnam('ubuntu')
    backup = install(PACKAGE, REPO, user.pw_uid, user.pw_gid)
    print('24/7 update policy installed.' if backup else '24/7 update policy already installed.')
    print('No services restarted; no broker requests; no trading flags changed.')
    print('Future engine deployments still require flat positions and no pending orders.')
    if backup:
        print('Backup:', backup)


if __name__ == '__main__':
    main()
