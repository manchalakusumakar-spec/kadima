#!/usr/bin/env python3
"""Read-only source comparison. Not an installer, broker-flat check or activation.

Run from the pinned review checkout, never import the installed application.
Unknown/mixed source and symlinks fail closed. No service or database access.
"""
import argparse
import hashlib
import json
from pathlib import Path
import stat

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    if path.is_symlink():
        raise ValueError('symlink_refused')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('symlink_parent_refused')
    try:
        info=path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size>2_000_000:
        raise ValueError('nonregular_or_oversize_source')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(live_root=None):
    manifest=json.loads((ROOT/'docs/cas_release_manifest.json').read_text())
    results=[];blocked=[];states=set()
    for name,item in manifest['files'].items():
        p=Path(name)
        if p.is_absolute() or '..' in p.parts:
            raise ValueError('unsafe_manifest_path')
        if digest(ROOT/p)!=item['after']:
            blocked.append({'path':name,'reason':'review_checkout_changed'})
    if live_root is not None:
        target=Path(live_root).absolute()
        if target==ROOT or not target.is_dir():
            raise ValueError('separate_existing_live_root_required')
        for name,item in manifest['files'].items():
            if not name.startswith('kadima/') and name!='run.py':
                continue
            try:
                h=digest(target/name)
                state=('shared' if item['before']==item['after']==h else
                       'before' if h==item['before'] else 'after' if h==item['after'] else 'unknown')
                if state=='unknown':blocked.append({'path':name,'reason':'unreviewed_live_source'})
                else:states.add(state)
                results.append({'path':name,'state':state})
            except ValueError as exc:
                blocked.append({'path':name,'reason':str(exc)})
        expected={n for n in manifest['files'] if n.startswith('kadima/')}
        extras={str(p.relative_to(target)) for p in (target/'kadima').rglob('*.py')}-expected
        for name in sorted(extras):blocked.append({'path':name,'reason':'unreviewed_additional_runtime_module'})
        if {'before','after'}<=states:
            blocked.append({'reason':'mixed_runtime_source'})
    return dict(ok=not blocked,scope='source_comparison_only',baseline=manifest['baseline'],
                broker_checked=False,services_changed=False,activation_changed=False,
                deployment_authorized=False,blocked=blocked,files=results)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-root')
    args=parser.parse_args()
    try:
        result=check(args.live_root)
    except Exception as exc:
        result=dict(ok=False,scope='source_comparison_only',reason=type(exc).__name__)
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['ok'] else 1)
