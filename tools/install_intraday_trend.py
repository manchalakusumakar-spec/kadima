"""Observation-only proposal. All anchors validated before any possible mutation.

This review build is deliberately dry-run-only: the task requires a trader-approved
alternate baseline and the pinned source does not implement the claimed strike priority.
"""
import argparse,difflib,hashlib,json
from pathlib import Path

PIN='8e8fb53873fdb7b13a46081c19a9b17e7b8a62b4e397ca29c44c57ebd29f8a46'
def digest(b): return hashlib.sha256(b).hexdigest()
def plan(root,source,spec):
    lane=source.read_bytes()
    if digest(lane)!=PIN:raise ValueError('pinned lane hash mismatch')
    proposed={}
    for name,p in spec.items():
        path=root/'kadima'/name;before=path.read_bytes();old=before.decode()
        if digest(before)==p['after_sha256']:continue
        if digest(before)!=p['before_sha256']:raise ValueError(name+': live file differs from reviewed identity')
        new=old
        for patch in p['patches']:
            if new.count(patch['old'])!=1:raise ValueError(name+': ambiguous/missing anchor '+patch['label'])
            new=new.replace(patch['old'],patch['new'],1)
        compile(new,str(path),'exec')
        if digest(new.encode())!=p['after_sha256']:raise ValueError(name+': post-plan hash mismatch')
        proposed[path]=(before,new.encode())
    path=root/'kadima/intraday_trend.py'
    if path.exists() and path.read_bytes()!=lane:raise ValueError('existing intraday module differs')
    if not path.exists():proposed[path]=(b'',lane)
    return proposed

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--source',type=Path,required=True);ap.add_argument('--dry-run',action='store_true')
    a=ap.parse_args();spec=json.loads(Path(__file__).with_name('intraday_anchor_plan.json').read_text())
    changes=plan(a.root,a.source,spec)
    # Print every diff only once all anchors/hash checks have passed.
    for p,(old,new) in changes.items():
        print(''.join(difflib.unified_diff(old.decode().splitlines(True),new.decode().splitlines(True),
              fromfile='before/'+str(p.relative_to(a.root)),tofile='after/'+str(p.relative_to(a.root)))),end='')
    if not a.dry_run:raise SystemExit('INSTALL BLOCKED: trader approval of alternate ledger baseline and source/strike-priority reconciliation required. No files changed.')
if __name__=='__main__':main()
