"""Hold exactly one reviewed Ubuntu watchdog cron job, preserving every byte.

The caller owns maintenance locks and verifies the application is stopped.
Only crontab listing/installing and private backup writes are permitted here.
No service, broker, database, repair, or automatic restoration action exists.
The crontab CLI has no atomic compare-and-swap operation. Before/after reads
detect observable changes; an external edit during installation can be lost.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


STATE = Path('/var/lib/kadima-stopped-readiness-20260928')
USER = 'ubuntu'
LINE = b'55 9 * * 1-5 systemctl start kadima-eod-watchdog'
LINE_SHA256 = 'b0814a2c4c0ffcf904743c6232b20313bd471ba3d2c91afef30c75bfa2d20847'
MARKER = b'# KADIMA_MAINTENANCE_HOLD_20260928 '
ORIGINAL = 'watchdog-crontab.original'
HELD = 'watchdog-crontab.held'
MANIFEST = 'watchdog-crontab.manifest.json'
LIMIT = 250_000


class Blocked(RuntimeError):
    def __init__(self, reason, receipt=None):
        super().__init__(reason)
        self.hold_receipt = receipt or dict(state='not_changed', held=False,
                                           mutation_attempted=False, atomic_compare_and_swap=False)


def require(value, reason):
    if not value:
        raise Blocked(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def invoke(command, **kwargs):
    return subprocess.run(command, capture_output=True, timeout=15,
                          env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, **kwargs)


def read_cron(invoke):
    result = invoke(['/usr/bin/crontab', '-u', USER, '-l'])
    require(result.returncode == 0 and isinstance(result.stdout, bytes) and
            0 < len(result.stdout) <= LIMIT, 'reviewed_crontab_unavailable')
    return result.stdout


def compose(original):
    require(isinstance(original, bytes) and 0 < len(original) <= LIMIT,
            'reviewed_crontab_unavailable')
    require(sha(LINE) == LINE_SHA256, 'reviewed_cron_definition_changed')
    # Split only on LF: preserve CRLF, final newline and all other bytes exactly.
    lines = original.split(b'\n')
    matches = [index for index, raw in enumerate(lines)
               if (raw[:-1] if raw.endswith(b'\r') else raw) == LINE]
    require(len(matches) == 1, 'exactly_one_reviewed_watchdog_line_required')
    require(not any(MARKER in raw for raw in lines), 'existing_hold_marker_requires_review')
    index = matches[0]
    lines[index] = MARKER + lines[index]
    return b'\n'.join(lines), index + 1


def manifest_for(original, held, line_number):
    return dict(schema=1, user=USER, reviewed_line_sha256=LINE_SHA256,
                marker=MARKER.decode('ascii'), original_sha256=sha(original),
                held_sha256=sha(held), line=line_number)


def receipt_for(state_dir, manifest, *, state='held_verified', held=True,
                mutation_attempted=False, already_held=False):
    return dict(state=state, held=held, mutation_attempted=mutation_attempted,
                already_held=already_held, user=USER, atomic_compare_and_swap=False,
                backup_path=str(state_dir/ORIGINAL),
                reviewed_line_sha256=LINE_SHA256,
                original_sha256=manifest['original_sha256'], held_sha256=manifest['held_sha256'],
                line=manifest['line'], schedule=dict(minute='55', hour='9', day_of_month='*',
                                                   month='*', day_of_week='1-5'),
                schedule_timezone='not_inferred', command_kind='systemctl_start',
                unit='kadima-eod-watchdog.service')


def directory(helper, state_dir, *, create=False):
    state_dir = Path(state_dir)
    helper.root_parents(state_dir)
    if create:
        state_dir.mkdir(mode=0o700, exist_ok=True)
    helper.private_path(state_dir, directory=True)
    return state_dir


def read_backup(helper, state_dir):
    original, held, encoded = [helper.read_regular(helper.private_path(state_dir/name), LIMIT)
                               for name in (ORIGINAL, HELD, MANIFEST)]
    expected, line_number = compose(original)
    require(held == expected, 'held_crontab_backup_changed')
    manifest = json.loads(encoded)
    require(manifest == manifest_for(original, held, line_number), 'watchdog_hold_manifest_changed')
    return held, manifest


def verify_held_cron(helper, *, invoke=invoke, state_dir=STATE):
    """Read-only guard; a changed cron never triggers an overwrite or rollback."""
    receipt = dict(state='hold_unverified', held=False, mutation_attempted=False,
                   atomic_compare_and_swap=False)
    try:
        require(os.geteuid() == 0, 'sudo_required')
        state_dir = directory(helper, state_dir)
        held, manifest = read_backup(helper, state_dir)
        receipt = receipt_for(state_dir, manifest, state='hold_unverified', held=False,
                              already_held=True)
        require(read_cron(invoke) == held, 'held_crontab_changed_requires_review')
        return receipt_for(state_dir, manifest, already_held=True)
    except Exception as exc:
        raise Blocked(str(exc) if isinstance(exc, Blocked) else 'watchdog_hold_verification_failed',
                      receipt) from None


def hold_reviewed_cron(helper, *, invoke=invoke, state_dir=STATE):
    """One reversible hold; the original private backup is never overwritten."""
    receipt = dict(state='not_changed', held=False, mutation_attempted=False,
                   atomic_compare_and_swap=False)
    try:
        require(os.geteuid() == 0, 'sudo_required')
        state_dir = directory(helper, state_dir, create=True)
        existing = [(state_dir/name).exists() or (state_dir/name).is_symlink()
                    for name in (ORIGINAL, HELD, MANIFEST)]
        if any(existing):
            require(all(existing), 'partial_watchdog_hold_record_requires_review')
            return verify_held_cron(helper, invoke=invoke, state_dir=state_dir)
        original = read_cron(invoke)
        held, line_number = compose(original)
        manifest = manifest_for(original, held, line_number)
        helper.create_private(state_dir/ORIGINAL, original)
        helper.create_private(state_dir/HELD, held)
        helper.create_private(state_dir/MANIFEST, json.dumps(manifest, sort_keys=True).encode())
        verified, verified_manifest = read_backup(helper, state_dir)
        require(verified == held and verified_manifest == manifest, 'watchdog_hold_backup_unverified')
        receipt = receipt_for(state_dir, manifest, state='prepared_no_install', held=False)
        require(read_cron(invoke) == original, 'crontab_changed_before_hold')
        receipt.update(state='hold_unverified', mutation_attempted=True)
        command_ok = False
        try:
            result = invoke(['/usr/bin/crontab', '-u', USER, '-'], input=held)
            command_ok = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            receipt['held'] = read_cron(invoke) == held
        except (OSError, subprocess.TimeoutExpired, Blocked):
            receipt['held'] = False
        if receipt['held']:
            receipt['state'] = 'held_verified'
        require(command_ok, 'crontab_hold_command_failed_review_receipt')
        require(receipt['held'], 'crontab_hold_not_verified_no_restore_attempted')
        return receipt
    except Exception as exc:
        if isinstance(exc, Blocked) and exc.hold_receipt.get('already_held'):
            raise
        raise Blocked(str(exc) if isinstance(exc, Blocked) else 'watchdog_hold_failed', receipt) from None
