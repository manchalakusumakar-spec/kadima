"""Read-only source compatibility review. Never installs or restarts anything."""
import argparse
import hashlib
import json
from pathlib import Path


def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def review(root, manifest):
    root = Path(root).resolve(strict=True)
    states, blockers = {}, []
    for name, versions in manifest['files'].items():
        path = root / name
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
            blockers.append(name + ':symlink')
            continue
        actual = blob(path.read_bytes()) if path.is_file() else None
        if actual == versions['after']:
            states[name] = 'release'
        elif actual == versions['before']:
            states[name] = 'baseline'
        else:
            blockers.append(name + ':source_mismatch')
    for name, expected in manifest['dependencies'].items():
        path = root / name
        if not path.is_file() or path.is_symlink() or blob(path.read_bytes()) != expected:
            blockers.append(name + ':dependency_mismatch')
    if len(set(states.values())) > 1:
        blockers.append('mixed_release_files')
    status = ('BLOCKED' if blockers else
              'RELEASE_SOURCE_PRESENT' if set(states.values()) == {'release'} else 'BASELINE_SOURCE_COMPATIBLE')
    return dict(status=status, files=states, blockers=blockers,
                runtime_verified=False, broker_verified=False,
                note='Source review only. No files, services, controls or broker state were changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Installed KADIMA repository root')
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('manifest.json').read_text())
    try:
        result = review(args.root, manifest)
    except (OSError, ValueError) as exc:
        result = dict(status='BLOCKED', blockers=[type(exc).__name__], runtime_verified=False)
    print(json.dumps(result, indent=2))
    return 1 if result['status'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
