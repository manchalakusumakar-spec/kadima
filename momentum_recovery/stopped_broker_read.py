"""Bounded read-only broker observation for stopped historical-repair review.

This module has no repair, service, restart, activation or order operation. Login
through the reviewed runtime may emit its ordinary local audit records. Account
flatness, complete books and permission for this narrow external holding are
reported separately; none authorizes startup or deployment.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import subprocess


PREFIX = 'KADIMA_STOPPED_BROKER '
ALLOWED_HOLDING = dict(exchange='NFO', producttype='CARRYFORWARD',
    tradingsymbol='NIFTY29SEP2623250CE', symboltoken='73921', netqty=130)
STAGES = frozenset(('account', 'login', 'position', 'orderBook', 'transport'))
REASONS = frozenset(('account_binding_invalid', 'account_mismatch', 'login_failed',
    'book_request_failed', 'response_invalid', 'response_unsuccessful',
    'book_schema_invalid', 'book_too_large', 'row_invalid', 'position_quantity_invalid',
    'position_identity_invalid', 'position_duplicate', 'historical_identity_mismatch',
    'order_identity_invalid', 'order_duplicate', 'order_status_invalid',
    'order_status_conflict', 'order_quantity_invalid', 'order_side_invalid',
    'order_price_invalid', 'unexpected_exposure',
    'historical_exposure_present', 'unresolved_broker_orders'))
ERROR_TYPES = frozenset(('ValueError', 'TypeError', 'KeyError', 'AttributeError',
    'ImportError', 'ModuleNotFoundError', 'OSError', 'RuntimeError', 'TimeoutError',
    'ConnectionError', 'RequestException', 'ConnectTimeout', 'ReadTimeout',
    'HTTPError', 'JSONDecodeError', 'TokenException', 'OtherError'))
RECEIPT_KEYS = frozenset(('started_at', 'checked_at', 'books_complete', 'flat',
    'readiness', 'historical_contract_absent', 'permitted_manual_holding_observed',
    'position_rows', 'order_rows', 'pending_orders', 'pending_order_details', 'nonzero_positions',
    'nonzero_position_count', 'error'))
PENDING_DETAIL_KEYS = frozenset(('exchange', 'tradingsymbol', 'symboltoken', 'producttype', 'variety',
    'orderid', 'transactiontype', 'status', 'quantity', 'filled_quantity',
    'remaining_quantity', 'price', 'trigger_price'))
TERMINAL = frozenset(('complete', 'completed', 'filled', 'cancelled', 'canceled', 'rejected'))


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


CHILD_CODE = r'''
import hashlib, hmac, json, math, os, re
from datetime import datetime, timezone
PREFIX = 'KADIMA_STOPPED_BROKER '
ALLOWED = dict(exchange='NFO',producttype='CARRYFORWARD',tradingsymbol='NIFTY29SEP2623250CE',symboltoken='73921',netqty=130)
TERMINAL = {'complete','completed','filled','cancelled','canceled','rejected'}
ERROR_TYPES = {'ValueError','TypeError','KeyError','AttributeError','ImportError','ModuleNotFoundError',
    'OSError','RuntimeError','TimeoutError','ConnectionError','RequestException','ConnectTimeout',
    'ReadTimeout','HTTPError','JSONDecodeError','TokenException'}
EXCHANGES = {'NSE','NFO','BSE','BFO','MCX','NCDEX'}
PRODUCTS = {'INTRADAY','CARRYFORWARD','DELIVERY','MARGIN','BO','CO'}
reason = 'account_binding_invalid'
stage = 'account'
report = dict(started_at=datetime.now(timezone.utc).isoformat(), checked_at=None,
    books_complete=False,flat=None,readiness=False,historical_contract_absent=None,
    permitted_manual_holding_observed=False,position_rows=None,order_rows=None,
    pending_orders=None,pending_order_details=[],nonzero_positions=[],nonzero_position_count=None,error=None)

def fail(value):
    global reason
    reason = value
    raise ValueError()

def integer(value, why, *, minimum=-1000000000, maximum=1000000000):
    if isinstance(value,bool) or type(value) not in (int,float,str): fail(why)
    if isinstance(value,str) and (len(value)>32 or not re.fullmatch(r'[+-]?\d+(?:\.0+)?',value)): fail(why)
    number=float(value)
    if not math.isfinite(number) or not number.is_integer() or not minimum<=number<=maximum: fail(why)
    return int(number)

def identity(row, why):
    exchange, symbol, token = row.get('exchange'),row.get('tradingsymbol'),row.get('symboltoken')
    if type(token) is int: token=str(token)
    if not isinstance(exchange,str) or exchange not in EXCHANGES or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}',symbol): fail(why)
    if not isinstance(token,str) or not re.fullmatch(r'[0-9]{1,16}',token): fail(why)
    return exchange,symbol,token

def rows(response):
    if not isinstance(response,dict): fail('response_invalid')
    if response.get('status') is not True: fail('response_unsuccessful')
    if 'data' not in response: fail('response_invalid')
    result=response['data']
    if result is None: result=[]
    if not isinstance(result,list): fail('book_schema_invalid')
    if len(result)>2000: fail('book_too_large')
    if any(not isinstance(row,dict) for row in result): fail('row_invalid')
    return result

def positions(items, historical):
    seen, nonzero = set(), []
    historical_absent=True
    for row in items:
        exchange,symbol,token=identity(row,'position_identity_invalid')
        product=row.get('producttype')
        if not isinstance(product,str) or product not in PRODUCTS: fail('position_identity_invalid')
        key=(exchange,token,product)
        if key in seen: fail('position_duplicate')
        seen.add(key)
        qty=integer(row.get('netqty'),'position_quantity_invalid')
        if (exchange,token)==historical[:2]:
            if symbol!=historical[2]: fail('historical_identity_mismatch')
            if qty: historical_absent=False
        if qty:
            nonzero.append(dict(exchange=exchange,producttype=product,tradingsymbol=symbol,symboltoken=token,netqty=qty))
    return nonzero,historical_absent

def optional_price(value):
    if value is None or value=='': return None
    if isinstance(value,bool) or type(value) not in (int,float,str): fail('order_price_invalid')
    if isinstance(value,str) and (len(value)>32 or not re.fullmatch(r'[+]?\d+(?:\.\d+)?',value)): fail('order_price_invalid')
    number=float(value)
    if not math.isfinite(number) or not 0<=number<=1000000000: fail('order_price_invalid')
    return number

def orders(items):
    seen, pending, details=set(),0,[]
    for row in items:
        exchange,symbol,token=identity(row,'order_identity_invalid')
        order_id=row.get('orderid')
        if type(order_id) is int: order_id=str(order_id)
        if not isinstance(order_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',order_id): fail('order_identity_invalid')
        if order_id in seen: fail('order_duplicate')
        seen.add(order_id)
        states=[]
        for key in ('orderstatus','status'):
            if key not in row or row[key] is None or row[key]=='': continue
            value=row[key]
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z_ -]{1,64}',value): fail('order_status_invalid')
            states.append(value.strip().lower())
        if not states or not states[0]: fail('order_status_invalid')
        if len(set(states))!=1: fail('order_status_conflict')
        quantity=integer(row.get('quantity'),'order_quantity_invalid',minimum=0)
        quantities={}
        for key in ('filledshares','unfilledshares'):
            if key in row and row[key] is not None and row[key]!='':
                quantities[key]=integer(row[key],'order_quantity_invalid',minimum=0,maximum=quantity)
        if sum(quantities.values())>quantity: fail('order_quantity_invalid')
        if states[0] not in TERMINAL:
            side=row.get('transactiontype')
            if not isinstance(side,str) or side.upper() not in ('BUY','SELL'): fail('order_side_invalid')
            product=row.get('producttype')
            if product=='': product=None
            if product is not None and (not isinstance(product,str) or product not in PRODUCTS): fail('order_identity_invalid')
            variety=row.get('variety')
            if variety=='': variety=None
            if variety is not None:
                if not isinstance(variety,str) or variety.upper() not in ('NORMAL','STOPLOSS','AMO','ROBO'): fail('order_identity_invalid')
                variety=variety.upper()
            detail=dict(exchange=exchange,tradingsymbol=symbol,symboltoken=token,producttype=product,variety=variety,
                orderid=order_id,transactiontype=side.upper(),status=states[0],quantity=quantity,
                filled_quantity=quantities.get('filledshares'),remaining_quantity=quantities.get('unfilledshares'),
                price=optional_price(row.get('price')),trigger_price=optional_price(row.get('triggerprice')))
            pending+=1
            if len(details)<8: details.append(detail)
    return pending,details

def error():
    import sys
    kind=type(sys.exc_info()[1]).__name__
    return dict(stage=stage,reason=reason,type=kind if kind in ERROR_TYPES else 'OtherError')

try:
    account=os.environ.get('KADIMA_NOTE_ACCOUNT_SHA256')
    exchange=os.environ.get('KADIMA_HISTORY_EXCHANGE')
    token=os.environ.get('KADIMA_HISTORY_TOKEN')
    symbol=os.environ.get('KADIMA_HISTORY_SYMBOL')
    if not isinstance(account,str) or not re.fullmatch(r'[0-9a-f]{64}',account): fail('account_binding_invalid')
    if exchange!='BFO' or not isinstance(token,str) or not re.fullmatch(r'[0-9]{1,16}',token): fail('account_binding_invalid')
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}',symbol): fail('account_binding_invalid')
    from kadima.config import ANGEL_CLIENT_ID
    if not isinstance(ANGEL_CLIENT_ID,str) or not ANGEL_CLIENT_ID.strip(): fail('account_binding_invalid')
    actual=hashlib.sha256(ANGEL_CLIENT_ID.strip().upper().encode()).hexdigest()
    if not hmac.compare_digest(actual,account): fail('account_mismatch')
    stage,reason='login','login_failed'
    from kadima.session import SESSION
    SESSION.ensure()
    completed=set()
    for method in ('position','orderBook'):
        stage,reason=method,'book_request_failed'
        try:
            items=rows(getattr(SESSION.api,method)())
            if method=='position':
                nonzero,historical_absent=positions(items,(exchange,token,symbol))
                report.update(position_rows=len(items),nonzero_positions=nonzero[:8],
                    nonzero_position_count=len(nonzero),historical_contract_absent=historical_absent,
                    permitted_manual_holding_observed=(ALLOWED in nonzero))
            else:
                pending,details=orders(items)
                report.update(order_rows=len(items),pending_orders=pending,pending_order_details=details)
            completed.add(method)
        except Exception:
            if report['error'] is None: report['error']=error()
    report['books_complete']=completed=={'position','orderBook'}
    if report['books_complete']:
        report['flat']=report['nonzero_position_count']==0 and report['pending_orders']==0
        exposure_ok=report['nonzero_position_count']==0 or (report['nonzero_position_count']==1 and report['permitted_manual_holding_observed'])
        report['readiness']=exposure_ok and report['historical_contract_absent'] and report['pending_orders']==0
        if not report['readiness']:
            if not report['historical_contract_absent']:
                report['error']=dict(stage='position',reason='historical_exposure_present',type='ValueError')
            elif not exposure_ok:
                report['error']=dict(stage='position',reason='unexpected_exposure',type='ValueError')
            else:
                report['error']=dict(stage='orderBook',reason='unresolved_broker_orders',type='ValueError')
except Exception:
    report['error']=error()
report['checked_at']=datetime.now(timezone.utc).isoformat()
print(PREFIX+json.dumps(report,separators=(',',':')))
'''


def safe_position(value):
    require(isinstance(value, dict) and set(value) == set(ALLOWED_HOLDING), 'broker_receipt_invalid')
    require(isinstance(value['exchange'], str) and value['exchange'] in ('NSE', 'NFO', 'BSE', 'BFO', 'MCX', 'NCDEX') and
            isinstance(value['producttype'], str) and value['producttype'] in ('INTRADAY', 'CARRYFORWARD', 'DELIVERY', 'MARGIN', 'BO', 'CO'),
            'broker_receipt_invalid')
    require(isinstance(value['tradingsymbol'], str) and
            re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}', value['tradingsymbol']) and
            isinstance(value['symboltoken'], str) and re.fullmatch(r'[0-9]{1,16}', value['symboltoken']),
            'broker_receipt_invalid')
    require(type(value['netqty']) is int and 0 < abs(value['netqty']) <= 1_000_000_000,
            'broker_receipt_invalid')
    return value


def safe_pending_order(value):
    require(isinstance(value, dict) and set(value) == PENDING_DETAIL_KEYS, 'broker_receipt_invalid')
    require(isinstance(value['exchange'], str) and value['exchange'] in ('NSE', 'NFO', 'BSE', 'BFO', 'MCX', 'NCDEX')
            and isinstance(value['tradingsymbol'], str)
            and re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}', value['tradingsymbol'])
            and isinstance(value['symboltoken'], str) and re.fullmatch(r'[0-9]{1,16}', value['symboltoken']),
            'broker_receipt_invalid')
    require(value['producttype'] is None or (isinstance(value['producttype'], str) and
            value['producttype'] in ('INTRADAY', 'CARRYFORWARD', 'DELIVERY', 'MARGIN', 'BO', 'CO')),
            'broker_receipt_invalid')
    require(value['variety'] is None or (isinstance(value['variety'], str) and
            value['variety'] in ('NORMAL', 'STOPLOSS', 'AMO', 'ROBO')), 'broker_receipt_invalid')
    require(isinstance(value['orderid'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['orderid'])
            and isinstance(value['transactiontype'], str) and value['transactiontype'] in ('BUY', 'SELL')
            and isinstance(value['status'], str) and re.fullmatch(r'[a-z_ -]{1,64}', value['status'])
            and value['status'].strip() == value['status'] and bool(value['status'])
            and value['status'] not in TERMINAL, 'broker_receipt_invalid')
    quantity = value['quantity']
    require(type(quantity) is int and 0 <= quantity <= 1_000_000_000, 'broker_receipt_invalid')
    quantities = []
    for key in ('filled_quantity', 'remaining_quantity'):
        number = value[key]
        require(number is None or (type(number) is int and 0 <= number <= quantity), 'broker_receipt_invalid')
        if number is not None:
            quantities.append(number)
    require(sum(quantities) <= quantity, 'broker_receipt_invalid')
    for key in ('price', 'trigger_price'):
        number = value[key]
        require(number is None or (type(number) in (int, float) and math.isfinite(number)
                and 0 <= number <= 1_000_000_000), 'broker_receipt_invalid')
    return value


def validate_receipt(value, now):
    require(isinstance(value, dict) and set(value) == RECEIPT_KEYS, 'broker_receipt_invalid')
    try:
        started, checked = (datetime.fromisoformat(value[key]) for key in ('started_at', 'checked_at'))
        require(now.tzinfo is not None and started.tzinfo is not None and checked.tzinfo is not None
                and started <= checked <= now and 0 <= (now-started).total_seconds() <= 60
                and 0 <= (now-checked).total_seconds() <= 30, 'broker_receipt_stale')
    except (TypeError, ValueError):
        raise Blocked('broker_receipt_invalid') from None
    for key in ('books_complete', 'readiness', 'permitted_manual_holding_observed'):
        require(type(value[key]) is bool, 'broker_receipt_invalid')
    for key in ('flat', 'historical_contract_absent'):
        require(value[key] is None or type(value[key]) is bool, 'broker_receipt_invalid')
    for key in ('position_rows', 'order_rows', 'pending_orders', 'nonzero_position_count'):
        require(value[key] is None or (type(value[key]) is int and 0 <= value[key] <= 2000),
                'broker_receipt_invalid')
    positions = value['nonzero_positions']
    require(isinstance(positions, list) and len(positions) <= 8, 'broker_receipt_invalid')
    for position in positions:
        safe_position(position)
    require(len({(p['exchange'], p['symboltoken'], p['producttype']) for p in positions}) == len(positions),
            'broker_receipt_invalid')
    pending_details = value['pending_order_details']
    require(isinstance(pending_details, list) and len(pending_details) <= 8, 'broker_receipt_invalid')
    for detail in pending_details:
        safe_pending_order(detail)
    require(len({detail['orderid'] for detail in pending_details}) == len(pending_details),
            'broker_receipt_invalid')
    error = value['error']
    if error is not None:
        require(isinstance(error, dict) and set(error) == {'stage', 'reason', 'type'} and
                all(isinstance(error[key], str) for key in ('stage', 'reason', 'type')) and
                error['stage'] in STAGES and error['reason'] in REASONS and error['type'] in ERROR_TYPES,
                'broker_receipt_invalid')
    if value['position_rows'] is None:
        require(value['nonzero_position_count'] is None and not positions and
                value['historical_contract_absent'] is None and
                value['permitted_manual_holding_observed'] is False, 'broker_receipt_invalid')
    else:
        count = value['nonzero_position_count']
        require(type(count) is int and count <= value['position_rows'] and
                len(positions) == min(count, 8) and type(value['historical_contract_absent']) is bool,
                'broker_receipt_invalid')
    if value['order_rows'] is None:
        require(value['pending_orders'] is None and not pending_details, 'broker_receipt_invalid')
    else:
        require(type(value['pending_orders']) is int and value['pending_orders'] <= value['order_rows'] and
                len(pending_details) == min(value['pending_orders'], 8),
                'broker_receipt_invalid')
    require(value['books_complete'] is (value['position_rows'] is not None and value['order_rows'] is not None),
            'broker_receipt_invalid')
    if value['books_complete']:
        require(all(value[key] is not None for key in
            ('position_rows', 'order_rows', 'pending_orders', 'nonzero_position_count',
             'flat', 'historical_contract_absent')), 'broker_receipt_invalid')
        count = value['nonzero_position_count']
        require(count <= value['position_rows'] and len(positions) == min(count, 8) and
                value['pending_orders'] <= value['order_rows'], 'broker_receipt_invalid')
        permitted = ALLOWED_HOLDING in positions
        # The summary is capped at eight rows. A larger book can include the
        # allowed holding outside that summary, but can never be ready.
        if count <= 8:
            require(value['permitted_manual_holding_observed'] is permitted, 'broker_receipt_invalid')
        elif permitted:
            require(value['permitted_manual_holding_observed'] is True, 'broker_receipt_invalid')
        flat = count == 0 and value['pending_orders'] == 0
        ready = (count == 0 or (count == 1 and permitted)) and value['historical_contract_absent'] and value['pending_orders'] == 0
        require(value['flat'] is flat and value['readiness'] is ready and
                ((error is None) is ready), 'broker_receipt_invalid')
    else:
        require(value['flat'] is None and value['readiness'] is False and error is not None,
                'broker_receipt_invalid')
    return value


def read_broker(python, env, user, target, evidence, *, invoke=subprocess.run, now=None):
    """Caller verifies the stopped service configuration and installed import graph."""
    target, interpreter = Path(target), Path(python)
    require(target.is_absolute() and target.resolve() == target and target.is_dir(),
            'canonical_runtime_directory_required')
    require(interpreter.is_absolute() and interpreter.is_file(), 'reviewed_interpreter_required')
    expected = evidence.get('account_sha256')
    require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected), 'account_binding_required')
    account = env.get('ANGEL_CLIENT_ID')
    require(isinstance(account, str) and account.strip() and hmac.compare_digest(
        hashlib.sha256(account.strip().upper().encode()).hexdigest(), expected), 'account_binding_mismatch')
    exchange, token, symbol = evidence.get('exchange'), str(evidence.get('token', '')), evidence.get('symbol')
    require(exchange == 'BFO' and re.fullmatch(r'[0-9]{1,16}', token) and isinstance(symbol, str) and
            re.fullmatch(r'[A-Z0-9][A-Z0-9&_.+\-]{0,79}', symbol), 'historical_contract_required')
    child_env = dict(env, PYTHONDONTWRITEBYTECODE='1', ORDER_PLACEMENT_ENABLED='0',
        KADIMA_FAST_MOMENTUM_LIVE='0', KADIMA_CAS_LIVE='0', KADIMA_NOTE_ACCOUNT_SHA256=expected,
        KADIMA_HISTORY_EXCHANGE=exchange, KADIMA_HISTORY_TOKEN=token, KADIMA_HISTORY_SYMBOL=symbol)
    try:
        result = invoke([str(interpreter), '-B', '-c', CHILD_CODE], cwd=target, env=child_env,
            user=user.pw_uid, group=user.pw_gid, extra_groups=[], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise Blocked('broker_read_timed_out') from None
    except OSError:
        raise Blocked('broker_reader_unavailable') from None
    require(result.returncode == 0, 'broker_reader_failed')
    require(isinstance(result.stdout, str) and len(result.stdout) <= 65_536, 'broker_output_invalid')
    receipts = [line[len(PREFIX):] for line in result.stdout.splitlines() if line.startswith(PREFIX)]
    require(len(receipts) == 1 and len(receipts[0]) <= 16_384, 'broker_output_invalid')
    try:
        receipt = json.loads(receipts[0])
    except (TypeError, ValueError):
        raise Blocked('broker_output_invalid') from None
    return validate_receipt(receipt, now if now is not None else datetime.now(timezone.utc))
