"""Read-only readiness of the stopped PR24 installation and reviewed ledger case.

Never repairs a ledger, writes runtime source, changes a service or arms trading.
Broker checking logs in and reads complete position/order books only. It can
observe the operator-managed NIFTY carryforward without calling it account flat.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parents[1]
TARGET = Path('/home/ubuntu/kadima-py')
AUDIT_STATE = Path('/var/lib/kadima-stopped-audit-20260928')
PRIOR_STATE = Path('/var/lib/kadima-stopped-source-20260928')
COMMIT = '8837cd6dc04e16123e94d8b78a3a716f83ed022c'
CONFIG_BLOB = 'fb31412c01019a8875a2c9321d71b9b6c840024f'
UNITS = ('kadima-dashboard.service', 'kadima-universe-scanner.service',
         'kadima.service', 'kadima-macro-watch.service', 'kadima-eod-watchdog.service')
PINS = {
    'scheduled_deploy.py': '019f35decefe72f56cfb600b5321b2c014e48c6a',
    'prepare_maintenance.py': '363fe8b8c2e1dbcafca11f89afb2666c2a8f87f3',
    'install_stopped_audit.py': '04f1a9e6c9c960ca91b4ccf7354b8a4992bd6581',
    'reconcile_contract_note.py': '931fe21de19aa8ef21c46c713836780363fe1a48',
    'stopped_context.py': '9d53fc3c847f4a5b135315e0397d99e10166af14',
    'stopped_broker_read.py': '971e8de30d884ff0b66e745b2a06b8c3fd9c0325',
}


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def blob(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def load_reviewed(name):
    path = PACKAGE/'momentum_recovery'/name
    require(path.resolve() == path and not path.is_symlink(), 'helper_path_unreviewed')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'regular_helper_required')
        raw = stream.read(4_000_001)
    require(len(raw) <= 4_000_000 and blob(raw) == PINS[name], 'reviewed_helper_required')
    spec = importlib.util.spec_from_file_location('_stopped_readiness_'+name.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_maintenance(helper, path):
    helper.root_parents(path)
    helper.private_path(path, directory=True)
    fingerprints = {}
    for name in ('status.json', 'attempted.json'):
        source = path/name
        helper.private_path(source)
        fingerprints[source] = helper.read_regular(source, 100_000)
    status = json.loads(fingerprints[path/'status.json'])
    attempt = json.loads(fingerprints[path/'attempted.json'])
    require(isinstance(status, dict) and isinstance(attempt, dict), 'maintenance_records_invalid')
    require(status.get('state') == 'audit_learning_source_installed_services_stopped' and
            status.get('source_commit') == COMMIT and status.get('source_installed') is True and
            status.get('dashboard_changed') is True and status.get('manual_holding_external') is True and
            status.get('activation') is False and status.get('restart_requested') is False and
            status.get('ledger_repaired') is False and status.get('runtime_health_verified') is False,
            'successful_stopped_audit_install_required')
    units = status.get('stopped_units')
    require(isinstance(units, list) and len(units) == len(UNITS) and set(units) == set(UNITS),
            'maintenance_units_unreviewed')
    try:
        started = datetime.fromisoformat(attempt['started_at'])
        observed = datetime.fromisoformat(status['observed_at'])
        now = helper.utc_now()
        require(started.tzinfo is not None and observed.tzinfo is not None and now.tzinfo is not None
                and started <= observed <= now, 'maintenance_record_chronology_invalid')
    except (KeyError, TypeError, ValueError):
        raise Blocked('maintenance_record_chronology_invalid') from None
    return status, fingerprints


def verify_sources(installer, release, target, ui_root, owner, source):
    base = installer.base
    changes, fingerprints = base.backend_plan(release, target, owner)
    require(not changes, 'installed_audit_sources_incomplete')
    run = target/'run.py'
    raw = base.read_file(run)
    require(blob(raw) == source.RUN_BLOB and blob(base.read_file(release/'run.py')) == source.RUN_BLOB,
            'installed_entrypoint_unreviewed')
    fingerprints[run] = raw
    for name, destination in (('gateway.py', base.GATEWAY), ('dashboard.html', base.PAGE)):
        raw = base.read_file(destination)
        require(blob(raw) == source.UI_REPLACEMENTS[name] and
                blob(base.read_file(ui_root/name)) == source.UI_REPLACEMENTS[name],
                'installed_dashboard_unreviewed')
        fingerprints[destination] = raw
    return fingerprints


def private_evidence(path, helper, owner):
    raw = helper.read_regular(path, 100_000)
    info = path.stat()
    require(info.st_uid in (0, owner) and not stat.S_IMODE(info.st_mode) & 0o077 and
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'private_reviewed_evidence_required')
    return raw


def local_readiness(db, note, evidence):
    require(db.is_absolute() and db.resolve() == db and db.is_file(), 'canonical_existing_db_required')
    metadata = db.stat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
            'regular_unlinked_database_required')
    identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink)
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=3) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        result = {
            'database_identity': identity(metadata),
            'unresolved_positions': connection.execute(
                "SELECT COUNT(*) FROM positions WHERE status IS NULL OR status!='closed'").fetchone()[0],
            'unresolved_orders': connection.execute(
                "SELECT COUNT(*) FROM execution_orders WHERE state IS NULL OR state!='terminal'").fetchone()[0],
        }
        cfg_row = connection.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
        stock_row = connection.execute('SELECT payload FROM stock_fast_controls WHERE id=1').fetchone()
        require(cfg_row is not None and stock_row is not None, 'persisted_controls_required')
        cfg, stock = json.loads(cfg_row[0]), json.loads(stock_row[0])
        require(isinstance(cfg, dict) and isinstance(stock, dict), 'persisted_controls_invalid')
        result['controls'] = dict(auto_trade_enabled=cfg.get('auto_trade_enabled'),
            kill_latched=cfg.get('kill_latched'), stock_live_enabled=stock.get('live_enabled'),
            stock_revision=stock.get('revision'))
        require(all(type(result['controls'][key]) is bool for key in
                    ('auto_trade_enabled', 'kill_latched', 'stock_live_enabled')) and
                type(result['controls']['stock_revision']) is int, 'persisted_controls_invalid')
        result['persisted_pause_verified'] = (cfg.get('auto_trade_enabled') is False and
            cfg.get('kill_latched') is False and stock.get('live_enabled') is False)
        try:
            expected = note.inspect(connection, evidence)
            result['historical_rows_sha256'] = hashlib.sha256(json.dumps(
                expected, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            result['reviewed_historical_case_matches'] = True
        except (ValueError, KeyError, TypeError):
            result['reviewed_historical_case_matches'] = False
            result['historical_rows_sha256'] = None
        require(identity(db.stat()) == identity(metadata), 'database_identity_changed_during_inspection')
        return result


def watchdog_triggers(helper, invoke=subprocess.run, cron_root=Path('/etc'), proc=Path('/proc')):
    """Report direct references only; never print cron commands or environments."""
    references, errors = [], []
    pattern = re.compile(r'(?:eod[_-]watchdog|kadima[^\n]*watchdog)', re.I)

    def inspect(raw, label):
        for number, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
            if line.strip() and not line.lstrip().startswith('#') and pattern.search(line):
                references.append(dict(source=label, line=number,
                    command_sha256=hashlib.sha256(line.encode()).hexdigest()))

    for user in ('root', 'ubuntu'):
        try:
            result = invoke(['/usr/bin/crontab', '-u', user, '-l'], capture_output=True,
                            timeout=10, text=True)
            if result.returncode == 0:
                inspect(result.stdout.encode(), 'crontab:'+user)
            elif result.returncode != 1 or 'no crontab for '+user not in result.stderr:
                errors.append('crontab_unavailable:'+user)
        except (OSError, subprocess.TimeoutExpired):
            errors.append('crontab_unavailable:'+user)
    paths = [cron_root/'crontab']
    for folder in ('cron.d', 'cron.hourly', 'cron.daily', 'cron.weekly', 'cron.monthly'):
        directory = cron_root/folder
        if directory.is_dir():
            paths.extend(sorted(directory.iterdir()))
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            errors.append('cron_entry_unreviewed')
            continue
        try:
            inspect(helper.read_regular(path, 200_000), str(path))
        except Exception:
            errors.append('cron_entry_unreadable')
    running = []
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw = (path/'cmdline').read_bytes()
            # Fixed source markers only; no arbitrary process args are emitted.
            if b'eod_watchdog' in raw or b'kadima-eod-watchdog' in raw:
                running.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            errors.append('process_scan_incomplete')
    timers = {}
    for name in ('kadima-eod-watchdog.timer', 'kadima-watchdog.timer'):
        timers[name] = helper.observe_unit(name)
    return dict(scope='direct_references_in_root_ubuntu_crontabs_etc_cron_and_processes',
                references=references, running_pids=running, timers=timers,
                inspection_errors=sorted(set(errors)), indirect_triggers_verified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--broker-check', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'sudo_required')
    sys.dont_write_bytecode = True
    os.umask(0o077)
    report = dict(action='read_only_stopped_readiness', timestamp=datetime.now(timezone.utc).isoformat(),
        source_commit=COMMIT, ledger_repaired=False, restart_requested=False, activation=False,
        broker_flat_verified=False, startup_authorized=False, final_services_verified_stopped=False)
    safe_errors = (Blocked,)
    try:
        helper, prep, source, note, context_module, broker_module = (load_reviewed(name) for name in PINS)
        safe_errors = (Blocked, context_module.Blocked, broker_module.Blocked)
        user = pwd.getpwnam('ubuntu')
        with ExitStack() as locks:
            for name in source.LOCKS:
                path = Path('/run/lock')/(name+'.lock')
                require(path.resolve(strict=False) == path, 'lock_path_unreviewed')
                handle = locks.enter_context(os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR |
                    os.O_NOFOLLOW, 0o600), 'w'))
                info = os.fstat(handle.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                        and not stat.S_IMODE(info.st_mode) & 0o022, 'private_lock_required')
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, fingerprints = verify_maintenance(helper, AUDIT_STATE)
            _, prior = source.verify_prior_state(helper, PRIOR_STATE)
            fingerprints.update(prior)
            prep.verify_timer_hold()
            source.ensure_stopped(UNITS, helper.observe_unit)
            report['services'] = {name: helper.observe_unit(name) for name in UNITS}
            report['watchdog'] = watchdog_triggers(helper)
            context = context_module.inspect_context(user=user, target=TARGET)
            report['service_context'] = context.summary
            report['runtime_flags'] = {name: (context.env[name] if context.env[name] in ('0', '1')
                else 'non_boolean_value_redacted') if name in context.env else 'unset' for name in
                ('KADIMA_FORCE_START', 'KADIMA_CELESTIAL_ENABLED', 'KADIMA_SESSION_AUTOTRADE',
                 'KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_CAS_LIVE', 'KADIMA_GAP_LIVE',
                 'KADIMA_GAP_PAPER', 'KADIMA_MCX_LIVE', 'KADIMA_MOVE_CAPTURE_LIVE')}
            directory = Path(tempfile.mkdtemp(prefix='kadima-stopped-readiness-', dir='/opt'))
            helper.private_path(directory, directory=True)
            helper.root_parents(directory)
            release = directory/'release'
            helper.stage_release(helper.canonical(args.history), release, source.RELEASE)
            installer = source.load_helper('_stopped_readiness_installer',
                release/'trade_audit_update/install.py', source.INSTALLER_BLOB)
            base = installer.base
            fingerprints.update(verify_sources(installer, release, TARGET,
                PACKAGE/'momentum_recovery/reviewed_dashboard', (user.pw_uid, user.pw_gid), source))
            require(blob(base.read_file(TARGET/'kadima/config.py')) == CONFIG_BLOB,
                    'installed_configuration_changed')
            report['order_placement_config'] = 'hardcoded_true_in_reviewed_source'
            report['source_admission_verified'] = True
            raw = private_evidence(args.evidence, helper, user.pw_uid)
            evidence = note.evidence(json.loads(raw), datetime.now(timezone.utc))
            note.account_matches(evidence, context.env)
            report['account_binding_verified'] = True
            report['evidence_sha256'] = hashlib.sha256(raw).hexdigest()
            report['historical_case'] = {key: evidence[key] for key in
                ('signal_id', 'symbol', 'token', 'exchange', 'quantity', 'trade_date', 'expiry', 'document_sha256')}

            def guard():
                context.guard()
                prep.verify_timer_hold()
                source.ensure_stopped(UNITS, helper.observe_unit)
                base.check_fingerprints(fingerprints)
                require(private_evidence(args.evidence, helper, user.pw_uid) == raw,
                        'reviewed_evidence_changed')

            guard()
            report['local'] = local_readiness(context.db_path, note, evidence)
            require(report['local']['persisted_pause_verified'], 'persisted_pause_required')
            require(report['local']['reviewed_historical_case_matches'], 'reviewed_historical_case_changed')
            if args.broker_check:
                report['broker'] = broker_module.read_broker(context.python, context.env, user, TARGET, evidence)
                # A complete observation is retained even if it prevents repair.
                report['broker_flat_verified'] = report['broker'].get('flat') is True
            guard()
            after = local_readiness(context.db_path, note, evidence)
            require(after == report['local'], 'local_state_changed_during_inspection')
            guard()
            report['final_services_verified_stopped'] = True
            report['state'] = 'stopped_readiness_observed'
            report['repair_review_pending'] = True
    except Exception as exc:
        # Only our own reason strings are emitted; arbitrary SDK/SQL text is private.
        report['state'] = 'blocked'
        report['broker_flat_verified'] = False
        report['blocked'] = str(exc) if isinstance(exc, safe_errors) else type(exc).__name__
        summary = getattr(exc, 'summary', None)
        if isinstance(summary, dict):
            report['service_context'] = summary
        print(json.dumps(report, indent=2, sort_keys=True))
        print('READ ONLY: no ledger repair, service start or activation requested. Review the reported blocker.')
        raise SystemExit(1)
    print(json.dumps(report, indent=2, sort_keys=True))
    print('READ ONLY: no ledger repair, source change, service start or activation. '
          'Broker login/read may append normal session audit logs; no order action is requested.')


if __name__ == '__main__':
    main()
