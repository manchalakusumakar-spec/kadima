"""Separate additive, disabled staging installer; never operates trading services.

Stage authorization is explicit. Enabling requires a separately reviewed native
cache handoff and broker reconciler; this host lacks both, so enable is refused.
No trading/forecast database is opened and no original source file is replaced.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone
from release_plan import plan
from inspect_install import inspect


def preflight(root, sha, live_root, expected):
    release = plan(root, sha)
    identity = inspect(live_root)
    if identity != expected:
        raise ValueError('installed source changed since reviewed preflight')
    return {'release_sha':release['release_sha'], 'installed_source':identity,
        'mode':'STAGED_DISABLED', 'service_operations':[],
        'dependencies':['isolated numpy==2.3.5; production dependencies untouched'],
        'enable_blockers':['loaded Python source identity unestablished',
            'no source-matched broker position/order reconciler',
            'no verified native cache handoff/writer lock',
            'no dated live NIFTY50 radar manifest and coverage'],
        'forecast_replacement':False, 'trading_changes':False}


def stage(root, sha, live_root, expected, destination, *, approved=False,
          reconciler=None, deployment_check=None):
    receipt=preflight(root,sha,live_root,expected)
    if not approved:
        raise ValueError('specific production staging approval required')
    # Callables must be source-matched local read-only implementations, never
    # operator-supplied claims or cached positions from the trading database.
    if reconciler is None or deployment_check is None:
        raise ValueError('fresh broker reconciliation and competing deployment checks unavailable')
    now=datetime.now(timezone.utc)
    broker=reconciler()
    at=datetime.fromisoformat(broker['received_at'])
    if at.tzinfo is None or not 0 <= (now-at).total_seconds() <= 10:
        raise ValueError('broker reconciliation is not fresh')
    if broker.get('complete') is not True or broker.get('positions') != [] or broker.get('pending_orders') != []:
        raise ValueError('broker is not reconciled flat with no pending orders')
    if deployment_check() is not True:
        raise ValueError('competing deployment exclusion not established')
    preflight(root,sha,live_root,expected)
    dest=Path(destination)
    if dest.exists() or not dest.name.endswith(sha):
        raise ValueError('require new release-specific destination ending in exact SHA')
    # Stage only this package. There is no import/bootstrap into the engine.
    source=Path(root)/'forecast_precision'
    tracked=subprocess.check_output(['git','-C',str(root),'ls-tree','-r','--name-only',sha,'--','forecast_precision/'],text=True).splitlines()
    dest.mkdir(parents=True,exist_ok=False)
    try:
        for relative in tracked:
            content=subprocess.check_output(['git','-C',str(root),'show',sha+':'+relative])
            target=dest/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
        (dest/'DISABLED').write_text('No runtime activation or trading authority\n')
        (dest/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        for relative in tracked:
            file=Path(root)/relative
            if hashlib.sha256(file.read_bytes()).digest()!=hashlib.sha256((dest/relative).read_bytes()).digest():
                raise ValueError('staged source hash mismatch')
    except Exception:
        (dest/'DISABLED').write_text('Staging failed; activation forbidden\n')
        raise
    return receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='.')
    p.add_argument('--release-sha',required=True)
    p.add_argument('--live-root',required=True)
    p.add_argument('--expected-identity',required=True)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--enable',action='store_true')
    a=p.parse_args()
    if a.apply or a.enable:
        p.error('BLOCKED: source-matched reconciliation/cache handoff unavailable; no production write')
    print(json.dumps(preflight(a.root,a.release_sha,a.live_root,
          json.loads(Path(a.expected_identity).read_text())),indent=2))

if __name__=='__main__': main()
