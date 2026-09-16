#!/usr/bin/env bash
# Paste this entire file into the EC2 terminal where KADIMA runs.
# Checks MCX contracts, access, quotes and a read-only margin calculation. No orders.
# Uses existing service credentials without printing them. Does not log out.
sudo -n python3 - <<'PY'
import json
import os
from pathlib import Path
import pwd
import subprocess

CHECK = r'''
import contextlib
import io
import json
import math
import re
from pathlib import Path
from datetime import datetime
from collections import Counter


def master_rows(path):
    # Bounded streaming decode; do not load the full scrip master in memory.
    decoder = json.JSONDecoder()
    with path.open() as handle:
        buffer = handle.read(65536).lstrip()
        if not buffer.startswith('['): raise ValueError('master_not_array')
        buffer = buffer[1:]
        first = True
        while True:
            buffer = buffer.lstrip()
            while not buffer:
                chunk = handle.read(65536)
                if not chunk: raise ValueError('truncated_master')
                buffer += chunk
                buffer = buffer.lstrip()
            if buffer.startswith(']'): return
            if not first:
                if not buffer.startswith(','): raise ValueError('invalid_master_separator')
                buffer = buffer[1:].lstrip()
            while True:
                try:
                    row, end = decoder.raw_decode(buffer)
                    buffer = buffer[end:]
                    break
                except json.JSONDecodeError:
                    chunk = handle.read(65536)
                    if not chunk or len(buffer) > 1048576: raise ValueError('invalid_master_row')
                    buffer += chunk
                    buffer = buffer.lstrip()
            first = False
            if not isinstance(row, dict): raise ValueError('invalid_master_row')
            yield row


def error_type(exc):
    return type(exc).__name__


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError): return None


def response_summary(response):
    if not isinstance(response, dict): return {'valid_response': False}
    code = str(response.get('errorcode') or '')
    message = str(response.get('message') or '').lower()
    categories = [label for words, label in [
        (('segment', 'exchange'), 'segment_or_exchange'),
        (('permission', 'not enabled', 'not activated', 'unauthor'), 'access'),
        (('token', 'session', 'expired'), 'token_or_session'),
        (('invalid', 'parameter', 'input'), 'request_validation'),
        (('margin', 'fund'), 'margin_or_funds'),
        (('rate limit', 'too many'), 'rate_limit'),
        (('not support', 'unsupported'), 'unsupported'),
        (('market closed',), 'market_closed'),
        (('success',), 'success')
    ] if any(word in message for word in words)]
    return {'valid_response': True, 'status': response.get('status') is True,
            'errorcode': code if re.fullmatch(r'[A-Za-z]{1,6}[0-9]{1,8}', code) else None,
            'message_categories': categories or ['unclassified'],
            'data_type': type(response.get('data')).__name__}


result = {'orders_submitted': 0, 'mcx_trading_enabled_by_this_check': False}
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    try:
        from kadima import audit, config
        from kadima.session import SESSION
        audit.log = lambda *args, **kwargs: None
        now = datetime.now(config.IST)
        result['checked_at'] = now.isoformat()
        SESSION.ensure()
        result['login_ok'] = True
        try:
            profile = SESSION.api.getProfile(SESSION.refresh_token)
            data = profile.get('data') if isinstance(profile, dict) and profile.get('status') is True else None
            exchanges = data.get('exchanges') if isinstance(data, dict) else None
            result['profile_response'] = response_summary(profile)
            if isinstance(exchanges, str):
                exchanges = re.split(r'[,;\s]+', exchanges.strip('[]'))
            labels = sorted({x.strip(" '\"").upper() for x in exchanges
                             if isinstance(x, str) and re.fullmatch(r'[A-Za-z_ -]{2,20}', x.strip(" '\""))}) if isinstance(exchanges, list) else []
            known = {'NSE', 'NSE_CM', 'NSE_FO', 'NFO', 'BSE', 'BSE_CM', 'BSE_FO',
                     'BFO', 'MCX', 'MCX_FO', 'MCXFO', 'CDS', 'NCDEX', 'NCDEX_FO'}
            result['profile_exchange_labels'] = [label for label in labels if label in known]
            result['unrecognized_exchange_label_count'] = sum(label not in known for label in labels)
            result['mcx_listed_in_profile'] = any(x in ('MCX', 'MCX_FO', 'MCXFO') for x in labels) if labels else None
            result['entitlement_note'] = 'Profile labels alone do not verify order entitlement.'
        except Exception as exc: result['profile_error_type'] = error_type(exc)
        try:
            response = SESSION.api.position()
            data = response.get('data') if isinstance(response, dict) and response.get('status') is True else False
            result['positions_read_ok'] = data is None or isinstance(data, list)
            result['mcx_position_metadata'] = []
            for row in data or []:
                if not isinstance(row, dict) or row.get('exchange') != 'MCX': continue
                # Contract identifiers and unit fields only; no client/account identifiers.
                item = {k: str(row.get(k, ''))[:80] for k in ('tradingsymbol', 'symboltoken', 'producttype', 'symbolname')}
                item.update({k: numeric(row.get(k)) for k in ('netqty', 'lotsize', 'multiplier', 'precision', 'pricenum', 'priceden', 'genden', 'gennum')})
                result['mcx_position_metadata'].append(item)
            result['mcx_position_metadata'] = result['mcx_position_metadata'][:12]
        except Exception as exc: result['positions_error_type'] = error_type(exc)
        sample = []
        try:
            master = Path(config.DATA_DIR) / 'scrip_master.json'
            result['master_age_hours'] = round((now.timestamp() - master.stat().st_mtime) / 3600, 2)
            counts = Counter()
            # These are representative contract probes, not an instrument restriction.
            roots = ('CRUDEOIL', 'NATURALGAS', 'GOLD', 'SILVER')
            choices = {}
            for row in master_rows(master):
                if row.get('exch_seg') != 'MCX': continue
                kind = str(row.get('instrumenttype', ''))
                counts[kind] += 1
                if row.get('name') not in roots or row.get('permitted_to_trade') is False: continue
                if kind not in ('FUTCOM', 'OPTFUT'): continue
                try:
                    expiry = datetime.strptime(row['expiry'], '%d%b%Y').date()
                    if expiry < now.date(): continue
                except (KeyError, ValueError, TypeError): continue
                key = row['name'], kind
                # Nearest dated example only; this is NOT an option-underlying mapping.
                ordering = expiry, str(row.get('symbol', ''))
                if key not in choices or ordering < choices[key][0]: choices[key] = ordering, row
            result['mcx_master_types'] = dict(counts)
            for key in sorted(choices):
                row = choices[key][1]
                fields = ('name', 'instrumenttype', 'symbol', 'token', 'expiry', 'lotsize', 'tick_size', 'strike')
                sample.append({k: str(row.get(k, ''))[:80] for k in fields})
            result['mcx_contract_samples'] = sample
            result['sample_note'] = 'Raw broker-master fields; price scale and P&L multiplier still require validation.'
        except Exception as exc: result['master_error_type'] = error_type(exc)
        if sample:
            try:
                tokens = [r['token'] for r in sample]
                response = SESSION.api.getMarketData('FULL', {'MCX': tokens})
                valid = isinstance(response, dict) and response.get('status') is True
                data = response.get('data') if valid else None
                rows = data.get('fetched') if isinstance(data, dict) else None
                result['mcx_quote_response_ok'] = valid and isinstance(rows, list)
                result['mcx_quotes'] = []
                for row in rows or []:
                    if str(row.get('symbolToken')) not in tokens or row.get('exchange', 'MCX') != 'MCX': continue
                    result['mcx_quotes'].append({'token': str(row.get('symbolToken')),
                        'ltp': numeric(row.get('ltp')), 'exchange_feed_time': str(row.get('exchFeedTime', ''))[:60],
                        'exchange_trade_time': str(row.get('exchTradeTime', ''))[:60]})
                result['quote_note'] = 'Quotes can be stale outside the session; success does not prove live freshness.'
            except Exception as exc: result['quote_error_type'] = error_type(exc)
            try:
                future = next(r for r in sample if r['instrumenttype'] == 'FUTCOM')
                qty = int(future['lotsize'])
                if qty <= 0: raise ValueError('invalid_quantity')
                request = {'positions': [{'exchange': 'MCX', 'qty': qty, 'price': 0,
                    'productType': 'INTRADAY', 'token': future['token'], 'tradeType': 'BUY'}]}
                quote = next((r for r in result.get('mcx_quotes', []) if r['token'] == future['token']), {})
                reference = numeric(quote.get('ltp'))
                if reference is not None and reference > 0:
                    request['positions'][0]['price'] = reference
                response = SESSION.api.getMarginApi(request)
                valid = isinstance(response, dict) and response.get('status') is True
                data = response.get('data') if valid else None
                margin = numeric(data.get('totalMarginRequired')) if isinstance(data, dict) else None
                result['mcx_margin_probe'] = {'ok': bool(margin is not None and margin > 0),
                    'symbol': future['symbol'], 'request_qty_from_master': qty,
                    'reference_price': request['positions'][0]['price'],
                    'broker_response': response_summary(response),
                    'total_margin_required': margin, 'order_sent': False}
            except Exception as exc: result['margin_error_type'] = error_type(exc)
        try:
            path = Path('/home/ubuntu/kadima-py/state/universe_snapshot.json')
            snapshot = json.loads(path.read_text())
            result['fno_snapshot'] = {'timestamp': snapshot.get('ts'),
                'tracked_count': len(snapshot.get('tracked') or []),
                'priced_count': len(snapshot.get('prices') or {})}
        except Exception as exc: result['fno_snapshot_error_type'] = error_type(exc)
    except Exception as exc:
        result['diagnostic_error_type'] = error_type(exc)
print('KADIMA_CHECK ' + json.dumps(result))

'''

try:
    pid = subprocess.check_output(['systemctl', 'show', 'kadima.service',
        '--property=MainPID', '--value'], text=True, timeout=10).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError('engine_not_running')
    proc = Path('/proc') / pid
    repo = Path('/home/ubuntu/kadima-py').resolve()
    argv = [os.fsdecode(x) for x in (proc / 'cmdline').read_bytes().split(b'\0') if x]
    args = argv[1:]
    if args and args[0] == '-u':
        args = args[1:]
    if ((proc / 'cwd').resolve() != repo or not argv or
        not Path(argv[0]).is_absolute() or 'python' not in Path(argv[0]).name or
        len(args) != 1 or (repo / args[0]).resolve() != repo / 'run.py'):
        raise RuntimeError('unexpected_engine_process')
    owner = pwd.getpwuid(proc.stat().st_uid)
    if owner.pw_uid == 0:
        raise RuntimeError('expected_non_root_engine')
    env = dict(os.fsdecode(x).split('=', 1) for x in
               (proc / 'environ').read_bytes().split(b'\0') if b'=' in x)
    result = subprocess.run([argv[0], '-c', CHECK], cwd=repo, env=env,
        user=owner.pw_uid, group=owner.pw_gid, extra_groups=[],
        capture_output=True, text=True, timeout=60)
    lines = [line for line in result.stdout.splitlines() if line.startswith('KADIMA_CHECK ')]
    if not lines:
        raise RuntimeError('diagnostic_did_not_complete')
    print(json.dumps(json.loads(lines[-1][len('KADIMA_CHECK '):]), indent=2))
    raise SystemExit(result.returncode)
except Exception as exc:
    print(json.dumps({'check': 'EC2_diagnostic', 'ok': False,
        'error_type': type(exc).__name__, 'orders_submitted': 0}))
    raise SystemExit(1)
PY
