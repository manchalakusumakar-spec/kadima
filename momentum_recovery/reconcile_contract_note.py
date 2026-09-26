"""Guarded historical repair from an operator-reviewed expired-option contract note.

No orders or automatic activation. Default is a local read-only plan. --apply
requires fresh successful broker books before and after stopping the engine.
The original unknown intent is archived, never represented as a confirmed fill.
Exchange order numbers are kept distinct from SmartAPI broker order IDs.
"""
import argparse
import ast
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import hmac
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

# Account-bound admission applies to every mode. These are the reviewed PR19
# runtime and its accepted API-first run.py predecessor; ordinary backend_plan
# dependency/source checks still run independently.
REVIEWED_RUNTIME_SOURCES = {
    'run.py': {'cb701bb145a996635d689376a6b24ab2dac030e4',
               '3dd1a763faf472831b33db0bbf21d0d2acca16f7'},
    'kadima/utils.py': {'4781fd73993d899aa0c6c606521147f49cab895d'},
    'kadima/commodity_watch.py': {'2697a9b637ff4bd19f4a100fd3340a91facb216b'},
    'kadima/engine.py': {'7f9ddb10bbc3a144a82d6de982c5ca934e2433d1'},
    'kadima/auto_executor.py': {'7cfe6cf21a520ba967d6a672b2f54a99bbe81321'},
    'kadima/control_api.py': {'dc2ec6f89de5b7bcfe40b67ba35e36a621d06965'},
    'kadima/celestial.py': {'dfac18b9d76f11428357cc7d6b4d8f88d15278fa'},
}


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
    require(isinstance(e.get('account_sha256'), str) and
            re.fullmatch(r'[0-9a-f]{64}', e['account_sha256']) is not None,
            'reviewed_account_digest_required')
    return e


def account_matches(e, env):
    """Bind reviewed evidence to the configured account without printing its ID."""
    value = env.get('ANGEL_CLIENT_ID')
    require(isinstance(value, str) and bool(value.strip()), 'configured_account_required')
    actual = hashlib.sha256(value.strip().upper().encode()).hexdigest()
    expected = e.get('account_sha256')
    require(isinstance(expected, str) and hmac.compare_digest(actual, expected),
            'reviewed_account_mismatch')


def identifier_bindings(tree, name):
    """Count ordinary Python bindings, including forms without ast.Name Store."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            found.append(node)
        elif isinstance(node, ast.arg) and node.arg == name:
            found.append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append(node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            found.append(node)
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            found.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for item in node.names:
                bound = item.asname or (item.name.split('.')[0] if isinstance(node, ast.Import) else item.name)
                if bound in (name, '*'):
                    found.append(node)
    return found


def reviewed_sources(fingerprints, root=ROOT):
    for name, allowed in REVIEWED_RUNTIME_SOURCES.items():
        raw = fingerprints.get(root/name)
        require(isinstance(raw, bytes) and
                hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() in allowed,
                'runtime_source_unreviewed')
    # config.py is host-specific in backend_plan. Confirm the account binding is
    # its reviewed direct environment assignment; the broker child independently
    # checks the imported value before calling ensure(). No live import here.
    raw = fingerprints.get(root/'kadima/config.py')
    require(isinstance(raw, bytes), 'account_config_unreviewed')
    tree = ast.parse(raw)
    for name, source, reason in (
            ('ZoneInfo', 'from zoneinfo import ZoneInfo', 'timezone_unreviewed'),
            ('os', 'import os', 'environment_binding_unreviewed')):
        bindings = identifier_bindings(tree, name)
        expected = ast.dump(ast.parse(source).body[0], include_attributes=False)
        require(len(bindings) == 1 and bindings[0] in tree.body
                and ast.dump(bindings[0], include_attributes=False) == expected, reason)
    for name, source, reason in (
            ('ANGEL_CLIENT_ID', 'ANGEL_CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID", "")',
             'account_config_unreviewed'),
            ('IST', 'IST = ZoneInfo("Asia/Kolkata")', 'timezone_unreviewed'),
            ('DATA_DIR', 'DATA_DIR = os.environ.get("KADIMA_DATA_DIR", "/var/lib/kadima")',
             'database_config_unreviewed'),
            ('DB_PATH', 'DB_PATH = os.path.join(DATA_DIR, "kadima.db")',
             'database_config_unreviewed')):
        expected = ast.parse(source).body[0]
        bindings = identifier_bindings(tree, name)
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False)]
        require(len(bindings) == len(assignments) == 1, reason)


def process_identity(pid, proc=Path('/proc'), ticks=None):
    """Include boot/start ticks so a reused PID cannot pass a same-process check."""
    require(isinstance(pid, str) and pid.isdigit() and int(pid) > 0, 'invalid_engine_pid')
    raw = (proc/pid/'stat').read_text()
    require(raw.split(' ', 1)[0] == pid and ') ' in raw, 'invalid_engine_process_stat')
    fields = raw.rsplit(') ', 1)[1].split()
    started_ticks = int(fields[19])  # /proc/PID/stat field 22, after pid and comm
    boots = [line.split()[1] for line in (proc/'stat').read_text().splitlines()
             if line.startswith('btime ')]
    require(len(boots) == 1 and started_ticks >= 0, 'invalid_engine_process_stat')
    frequency = ticks if ticks is not None else os.sysconf('SC_CLK_TCK')
    require(frequency > 0, 'invalid_engine_process_stat')
    boot_time = int(boots[0])
    return dict(pid=pid, start_ticks=started_ticks, boot_time=boot_time,
                started_at=datetime.fromtimestamp(boot_time+started_ticks/frequency, timezone.utc))


def off_session_wait(e, env, status, identity, now, expected_identity=None):
    """Empty API positions are an unrestored view, never evidence of flatness."""
    if expected_identity is not None:
        require(identity == expected_identity, 'engine_changed')
    require(now.tzinfo is not None, 'off_session_clock_unknown')
    started = identity['started_at']
    require(started.tzinfo is not None, 'off_session_clock_unknown')
    local = now.astimezone(IST)
    require(local.weekday() >= 5 and started.astimezone(IST).date() == local.date()
            and 0 <= (now-started).total_seconds(), 'same_day_weekend_start_required')
    # Do not start or commit a maintenance operation across the day boundary.
    require((local.hour, local.minute) < (23, 55), 'weekend_maintenance_window_ending')
    require(env.get('KADIMA_FORCE_START', '0') == '0', 'force_start_must_be_disabled')
    require(env.get('KADIMA_CELESTIAL_ENABLED') == '1', 'celestial_runtime_required')
    account_matches(e, env)
    require(status.get('positions') == [], 'off_session_api_position_changed')
    cfg = status.get('config') or {}
    require(cfg.get('rollout_enabled') is True and cfg.get('auto_trade_enabled') is False
            and cfg.get('kill_latched') is False, 'paused_controls_required')
    health = (status.get('system') or {}).get('reconciliation')
    if health is not None:
        require(isinstance(health, dict) and isinstance(health.get('ts'), str),
                'reconciliation_start_state_unknown')
        at = datetime.fromisoformat(health['ts'])
        # btime has whole-second precision: requiring a one-second margin avoids
        # treating a post-start write near that boundary as old health.
        require(at.tzinfo is not None and (started-at).total_seconds() >= 1,
                'reconciliation_started_in_current_process')


def off_session_persisted(c, e, env, identity, now, require_stock_paused=True):
    """Check persisted admission in plan and repeat it inside the transaction."""
    cfg_row = c.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
    stock_row = c.execute('SELECT payload FROM stock_fast_controls WHERE id=1').fetchone()
    require(cfg_row is not None and stock_row is not None, 'persisted_controls_required')
    cfg = json.loads(cfg_row[0])
    stock_live = json.loads(stock_row[0]).get('live_enabled')
    require(type(stock_live) is bool, 'persisted_stock_controls_invalid')
    if require_stock_paused:
        require(stock_live is False, 'stock_intent_must_be_paused')
    row = c.execute("SELECT ts,payload FROM celestial_health WHERE name='reconciliation'").fetchone()
    health = None if row is None else dict(json.loads(row[1]), ts=row[0])
    off_session_wait(e, env, dict(positions=[], config=dict(cfg, rollout_enabled=True),
                                 system=dict(reconciliation=health)), identity, now)


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


def repair(c, e, expected, broker_receipt, now, guard=None):
    """Caller owns exclusive service stop, fresh broker checks and SQLite backup."""
    c.execute('BEGIN IMMEDIATE')
    try:
        require(inspect(c, e) == expected, 'state_changed_since_review')
        if guard is not None:
            guard(c)
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


def verify_committed(c, e, expected):
    """Verify exact durable outcomes, rather than inferring success from the UI."""
    p, order = expected
    observed = c.execute('SELECT * FROM positions WHERE signal_id=?', (e['signal_id'],)).fetchone()
    wanted = dict(p, entry_price=float(e['entry_price']), exit_price=float(e['exit_price']),
                  exit_ts=e['exit_at'], remaining_qty=0, status='closed',
                  exit_reason='external_fill_reconciled')
    require(observed is not None and dict(observed) == wanted, 'repaired_position_changed')
    observed = c.execute('SELECT * FROM execution_orders WHERE intent=?', (e['intent'],)).fetchone()
    require(observed is not None and dict(observed) == dict(order, state='terminal'),
            'repaired_intent_changed')
    require(c.execute("SELECT 1 FROM positions WHERE status IS NULL OR status!='closed'").fetchone() is None
            and c.execute("SELECT 1 FROM execution_orders WHERE state IS NULL OR state!='terminal'").fetchone() is None,
            'unresolved_state_after_repair')
    require(c.execute('SELECT 1 FROM execution_fills WHERE intent=?', (e['intent'],)).fetchone() is None,
            'unexpected_internal_fill_after_repair')
    receipt = c.execute('SELECT * FROM contract_note_reconciliations WHERE intent=?', (e['intent'],)).fetchone()
    require(receipt is not None and json.loads(receipt['evidence']) == e
            and json.loads(receipt['original_position']) == p and json.loads(receipt['original_order']) == order,
            'repair_archive_changed')
    broker = json.loads(receipt['broker_receipt'])
    require(isinstance(broker, dict) and set(broker) == {'before_stop','after_stop','before_commit'}
            and all(isinstance(item, dict) and item.get('flat') is True for item in broker.values()),
            'repair_broker_receipt_changed')
    pnl = (Decimal(str(e['exit_price']))-Decimal(str(e['entry_price']))) * e['quantity']
    reference = 'EXCHANGE:'+e['exchange']+':'+str(e['exchange_order_id'])+':'+str(e['trade_id'])
    fills = [dict(r) for r in c.execute('SELECT * FROM celestial_external_fills WHERE signal_id=?', (e['signal_id'],))]
    require(fills == [dict(signal_id=e['signal_id'], order_id=reference, cumulative_qty=e['quantity'],
                          quantity=e['quantity'], price=float(e['exit_price']), pnl=float(pnl), ts=e['exit_at'])],
            'historical_external_fill_changed')


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
import hashlib, hmac, json, math, os, re
from datetime import datetime, timezone
def safe_position(row):
    if not isinstance(row,dict): return {}
    out={}
    for key,allowed in (
        ('exchange',{'NSE','NFO','BSE','BFO','MCX','NCDEX'}),
        ('producttype',{'INTRADAY','CARRYFORWARD','DELIVERY','MARGIN','BO','CO'})):
        value=row.get(key)
        if isinstance(value,str) and value in allowed: out[key]=value
    value=row.get('tradingsymbol')
    if isinstance(value,str) and re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}',value):
        out['tradingsymbol']=value
    value=row.get('symboltoken')
    if type(value) is int: value=str(value)
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,16}',value): out['symboltoken']=value
    for key in ('netqty',):
        value=row.get(key)
        if type(value) not in (int,float,str) or (isinstance(value,str) and len(value)>32): continue
        try:
            quantity=float(value)
            if math.isfinite(quantity) and abs(quantity)<=1_000_000_000:
                out[key]=int(quantity) if quantity.is_integer() else quantity
        except (ValueError,TypeError,OverflowError): pass
    return out
stage='imports'
reason='imports_failed'
broker_code=None
blocked_position=None
try:
    stage='account'
    reason='account_check_failed'
    expected_account = os.environ.get('KADIMA_NOTE_ACCOUNT_SHA256')
    if not isinstance(expected_account,str) or re.fullmatch(r'[0-9a-f]{64}',expected_account) is None:
        raise ValueError('reviewed_account_digest_required')
    from kadima.config import ANGEL_CLIENT_ID
    if not isinstance(ANGEL_CLIENT_ID,str) or not ANGEL_CLIENT_ID.strip():
        raise ValueError('configured_account_required')
    actual = hashlib.sha256(ANGEL_CLIENT_ID.strip().upper().encode()).hexdigest()
    if not hmac.compare_digest(actual, expected_account):
        reason='account_mismatch'
        raise ValueError('reviewed_account_mismatch')
    stage='imports'
    reason='imports_failed'
    from kadima.session import SESSION
    stage='login'
    reason='login_failed'
    SESSION.ensure()
    result={'started_at':datetime.now(timezone.utc).isoformat()}
    for method in ('position','orderBook'):
        stage=method
        reason='broker_read_failed'
        response=getattr(SESSION.api,method)()
        if not isinstance(response,dict):
            reason='response_schema_invalid'
            raise ValueError('broker_response_invalid')
        if response.get('status') is not True:
            reason='response_unsuccessful'
            candidate=response.get('errorcode') or response.get('errorCode')
            if isinstance(candidate,str) and candidate in {'AG8001','AG8002','AG8003','AB8050','AB8051',
                    'AB1000','AB1004','AB1006','AB1007','AB1010','AB1011'}:
                broker_code=candidate
            raise ValueError('broker_read_unsuccessful')
        if 'data' not in response:
            reason='response_schema_invalid'
            raise ValueError('broker_response_invalid')
        rows=response['data']
        if rows is None: rows=[]
        if not isinstance(rows,list):
            reason='book_schema_invalid'
            raise ValueError('invalid_book')
        for row in rows:
            if not isinstance(row,dict):
                reason='row_schema_invalid'
                raise ValueError('invalid_row')
            if method=='position':
                reason='position_quantity_invalid'
                qty=row.get('netqty')
                if qty is None or isinstance(qty,bool) or not math.isfinite(float(qty)):
                    raise ValueError('invalid_position_quantity')
                if float(qty)!=0:
                    reason='position_nonzero'
                    blocked_position=safe_position(row)
                    raise ValueError('broker_not_flat')
            elif str(row.get('orderstatus') or row.get('status') or '').lower() not in ('complete','completed','filled','cancelled','canceled','rejected'):
                reason='order_unresolved'
                raise ValueError('unresolved_broker_order')
        result[method+'_rows']=len(rows)
    result['verified_at']=datetime.now(timezone.utc).isoformat()
    result['flat']=True
    print('NOTE_BROKER '+json.dumps(result))
except Exception as exc:
    error=type(exc).__name__
    if error not in {'ModuleNotFoundError','ImportError','PermissionError','FileNotFoundError',
                     'ValueError','TypeError','AttributeError','KeyError','IndexError','RuntimeError',
                     'TimeoutError','ConnectionError','OSError','RequestException','ConnectTimeout',
                     'ReadTimeout','HTTPError','JSONDecodeError','DataException','TokenException',
                     'NetworkException','GeneralException','InputException','OrderException'}:
        error='OtherError'
    diagnostic={'flat':False,'stage':stage,'reason':reason,'error':error}
    if broker_code is not None: diagnostic['broker_code']=broker_code
    if stage=='position' and reason=='position_nonzero' and blocked_position:
        diagnostic['position']=blocked_position
        diagnostic['position_scope']='first_nonzero_blocker'
    print('NOTE_BROKER '+json.dumps(diagnostic))
'''


BROKER_STAGES = frozenset(('imports','account','login','position','orderBook','transport'))
BROKER_REASONS = frozenset(('imports_failed','account_check_failed','account_mismatch','login_failed',
    'broker_read_failed','response_schema_invalid','response_unsuccessful','book_schema_invalid',
    'row_schema_invalid','position_quantity_invalid','position_nonzero','order_unresolved',
    'child_failed','child_timeout','child_unavailable','diagnostic_invalid','receipt_invalid','receipt_stale'))
BROKER_ERROR_TYPES = frozenset(('ModuleNotFoundError','ImportError','PermissionError','FileNotFoundError',
    'ValueError','TypeError','AttributeError','KeyError','IndexError','RuntimeError','TimeoutError',
    'ConnectionError','OSError','RequestException','ConnectTimeout','ReadTimeout','HTTPError',
    'JSONDecodeError','DataException','TokenException','NetworkException','GeneralException',
    'InputException','OrderException','TimeoutExpired','ChildProcessError','OtherError'))
BROKER_CODES = frozenset(('AG8001','AG8002','AG8003','AB8050','AB8051','AB1000','AB1004',
                         'AB1006','AB1007','AB1010','AB1011'))
BROKER_CHECKPOINTS = frozenset(('check_only','before_stop','after_stop','before_commit','after_restart'))


def safe_position(row):
    """Bounded contract/quantity evidence only, independently checked by parent."""
    if not isinstance(row, dict):
        return {}
    out = {}
    for key, allowed in (
            ('exchange', {'NSE','NFO','BSE','BFO','MCX','NCDEX'}),
            ('producttype', {'INTRADAY','CARRYFORWARD','DELIVERY','MARGIN','BO','CO'})):
        value = row.get(key)
        if isinstance(value, str) and value in allowed:
            out[key] = value
    value = row.get('tradingsymbol')
    if isinstance(value, str) and re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}', value):
        out['tradingsymbol'] = value
    value = row.get('symboltoken')
    if type(value) is int:
        value = str(value)
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,16}', value):
        out['symboltoken'] = value
    for key in ('netqty',):
        value = row.get(key)
        if type(value) not in (int, float, str) or (isinstance(value, str) and len(value) > 32):
            continue
        try:
            quantity = float(value)
            if math.isfinite(quantity) and abs(quantity) <= 1_000_000_000:
                out[key] = int(quantity) if quantity.is_integer() else quantity
        except (ValueError, TypeError, OverflowError):
            pass
    return out


def broker_failure(stage, reason, error, broker_code=None, checkpoint=None, position=None):
    """Only allowlisted diagnostics reach the terminal; never provider messages."""
    def safe(value, allowed, fallback):
        return value if isinstance(value, str) and value in allowed else fallback
    diagnostic = dict(stage=safe(stage, BROKER_STAGES, 'transport'),
                      reason=safe(reason, BROKER_REASONS, 'diagnostic_invalid'),
                      error=safe(error, BROKER_ERROR_TYPES, 'OtherError'))
    if checkpoint is not None:
        diagnostic['checkpoint'] = safe(checkpoint, BROKER_CHECKPOINTS, 'unknown')
    if (diagnostic['stage'] in ('position','orderBook') and diagnostic['reason']=='response_unsuccessful'
            and isinstance(broker_code, str) and broker_code in BROKER_CODES):
        diagnostic['broker_code'] = broker_code
    if diagnostic['stage']=='position' and diagnostic['reason']=='position_nonzero':
        filtered = safe_position(position)
        if filtered:
            diagnostic['position'] = filtered
            diagnostic['position_scope'] = 'first_nonzero_blocker'
    print('NOTE_BROKER_DIAGNOSTIC '+json.dumps(diagnostic), flush=True)
    raise ValueError('fresh_broker_flat_check_failed')


def broker_receipt(result, now, checkpoint=None):
    """Parse the child's narrow receipt without exposing its captured output."""
    def fail(stage, reason, error, broker_code=None, position=None):
        broker_failure(stage, reason, error, broker_code, checkpoint, position)
    if result.returncode != 0:
        fail('transport', 'child_failed', 'ChildProcessError')
    lines = [line[len('NOTE_BROKER '):] for line in result.stdout.splitlines() if line.startswith('NOTE_BROKER ')]
    if len(lines) != 1:
        fail('transport', 'diagnostic_invalid', 'ValueError')
    try:
        receipt = json.loads(lines[0])
    except (TypeError, ValueError):
        fail('transport', 'diagnostic_invalid', 'ValueError')
    if not isinstance(receipt, dict):
        fail('transport', 'diagnostic_invalid', 'ValueError')
    if receipt.get('flat') is not True:
        fail(receipt.get('stage'), receipt.get('reason'), receipt.get('error'), receipt.get('broker_code'),
             receipt.get('position'))
    try:
        started = datetime.fromisoformat(receipt['started_at'])
        require(started.tzinfo is not None, 'receipt_invalid')
        age = (now-started).total_seconds()
    except (KeyError, TypeError, ValueError):
        fail('transport', 'receipt_invalid', 'ValueError')
    if not 0 <= age <= 30:
        fail('transport', 'receipt_stale', 'ValueError')
    return receipt


def initial_broker_check(apply, check_only, read):
    """The only path from admission to mutation requires explicit apply."""
    require(not (apply and check_only), 'mutually_exclusive_broker_modes')
    if not apply and not check_only:
        print('PLAN ONLY: no broker login, database write, restart or activation.')
        return None
    receipt = read()
    if check_only:
        print('BROKER CHECK ONLY: fresh position/order books passed; no control pause, database repair, service change or activation.')
        return None
    return receipt


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--broker-check-only', action='store_true',
                      help='After local admission, perform one fresh broker read and exit without repair')
    parser.add_argument('--off-session-unrestored', action='store_true',
                        help='Reviewed same-day weekend startup only; requires account-bound evidence')
    return parser.parse_args(argv)


def main():
    args = parse_arguments()
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
    account_matches(e, env)
    reviewed_sources(fingerprints)
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
    identity = None
    if args.off_session_unrestored:
        identity = process_identity(pid)
        off_session_wait(e, env, s, identity, datetime.now(IST))
        require(base.process(base.ENGINE, ROOT, user.pw_uid)[0] == pid
                and process_identity(pid) == identity, 'engine_changed')
    else:
        require(len(s['positions']) == 1 and s['positions'][0]['signal_id'] == e['signal_id'], 'api_position_changed')
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        expected = inspect(c, e)
        if args.off_session_unrestored:
            off_session_persisted(c, e, env, identity, datetime.now(IST), require_stock_paused=False)
    print('PLAN: reconcile one expired historical position from reviewed contract-note fills; preserve original rows and unknown-order history.', flush=True)
    if args.off_session_unrestored:
        print('WEEKEND PLAN: account-bound evidence and pinned pre-restoration startup verified; empty API is not broker-flat proof.', flush=True)
    child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0', KADIMA_FAST_MOMENTUM_LIVE='0', KADIMA_CAS_LIVE='0')
    child_env['KADIMA_NOTE_ACCOUNT_SHA256'] = e['account_sha256']
    def flat(checkpoint):
        try:
            result = subprocess.run([python, '-B', '-c', BROKER_READ], cwd=ROOT, env=child_env,
                                    user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                                    capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            broker_failure('transport', 'child_timeout', 'TimeoutExpired', checkpoint=checkpoint)
        except OSError as exc:
            broker_failure('transport', 'child_unavailable', type(exc).__name__, checkpoint=checkpoint)
        return broker_receipt(result, datetime.now(IST), checkpoint)
    first = initial_broker_check(args.apply, args.broker_check_only,
        lambda: flat('check_only' if args.broker_check_only else 'before_stop'))
    if first is None:
        return
    pause_stock(base, token)
    base.check_fingerprints(fingerprints)
    require(base.process(base.ENGINE, ROOT, user.pw_uid)[0] == pid, 'engine_changed')
    require(status()['positions'] == s['positions'], 'api_state_changed')
    if args.off_session_unrestored:
        current_pid, _, current_env = base.process(base.ENGINE, ROOT, user.pw_uid)
        off_session_wait(e, current_env, status(), process_identity(current_pid), datetime.now(IST), identity)
    stopped = False
    metadata = db.stat()
    try:
        # Restart in finally even if the stop request has an uncertain response.
        stopped = True
        base.command(['systemctl', 'stop', base.ENGINE], timeout=60)
        unit = base.unit_state(base.ENGINE)
        require(unit.get('ActiveState') == 'inactive' and unit.get('MainPID') == '0', 'engine_not_stopped')
        base.check_fingerprints(fingerprints)
        second = flat('after_stop')
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
            third = flat('before_commit')
            require(base.unit_state(base.ENGINE).get('ActiveState') == 'inactive', 'engine_restarted_during_repair')
            base.check_fingerprints(fingerprints)
            guard = None
            if args.off_session_unrestored:
                guard = lambda connection: off_session_persisted(connection, e, env, identity, datetime.now(IST))
            repair(c, e, expected, {'before_stop':first,'after_stop':second,'before_commit':third}, datetime.now(IST), guard)
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
    if args.off_session_unrestored:
        # Restart intentionally remains in the same weekend wait. Neither fresh
        # API timestamps nor old persisted health imply startup reconciliation.
        try:
            post_status = None
            for attempt in range(20):
                try:
                    post_status = status()
                    break
                except Exception:
                    time.sleep(2)
            require(post_status is not None, 'restarted_api_unavailable')
            post_pid, _, post_env = base.process(base.ENGINE, ROOT, user.pw_uid)
            post_identity = process_identity(post_pid)
            require(post_identity != identity and post_identity['started_at'] >= identity['started_at'],
                    'engine_restart_unconfirmed')
            require(helper.engine_interpreter(Path('/proc')/post_pid, ROOT) == python,
                    'engine_interpreter_changed')
            require(Path(post_env.get('KADIMA_DATA_DIR', '/var/lib/kadima'))/'kadima.db' == db,
                    'engine_database_changed')
            base.check_fingerprints(fingerprints)
            off_session_wait(e, post_env, post_status, post_identity, datetime.now(IST))
            flat('after_restart')  # Fourth successful account-bound read.
            current_pid, _, current_env = base.process(base.ENGINE, ROOT, user.pw_uid)
            off_session_wait(e, current_env, status(), process_identity(current_pid),
                             datetime.now(IST), post_identity)
            base.check_fingerprints(fingerprints)
            with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as c:
                c.row_factory = sqlite3.Row
                c.execute('PRAGMA query_only=ON')
                c.execute('BEGIN')
                verify_committed(c, e, expected)
                off_session_persisted(c, e, current_env, post_identity, datetime.now(IST))
            print('HISTORICAL REPAIR VERIFIED: exact ledger/archive/fill checks and fresh broker-flat reads passed. '
                  'Auto Trade and stock live intent remain paused; live startup reconciliation remains pending. '
                  'Run the guarded installer separately.')
            return
        except Exception:
            raise ValueError('repair_committed_postcheck_unconfirmed_do_not_repeat_apply') from None
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
