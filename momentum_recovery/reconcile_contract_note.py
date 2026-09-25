"""Guarded historical repair from an operator-reviewed expired-option contract note.

No orders or automatic activation. Default is a local read-only plan. --apply
requires fresh successful broker books before and after stopping the engine.
The original unknown intent is archived, never represented as a confirmed fill.
Exchange order numbers are kept distinct from SmartAPI broker order IDs.
"""
import argparse
from datetime import datetime
from decimal import Decimal
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
ROOT = Path('/home/ubuntu/kadima-py')
PACKAGE = Path(__file__).resolve().parents[1]


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def evidence(raw, now):
    e = dict(raw)
    require(re.fullmatch(r'[0-9a-f]{64}', e['document_sha256']) is not None, 'document_digest_required')
    require(type(e['signal_id']) is int and e['signal_id'] > 0, 'invalid_signal')
    require(type(e['quantity']) is int and e['quantity'] > 0, 'invalid_quantity')
    require(e['exchange'] == 'BFO', 'reviewed_exchange_required')
    day = datetime.fromisoformat(e['trade_date']).date()
    expiry = datetime.fromisoformat(e['expiry']).date()
    require(day == expiry < now.astimezone(IST).date(), 'expired_prior_day_contract_required')
    for name in ('entry_at', 'exit_at', 'intent_at'):
        at = datetime.fromisoformat(e[name])
        require(at.tzinfo is not None and at.astimezone(IST).date() == day, 'invalid_evidence_time')
    require(datetime.fromisoformat(e['entry_at']) < datetime.fromisoformat(e['intent_at']) < datetime.fromisoformat(e['exit_at']), 'inconsistent_execution_sequence')
    for name in ('entry_price', 'exit_price', 'expected_entry_price'):
        require(Decimal(str(e[name])).is_finite() and Decimal(str(e[name])) > 0, 'invalid_price')
    require(str(e['exchange_order_id']).isdigit() and str(e['trade_id']).isdigit(), 'exchange_execution_reference_required')
    require(type(e['day_buy_qty']) is int and type(e['day_sell_qty']) is int and e['day_buy_qty'] == e['day_sell_qty'] and e['day_buy_qty'] >= e['quantity'], 'note_not_balanced')
    return e


def inspect(c, e):
    positions = [dict(r) for r in c.execute("SELECT * FROM positions WHERE status IS NULL OR status!='closed'")]
    pending = [dict(r) for r in c.execute("SELECT * FROM execution_orders WHERE state IS NULL OR state!='terminal'")]
    require(len(positions) == len(pending) == 1, 'exactly_one_unresolved_position_and_intent_required')
    p, order = positions[0], pending[0]
    require(p['signal_id'] == order['signal_id'] == e['signal_id'], 'signal_changed')
    require(all(str(p[k]) == str(e[k]) for k in ('symbol', 'token', 'exchange')), 'contract_changed')
    require(p['kind'] == 'MANUAL_ADOPTED' and p['status'] == 'exit_pending', 'position_state_changed')
    require(p['remaining_qty'] == order['quantity'] == e['quantity'], 'quantity_changed')
    require(p['lots'] * p['lot_size'] == e['quantity'], 'partial_position_requires_separate_review')
    require(p['exit_order_id'] is None and p['exit_price'] is None and p['exit_ts'] is None, 'existing_exit_evidence_requires_review')
    require(Decimal(str(p['entry_price'])) == Decimal(str(e['expected_entry_price'])), 'entry_basis_changed')
    entry_at = datetime.fromisoformat(p['entry_ts'])
    require(entry_at.tzinfo is not None and entry_at.astimezone(IST).date().isoformat() == e['trade_date'], 'position_day_changed')
    require(order['intent'] == e['intent'] and order['created_at'] == e['intent_at'], 'intent_changed')
    require(order['side'] == 'SELL' and order['state'] == 'unknown' and order['order_id'] is None, 'order_state_changed')
    require(order['filled'] == 0 and order['notional'] == 0 and order['dry_run'] == 0, 'existing_fill_requires_review')
    require(c.execute('SELECT 1 FROM execution_fills WHERE intent=?', (e['intent'],)).fetchone() is None, 'existing_internal_fill')
    require(c.execute('SELECT 1 FROM celestial_external_fills WHERE signal_id=?', (e['signal_id'],)).fetchone() is None, 'existing_external_fill')
    cfg = json.loads(c.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()[0])
    require(cfg.get('auto_trade_enabled') is False and cfg.get('kill_latched') is False, 'paused_controls_required')
    return p, order


def repair(c, e, expected, broker_receipt, now):
    """Caller owns exclusive service stop, fresh broker checks and SQLite backup."""
    c.execute('BEGIN IMMEDIATE')
    try:
        require(inspect(c, e) == expected, 'state_changed_since_review')
        c.execute('''CREATE TABLE IF NOT EXISTS contract_note_reconciliations (
            intent TEXT PRIMARY KEY, reconciled_at TEXT NOT NULL,
            evidence TEXT NOT NULL, original_position TEXT NOT NULL,
            original_order TEXT NOT NULL, broker_receipt TEXT NOT NULL)''')
        p, order = expected
        c.execute('INSERT INTO contract_note_reconciliations VALUES (?,?,?,?,?,?)',
                  (e['intent'], now.isoformat(), json.dumps(e), json.dumps(p), json.dumps(order), json.dumps(broker_receipt)))
        # No broker order ID or fill is assigned to the unknown KADIMA request.
        c.execute("UPDATE execution_orders SET state='terminal' WHERE intent=?", (e['intent'],))
        c.execute("""UPDATE positions SET entry_price=?, exit_price=?, exit_ts=?,
            remaining_qty=0, status='closed', exit_reason='external_fill_reconciled'
            WHERE signal_id=?""", (float(e['entry_price']), float(e['exit_price']), e['exit_at'], e['signal_id']))
        pnl = (Decimal(str(e['exit_price'])) - Decimal(str(e['entry_price']))) * e['quantity']
        ref = 'EXCHANGE:' + e['exchange'] + ':' + str(e['exchange_order_id']) + ':' + str(e['trade_id'])
        c.execute('INSERT INTO celestial_external_fills VALUES (?,?,?,?,?,?,?)',
                  (e['signal_id'], ref, e['quantity'], e['quantity'], float(e['exit_price']), float(pnl), e['exit_at']))
        c.commit()
    except BaseException:
        c.rollback()
        raise


def pause_stock(base, token):
    """Pause stock intent through its revision-checked API for the next install."""
    state = base.local_api('/api/kadima/stock-fast', token)
    controls = state.get('controls') or {}
    require(type(controls.get('live_enabled')) is bool and type(controls.get('revision')) is int, 'stock_controls_unknown')
    if controls['live_enabled'] is False:
        return
    request = urllib.request.Request('http://127.0.0.1:8081/api/kadima/stock-fast/config',
        data=json.dumps({'patch':{'live_enabled':False},'expected_revision':controls['revision']}).encode(),
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), base.NoRedirect())
    with opener.open(request, timeout=10) as response:
        result = json.loads(response.read(10000))
    require(isinstance(result,dict) and result.get('live_enabled') is False, 'stock_pause_unconfirmed')
    print('Stock live intent paused for source installation; global Auto Trade remains paused.', flush=True)


BROKER_READ = r'''
import json, math, re
from datetime import datetime, timezone
stage='login'
try:
    from kadima.session import SESSION
    SESSION.ensure()
    result={'started_at':datetime.now(timezone.utc).isoformat()}
    for method in ('position','orderBook'):
        stage=method
        response=getattr(SESSION.api,method)()
        if not isinstance(response,dict) or response.get('status') is not True or 'data' not in response:
            raise ValueError('broker_read_unsuccessful')
        rows=response['data']
        if rows is None: rows=[]
        if not isinstance(rows,list): raise ValueError('invalid_book')
        for row in rows:
            if not isinstance(row,dict): raise ValueError('invalid_row')
            if method=='position':
                qty=row.get('netqty')
                if qty is None or isinstance(qty,bool) or not math.isfinite(float(qty)) or float(qty)!=0:
                    raise ValueError('broker_not_flat')
            elif str(row.get('orderstatus') or row.get('status') or '').lower() not in ('complete','completed','filled','cancelled','canceled','rejected'):
                raise ValueError('unresolved_broker_order')
        result[method+'_rows']=len(rows)
    result['verified_at']=datetime.now(timezone.utc).isoformat()
    result['flat']=True
    print('NOTE_BROKER '+json.dumps(result))
except Exception:
    print('NOTE_BROKER '+json.dumps({'flat':False,'stage':stage}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'sudo_required')
    e = evidence(json.loads(args.evidence.read_text()), datetime.now(IST))
    raw = (PACKAGE/'momentum_recovery/install.py').read_bytes()
    require(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() == '1a01595195c4440732b1a277fe75a2ecc7f058ea', 'reviewed_helper_required')
    spec = importlib.util.spec_from_file_location('_note_install', PACKAGE/'momentum_recovery/install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    base = installer.base
    user = pwd.getpwnam('ubuntu')
    locks = []
    for name in ('kadima-stock-fast-install', 'kadima-activation', 'kadima-session-auto-run',
                 'kadima-session-auto-install', 'kadima-multi-position-install',
                 'kadima-auth-recovery', 'kadima-kill-reset', 'kadima-contract-note-repair'):
        fd = os.open('/run/lock/'+name+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, 'w'); locks.append(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _, fingerprints = base.backend_plan(PACKAGE, ROOT, (user.pw_uid, user.pw_gid))
    pid, argv, env = base.process(base.ENGINE, ROOT, user.pw_uid)
    helper = base.load_helper(PACKAGE/'executor_fix/install.py', base.HELPER)
    python = helper.engine_interpreter(Path('/proc')/pid, ROOT)
    token = json.loads(base.read_file(base.TOKEN_FILE))['api_token']
    db = Path(env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db'
    require(db.is_absolute() and db.resolve() == db and db.is_file(), 'canonical_existing_db_required')
    def status():
        s = base.local_api('/api/kadima/top20', token)
        at = datetime.fromisoformat(s['ts'])
        require(at.tzinfo is not None and 0 <= (datetime.now(IST)-at).total_seconds() <= 30, 'fresh_status_required')
        require(s['config']['auto_trade_enabled'] is False and s['config']['kill_latched'] is False, 'paused_controls_required')
        return s
    s = status()
    require(len(s['positions']) == 1 and s['positions'][0]['signal_id'] == e['signal_id'], 'api_position_changed')
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        expected = inspect(c, e)
    print('PLAN: reconcile one expired historical position from reviewed contract-note fills; preserve original rows and unknown-order history.', flush=True)
    if not args.apply:
        print('PLAN ONLY: no broker login, database write, restart or activation.')
        return
    child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0', KADIMA_FAST_MOMENTUM_LIVE='0', KADIMA_CAS_LIVE='0')
    def flat():
        r = subprocess.run([python, '-B', '-c', BROKER_READ], cwd=ROOT, env=child_env,
                           user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                           capture_output=True, text=True, timeout=60)
        lines = [line[len('NOTE_BROKER '):] for line in r.stdout.splitlines() if line.startswith('NOTE_BROKER ')]
        require(r.returncode == 0 and len(lines) == 1, 'broker_read_unavailable')
        receipt = json.loads(lines[0])
        require(receipt.get('flat') is True, 'fresh_broker_flat_check_failed')
        started = datetime.fromisoformat(receipt['started_at'])
        require(0 <= (datetime.now(IST)-started).total_seconds() <= 30, 'broker_check_too_old')
        return receipt
    first = flat()
    pause_stock(base, token)
    base.check_fingerprints(fingerprints)
    require(base.process(base.ENGINE, ROOT, user.pw_uid)[0] == pid, 'engine_changed')
    require(status()['positions'] == s['positions'], 'api_state_changed')
    stopped = False
    metadata = db.stat()
    try:
        # Restart in finally even if the stop request has an uncertain response.
        stopped = True
        base.command(['systemctl', 'stop', base.ENGINE], timeout=60)
        unit = base.unit_state(base.ENGINE)
        require(unit.get('ActiveState') == 'inactive' and unit.get('MainPID') == '0', 'engine_not_stopped')
        base.check_fingerprints(fingerprints)
        second = flat()
        require(shutil.disk_usage('/var/backups').free > db.stat().st_size*1.2 + 100_000_000, 'backup_space_required')
        backup = Path('/var/backups')/('kadima-note-'+datetime.now(IST).strftime('%Y%m%dT%H%M%S'))
        backup.mkdir(mode=0o700)
        with sqlite3.connect(db.as_uri()+'?mode=rw', uri=True, timeout=5) as c:
            c.row_factory = sqlite3.Row
            require(inspect(c, e) == expected, 'local_state_changed')
            deadline = time.monotonic()+60
            def progress(*unused):
                require(time.monotonic() < deadline, 'backup_timed_out')
            with sqlite3.connect(backup/'before.db') as b:
                c.backup(b, pages=1024, progress=progress)
            # Backup can take time: renew the broker proof before committing.
            third = flat()
            require(base.unit_state(base.ENGINE).get('ActiveState') == 'inactive', 'engine_restarted_during_repair')
            base.check_fingerprints(fingerprints)
            repair(c, e, expected, {'before_stop':first,'after_stop':second,'before_commit':third}, datetime.now(IST))
        c.close()
        print('HISTORICAL REPAIR COMMITTED. Backup: '+str(backup), flush=True)
    finally:
        if stopped:
            for suffix in ('', '-wal', '-shm'):
                path = Path(str(db)+suffix)
                if path.exists():
                    require(not path.is_symlink(), 'unexpected_database_symlink')
                    os.chown(path, metadata.st_uid, metadata.st_gid)
            base.command(['systemctl', 'start', base.ENGINE], timeout=60)
    for attempt in range(20):
        try:
            s = status()
            if s['positions'] == [] and (s.get('system',{}).get('reconciliation') or {}).get('ok') is True:
                print('REPAIR VERIFIED: API positions empty; reconciliation healthy; Auto Trade remains paused. Run the guarded installer separately.')
                return
        except Exception:
            pass
        time.sleep(2)
    raise ValueError('repair_committed_postcheck_unconfirmed_do_not_repeat_apply')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only authored reason strings; never echo SDK errors, payloads or secrets.
        message = str(exc) if type(exc) is ValueError and re.fullmatch(r'[a-z_]+', str(exc)) else type(exc).__name__
        print('BLOCKED:', message)
        raise SystemExit(1)
