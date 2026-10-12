"""Release preflight only. This revision has NO AWS apply/activation operation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

BASE = '15a05c25497f3cb93b6f5ec61025de6824737ce4'
WORKFLOWS = {'.github/workflows/nifty-precision-checks.yml',
             '.github/workflows/nifty-precision-release-plan.yml'}
BLOCKERS = ['AWS_LOADED_SOURCE_AND_OVERLAYS_UNVERIFIED',
            'CURRENT_DATED_NIFTY50_WEIGHT_MANIFEST_REQUIRED',
            'EXCHANGE_TIMESTAMP_DECODER_AND_CACHE_FRESHNESS_UNVERIFIED',
            'INDEPENDENT_OI_GREEKS_PRODUCER_MAPPING_REQUIRED',
            'SHADOW_SCHEDULER_AND_CHAMPION_SAME_PACKET_BINDING_REQUIRED',
            'FULL_RUNTIME_PARITY_AND_RESOURCE_MEASUREMENTS_REQUIRED',
            'GUARDED_AWS_INSTALLER_AND_TRANSPORT_NOT_YET_CONFIGURED']


def check_sha(sha):
    if not isinstance(sha, str) or re.fullmatch('[0-9a-f]{40}', sha) is None:
        raise ValueError('exact lowercase 40-character commit required')
    return sha


def check_scope(changes):
    for status, path in changes:
        if status != 'A' or not (path.startswith('forecast_precision/') or path in WORKFLOWS):
            raise ValueError('unexpected modification outside additive forecast scope: '+path)
        if '..' in Path(path).parts:
            raise ValueError('unsafe path')
    if not changes:
        raise ValueError('empty release')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def plan(root, release_sha):
    root=Path(root).resolve(); release_sha=check_sha(release_sha)
    if git(root, 'rev-parse', 'HEAD') != release_sha:
        raise ValueError('checked-out source differs from requested commit')
    # Compare commit endpoints, never working-tree contents.
    raw=git(root, 'diff', '--name-status', '--no-renames', BASE, release_sha)
    changes=[line.split('\t',1) for line in raw.splitlines()]
    check_scope(changes)
    if git(root, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('tracked source changed after checkout')
    blobs={path:git(root,'rev-parse',release_sha+':'+path) for _,path in changes}
    return {'state':'PLAN_ONLY_BLOCKED_FOR_AWS_APPLY','base_sha':BASE,'release_sha':release_sha,
            'added_blobs':blobs,'trading_changes':False,'aws_called':False,
            'blockers':BLOCKERS,'apply_supported':False,
            'note':'A passing plan or unit suite is not live deployment or market accuracy.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--release-sha', required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    if args.apply:
        parser.error('AWS apply is not implemented or authorized by this draft')
    print(json.dumps(plan(args.root,args.release_sha),indent=2))

if __name__=='__main__': main()
