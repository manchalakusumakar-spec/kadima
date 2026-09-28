"""Plan by default; atomically update only the reviewed Celestial HTML file."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

ROOT = Path(__file__).resolve().parent
PAGE = Path('/var/www/html/dashboard.html')
APPROVED = frozenset(('579bb9131f4265c8d67171ca38b3df4803fdc837',
                      '3816a160f46420aaf28cb7f8de8579fdd3cf31e5'))
ASSETS = {'layout.css': 'fe7b8f18ccb34719b20bb965b7f2c1401dccfc5351c215f0d71d492476370bda',
          'layout.js': 'd299afe714120e2d41e13564ec410a385bf4f659be08c5a9c9c0e26e34247e1e'}
START = b'<!-- BEGIN CELESTIAL LAYOUT -->'
END = b'<!-- END CELESTIAL LAYOUT -->'


class Blocked(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def blob(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def canonical(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'symlink_or_noncanonical_path')
    return path


def fingerprint(info):
    return tuple(getattr(info, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns',
                 'st_ctime_ns', 'st_mode', 'st_uid', 'st_gid', 'st_nlink'))


def read(path):
    path = canonical(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'single_regular_file_required')
        require(not before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o002), 'unsafe_file_mode')
        raw = stream.read(2_000_001)
        require(len(raw) <= 2_000_000, 'page_or_asset_too_large')
        require(fingerprint(before) == fingerprint(os.fstat(stream.fileno())) ==
                fingerprint(path.lstat()), 'file_changed_during_read')
    return raw, before


def build(raw, assets=ROOT):
    parts = {}
    for name, expected in ASSETS.items():
        content, _ = read(assets/name)
        require(hashlib.sha256(content).hexdigest() == expected, 'layout_asset_changed')
        parts[name] = content
    block = START+b'\n<style>'+parts['layout.css']+b'</style>\n<script>'+parts['layout.js']+b'</script>\n'+END+b'\n'
    if START in raw or END in raw:
        require(raw.count(block) == raw.count(START) == raw.count(END) == 1,
                'existing_layout_requires_review')
        original = raw.replace(block, b'', 1)
        require(blob(original) in APPROVED and raw == original.replace(b'</body>', block+b'</body>'),
                'installed_page_source_changed')
        return raw
    require(blob(raw) in APPROVED, 'unreviewed_page_preserved')
    require(raw.count(b'</body>') == 1 and raw.count(b'id="desk"') == 1, 'unsupported_page_structure')
    return raw.replace(b'</body>', block+b'</body>')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def install(page=PAGE, assets=ROOT, backups=Path('/var/backups'), apply=False):
    page = canonical(page)
    original, info = read(page)
    updated = build(original, assets)
    if original == updated:
        print('ALREADY INSTALLED: the reviewed layout is present; no file changed.')
        return None
    print('PLAN: add the layout editor to the reviewed webpage; preserve existing page content and ownership.')
    if not apply:
        print('PLAN ONLY: no page, service or trading changes.')
        return None
    backups = canonical(backups)
    backup = Path(tempfile.mkdtemp(prefix='kadima-layout-', dir=backups))
    private_write(backup/'dashboard.before.html', original)
    private_write(backup/'receipt.json', json.dumps({
        'created_at':datetime.now(timezone.utc).isoformat(), 'page':str(page),
        'before_blob':blob(original), 'after_blob':blob(updated),
        'uid':info.st_uid, 'gid':info.st_gid, 'mode':stat.S_IMODE(info.st_mode),
    }, sort_keys=True).encode())
    sync_dir(backup); sync_dir(backups)
    print('BACKUP:', backup, flush=True)
    fd, temporary = tempfile.mkstemp(prefix='.kadima-layout-', suffix='.html', dir=page.parent)
    replaced = False
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated); stream.flush()
            current = os.fstat(stream.fileno())
            if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode)); os.fsync(stream.fileno())
        current, current_info = read(page)
        require(current == original and fingerprint(current_info) == fingerprint(info),
                'page_changed_before_replace')
        require(build(original, assets) == updated, 'layout_assets_changed_before_replace')
        os.replace(temporary, page); replaced = True; sync_dir(page.parent)
        actual, metadata = read(page)
        require(actual == updated and (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) ==
                (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), 'post_install_verification_failed')
        private_write(backup/'installed.json', json.dumps({'state':'installed', 'after_blob':blob(actual)}).encode())
        sync_dir(backup)
        print('INSTALLED: HTML bytes and ownership verified. Refresh Celestial, then select Edit layout.')
        print('No service restart, trading activation, broker request or database change was performed.')
        return backup
    except BaseException:
        print('HTML REPLACED; verification incomplete. Preserve the backup and inspect before retrying.'
              if replaced else 'NOT INSTALLED: original webpage retained.')
        raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'sudo_required')
    with ExitStack() as stack:
        lock_dir = canonical(Path('/run/lock'))
        meta = lock_dir.stat()
        require(meta.st_uid == 0 and (not meta.st_mode & 0o022 or stat.S_IMODE(meta.st_mode) == 0o1777),
                'unsafe_lock_directory')
        for name in ('kadima-stock-fast-install', 'kadima-activation', 'kadima-layout-install'):
            path = lock_dir/(name+'.lock')
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            stream = stack.enter_context(os.fdopen(fd, 'r+b'))
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                    and not before.st_mode & 0o022, 'unsafe_install_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require((path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino), 'install_lock_changed')
        install(apply=args.apply)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:', str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        raise SystemExit(1)
