#!/usr/bin/env python3
"""Report all static source mismatches without importing or changing live code.

Run from a separate reviewed checkout with python3 -I -B -S. No broker calls,
API requests, subprocesses, service changes, installer invocation or allowlist
writes. Output contains only release-relative paths, state labels and hashes;
source text, credentials and exception messages are never emitted.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import warnings

FILES = {
    'run.py': 'b27d03215ea7ba7e4fe6e73b1052da208beea8d2',
    'kadima/full_universe_scanner.py': 'aca08c5c6af10e7fffbbb316eea3d039aeb41731',
    'kadima/stock_fast.py': None,
    'kadima/stock_fast_api.py': None,
}
FIFO = '9886d951be6997e0ddf8fdda640529f5f751a25c'
MAX_BYTES = 2_000_000
MAX_MODULES = 512
MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z')


def digest(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def read_source(path):
    """Refuse symlinks/nonregular files and bound reads. No source execution."""
    path = Path(path)
    try:
        if not path.is_absolute() or path.resolve(strict=False) != path:
            return None, 'SYMLINK_OR_NONCANONICAL'
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None, 'NOT_REGULAR'
            raw = handle.read(MAX_BYTES + 1)
        return (None, 'OVERSIZE') if len(raw) > MAX_BYTES else (raw, 'PRESENT')
    except FileNotFoundError:
        return None, 'MISSING'
    except (OSError, ValueError, RuntimeError):
        return None, 'UNREADABLE'


def syntax_tree(raw):
    if raw is None:
        return None
    try:
        # Warnings can quote unreviewed source lines; keep them private too.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse(raw)
            # Compilation checks syntax only; it never executes the code object.
            compile(tree, '<source-review>', 'exec')
        return tree
    except (SyntaxError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def local_modules(tree, package):
    """Follow the installer's static local-import surface; dynamic imports excluded."""
    found = set()
    if tree is None:
        return found
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                names = [node.module] if node.module else [a.name for a in node.names]
            elif node.module == 'kadima':
                names = [a.name for a in node.names]
            elif node.module and node.module.startswith('kadima.'):
                names = [node.module[7:]]
        elif isinstance(node, ast.Import):
            names = [a.name[7:] for a in node.names if a.name.startswith('kadima.')]
        for name in names:
            if not MODULE.fullmatch(name):
                continue
            relative = 'kadima/' + name.replace('.', '/') + '.py'
            path = package / relative
            if path.is_file() or path.is_symlink():
                found.add(relative)
    return found


def compare(package, target):
    package, target = Path(package).absolute(), Path(target).absolute()
    if package.resolve() == target.resolve():
        raise ValueError('SEPARATE_RELEASE_DIRECTORY_REQUIRED')
    todo = set(FILES) | {'kadima/rate_limiter.py'}
    if (package / 'kadima/__init__.py').exists():
        todo.add('kadima/__init__.py')
    visited, rows = set(), []
    while todo:
        name = min(todo)
        todo.remove(name)
        if name in visited:
            continue
        if len(visited) >= MAX_MODULES:
            raise ValueError('STATIC_MODULE_LIMIT_EXCEEDED')
        visited.add(name)
        release, rs = read_source(package / name)
        installed, ins = read_source(target / name)
        rt, it = syntax_tree(release), syntax_tree(installed)
        todo.update(local_modules(rt, package) - visited)
        # An installed change may import further shipped modules; report those too.
        todo.update(local_modules(it, package) - visited)
        expected = digest(release) if release is not None else None
        actual = digest(installed) if installed is not None else None
        reasons = []
        if rs != 'PRESENT':
            reasons.append('RELEASE_' + rs)
        elif rt is None:
            reasons.append('RELEASE_SYNTAX_INVALID')
        if ins != 'PRESENT' and not (name in FILES and FILES[name] is None and ins == 'MISSING'):
            reasons.append('INSTALLED_' + ins)
        elif installed is not None and it is None:
            reasons.append('INSTALLED_SYNTAX_INVALID')
        if name == 'kadima/config.py':
            state = 'DEPLOYMENT_CONFIG_PRESERVED'
            # Do not print config contents or config fingerprints.
            actual = expected = None
        elif name == 'kadima/rate_limiter.py':
            state = 'FIFO_PRESERVED' if actual == expected == FIFO else 'REVIEW_REQUIRED'
            if state == 'REVIEW_REQUIRED':
                reasons.append('FIFO_HASH_MISMATCH')
        elif installed is not None and installed == release:
            state = 'MATCH'
        elif name in FILES and ins in ('MISSING', 'PRESENT') and actual == FILES[name]:
            state = 'PLANNED_ADD' if installed is None else 'KNOWN_BASE_UPGRADE'
        else:
            state = 'REVIEW_REQUIRED'
            if installed is not None and release is not None and installed != release:
                reasons.append('UNREVIEWED_TARGET' if name in FILES else 'UNREVIEWED_DEPENDENCY')
        if reasons:
            state = 'REVIEW_REQUIRED'
        rows.append(dict(path=name, state=state, installed_blob=actual,
            release_blob=expected, reasons=reasons,
            same_syntax_tree=(ast.dump(rt, include_attributes=False) == ast.dump(it, include_attributes=False))
            if rt is not None and it is not None and name != 'kadima/config.py' else None))
    rows.sort(key=lambda row: row['path'])
    return dict(static_modules_checked=len(rows),
                review_required=[r for r in rows if r['state'] == 'REVIEW_REQUIRED'],
                planned_updates=[r['path'] for r in rows if r['state'] in ('PLANNED_ADD', 'KNOWN_BASE_UPGRADE')],
                all_observations=rows, live_files_changed=False, broker_called=False,
                verdict='SOURCE_INVENTORY_ONLY_NOT_INSTALL_APPROVAL',
                limitation='Static shipped-module graph only; no in-memory, runtime, broker or gateway validation.')


def summary(result):
    lines = ['STATIC MODULES CHECKED: ' + str(result['static_modules_checked']),
             'PLANNED STOCK FILE UPDATES: ' + str(len(result['planned_updates'])),
             'REVIEW REQUIRED: ' + str(len(result['review_required']))]
    for row in result['review_required']:
        lines.extend(['FILE: ' + row['path'],
                      'AWS: ' + (row['installed_blob'] or 'UNAVAILABLE'),
                      'PR:  ' + (row['release_blob'] or 'UNAVAILABLE'),
                      'WHY: ' + ','.join(row['reasons'])])
    lines.append('NO INSTALL / NO RESTART / NO BROKER CALL')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('/home/ubuntu/kadima-py'))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    result = compare(Path(__file__).resolve().parents[1], args.target)
    print(json.dumps(result, indent=2) if args.json else summary(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # SyntaxError and OS messages may contain source text or arbitrary paths.
        print('REVIEW UNAVAILABLE: cannot complete source inventory; no installation performed.')
        sys.exit(1)
