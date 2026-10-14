#!/usr/bin/env python3
"""Read-only N50 compatibility receipt. Never installs, restarts or changes trading."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

SOURCE = 'a715e3c6e9ae7821cfab7ffc182a30834f63546b'
BASE = 'c53365deddcca84ffee4484ef8246c14dc6cc450'
PATHS = (
    'kadima/engine.py', 'kadima/feed.py', 'kadima/api.py',
    'kadima/forecast_observer.py', 'kadima/checklist_forecast.py',
    'kadima/constituent_radar.py', 'kadima/rest_day_forecast.py',
)


def command(args, cwd):
    p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=15)
    if p.returncode:
        raise RuntimeError('command_failed')
    return p.stdout


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def classify(installed, base, candidate):
    if installed is None:
        return 'missing'
    if installed == candidate:
        return 'candidate_matches_disk'
    if base is not None and installed == base:
        return 'base_matches_disk'
    return 'installed_overlay_differs'


def inspect(repo):
    root = Path(command(['git', 'rev-parse', '--show-toplevel'], repo).decode().strip())
    if root.resolve() != repo.resolve():
        raise ValueError('repository_root_required')
    # Require exact reviewed commit locally; never fall back to latest main.
    command(['git', 'cat-file', '-e', SOURCE+'^{commit}'], repo)
    records = []
    for path in PATHS:
        candidate = command(['git', 'show', SOURCE+':'+path],repo)
        try:
            base = command(['git', 'show', BASE+':'+path],repo)
        except RuntimeError:
            base = None
        local = repo/path
        installed = local.read_bytes() if local.is_file() and not local.is_symlink() else None
        records.append(dict(path=path, status=classify(installed,base,candidate),
            installed_sha256=digest(installed) if installed is not None else None,
            candidate_sha256=digest(candidate)))
    try:
        raw = command(['systemctl','show','kadima.service','-p','ActiveState','-p','SubState','-p','MainPID'],repo)
        service = dict(line.split('=',1) for line in raw.decode().splitlines() if '=' in line)
        service = {k:v for k,v in service.items() if k in ('ActiveState','SubState','MainPID')}
    except (RuntimeError,FileNotFoundError,subprocess.TimeoutExpired):
        service = {'status':'unavailable'}
    memory = {}
    if Path('/proc/meminfo').is_file():
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':',1)
            if key in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):
                memory[key+'_KiB'] = int(value.split()[0])
    candidate_matches = all(r['status']=='candidate_matches_disk' for r in records)
    return dict(checked_at=datetime.now(timezone.utc).isoformat(), reviewed_source=SOURCE,
        checkout_head=command(['git','rev-parse','HEAD'],repo).decode().strip(),
        files=records, service=service,memory=memory,
        source_status='candidate_files_match_disk' if candidate_matches else 'source_integration_required',
        loaded_source_verified=False, radar_activation_verified=False,
        weights_and_feed_verified=False, broker_reconciliation_verified=False,
        deployment_performed=False,
        next_step='Verify loaded-source identity, current all-50 weights/feed and safe restart before activation.'
            if candidate_matches else 'Prepare a reviewed adapter against these installed file hashes; do not overwrite overlays.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    args=parser.parse_args()
    try:
        report=inspect(args.repo)
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
        # Never print command stderr, environment, URLs, config or secret values.
        print(json.dumps(dict(status='check_failed',reason='repository_or_reviewed_source_unavailable',deployment_performed=False)))
        return 1
    print(json.dumps(report,indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
