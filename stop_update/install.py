"""Source-matched, disabled stop-adapter installation on the observed MCX host.

Reuses the reviewed flat-account installer. Plan-only unless --broker-check
and --apply are both supplied. Never enables trading or the stop rollout.
"""
import argparse
import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = '\n# KADIMA SOURCE-MATCHED STOP SETTINGS v1\n'


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def config_patch(raw, additions, expected):
    """Preserve all installed settings; only append reviewed new names."""
    if MARKER.encode() in raw:
        original, appended = raw.split(MARKER.encode(), 1)
        if digest(original) != expected or appended != additions:
            raise ValueError('unreviewed_stop_configuration')
        return raw
    if digest(raw) != expected:
        raise ValueError('installed_config_hash_mismatch')
    old_tree, new_tree = ast.parse(raw), ast.parse(additions)
    names = {n.id for n in ast.walk(old_tree) if isinstance(n, ast.Name)
             and isinstance(n.ctx, ast.Store)}
    added = {n.id for n in ast.walk(new_tree) if isinstance(n, ast.Name)
             and isinstance(n.ctx, ast.Store)}
    if names & added or any(not isinstance(n, ast.Assign) for n in new_tree.body):
        raise ValueError('configuration_addition_conflict')
    result = raw + MARKER.encode() + additions
    compile(result, 'installed_config.py', 'exec')
    return result


def configure(target, applying=False):
    manifest = json.loads((ROOT/'stop_update/manifest.json').read_text())
    helper_path = ROOT/'stock_fast_update/install.py'
    if digest(helper_path.read_bytes()) != manifest['installer_helper']:
        raise ValueError('installer_helper_hash_mismatch')
    spec = importlib.util.spec_from_file_location('_stop_install_helper', helper_path)
    base = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)
    original_read = base.read_file
    additions = original_read(ROOT/'stop_update/config_additions.py')
    if digest(additions) != manifest['config_additions']:
        raise base.Blocked('settings_payload_hash_mismatch')
    installed_config = original_read(target/'kadima/config.py')
    new_config = config_patch(installed_config, additions,
                              manifest['installed']['kadima/config.py'])
    for name, expected in manifest['incoming'].items():
        path = ROOT/('stop_update/auto_executor.py' if name == 'kadima/auto_executor.py' else name)
        if digest(original_read(path)) != expected:
            raise base.Blocked('release_hash_mismatch:'+name)

    def release_read(path, missing=False):
        path = Path(path)
        if path == ROOT/'kadima/auto_executor.py':
            return original_read(ROOT/'stop_update/auto_executor.py')
        if path == ROOT/'kadima/config.py':
            return new_config
        return original_read(path, missing=missing)

    base.read_file = release_read
    base.FILES = manifest['installed']
    original_process = base.process

    def process(*args):
        result = original_process(*args)
        # Installation is explicitly disabled; refuse a pre-existing enabling flag.
        if result[2].get('KADIMA_REGIME_STOPS_ENABLED', '0') != '0':
            raise base.Blocked('stop_rollout_must_be_disabled_for_install')
        return result

    base.process = process

    def paused_status(status):
        cfg = status.get('config') if isinstance(status, dict) else None
        if not isinstance(cfg, dict) or type(cfg.get('rollout_enabled')) is not bool:
            raise base.Blocked('control_schema_unknown')
        if cfg.get('auto_trade_enabled') is not False:
            raise base.Blocked('pause_auto_trading_before_install')
        if cfg.get('kill_latched') is not False:
            raise base.Blocked('kill_state_requires_operator_review')
        if not isinstance(status.get('positions'), list) or status['positions']:
            raise base.Blocked('local_api_exposure_or_unknown')
        try:
            ts = datetime.fromisoformat(status['ts'])
            if ts.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-ts).total_seconds() <= 30:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise base.Blocked('fresh_control_snapshot_required') from None
        if applying:
            for unit in ('kadima-session-auto.timer', 'kadima-session-auto.service'):
                state = base.unit_state(unit)
                if state.get('ActiveState') != 'inactive' or state.get('MainPID', '0') != '0':
                    raise base.Blocked('pause_session_scheduler_before_install:'+unit)

    base.paused_status = paused_status
    original_transaction = base.install_transaction

    def transaction(changes, units, backup, flat_check, source_check, import_check,
                    post_check, **kwargs):
        # Freeze the same verified configuration used to construct the payload.
        def checked_source():
            source_check()
            if original_read(target/'kadima/config.py') != installed_config:
                raise base.Blocked('configuration_changed_since_plan')
        return original_transaction(changes, units, backup, flat_check, checked_source,
                                    import_check, post_check, **kwargs)

    base.install_transaction = transaction
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('/home/ubuntu/kadima-py'))
    parser.add_argument('--broker-check', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('sudo_required')
    if args.apply and not args.broker_check:
        raise ValueError('apply_requires_broker_check')
    base = configure(args.target.absolute(), args.apply)
    fd = os.open('/run/lock/kadima-stock-fast-install.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.main()
    print('STOP ADAPTER: rollout remains OFF; no production plan producer or validated forecast enabled.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only our allowlisted diagnostic types contain nonsecret authored text.
        if isinstance(exc, ValueError) or type(exc).__name__ == 'Blocked':
            print('BLOCKED:', str(exc))
        else:
            print('BLOCKED:', type(exc).__name__)
        raise SystemExit(1)
