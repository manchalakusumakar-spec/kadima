"""Install only the tick price validation; service lifecycle remains operator-owned."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


BASELINE_SHA256 = '7f75be3200199a347f1d634573a1cded3e7787f71dee508acbd8d13474db550e'
SOURCE = Path(__file__).resolve().parents[1] / 'kadima' / 'feed.py'


def install(root, source=SOURCE, apply=False):
    target = Path(root).resolve() / 'kadima' / 'feed.py'
    if target.is_symlink():
        raise ValueError('Refusing a symlink target; inspect the installed source path.')
    old = target.read_bytes()
    new = Path(source).read_bytes()
    compile(new, str(target), 'exec')
    if old == new:
        return 'Already installed; no files changed.'
    if hashlib.sha256(old).hexdigest() != BASELINE_SHA256:
        raise ValueError('Installed feed.py differs from the reviewed baseline; '
                         'no files changed. SHA256=' + hashlib.sha256(old).hexdigest())
    if not apply:
        return 'Compatible: only kadima/feed.py will change. Pass --apply to install.'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = target.with_name(target.name + '.tick-backup-' + stamp)
    with backup.open('xb') as out:
        out.write(old)
    shutil.copystat(target, backup)
    info = target.stat()
    fd, temp_path = tempfile.mkstemp(prefix='.tick-input-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp_path, info.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(temp_path, info.st_uid, info.st_gid)
            os.chown(backup, info.st_uid, info.st_gid)
        # Refuse to overwrite a concurrent source update after the preflight.
        if target.read_bytes() != old:
            raise ValueError('Installed source changed during preflight; refusing replacement.')
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return ('Installed tick price validation. Backup: ' + str(backup) + '\n'
            'Running processes still use their loaded version. Reload the KADIMA engine '
            'in the operator maintenance window. No services or trading settings were changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True,
                        help='Installed KADIMA checkout, for example /home/ubuntu/kadima-py')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(install(args.root, apply=args.apply))
    except (OSError, ValueError, SyntaxError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
