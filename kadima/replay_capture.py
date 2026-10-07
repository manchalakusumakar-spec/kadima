"""Opt-in observational evidence. No broker calls, execution writes or policy edits.

Uses the existing append-only audit sink; failures never escape into trading.
This is sampled evidence, not a complete market tape or broker trade book.
"""
from dataclasses import asdict, is_dataclass
from datetime import datetime
from functools import wraps
import hashlib
import json
import logging
import marshal
import math
import os
from pathlib import Path
import sys
import threading
import types
import uuid

from . import audit
from .utils import now_ist

MAX_EVENT_BYTES = 1024 * 1024
_lock = threading.RLock()
_run = uuid.uuid4().hex
_seq = 0
_manifest = None
_failures = 0
# Explicitly exclude credentials, account identifiers, paths and arbitrary env.
POLICY_MODULES = {
    'config': ('FAST_MIN_DIRECTIONAL_EFFICIENCY', 'FAST_MIN_BREAKOUT_BODY_FRACTION',
               'FAST_MIN_BREAKOUT_CLOSE_LOCATION', 'FAST_MAX_EXTENSION_TRUE_RANGE',
               'FAST_STOP_BUFFER_TRUE_RANGE', 'MIN_OI', 'MIN_VOLUME', 'MIN_DELTA',
               'MAX_DELTA', 'MAX_SPREAD_PCT', 'MARKET_OPEN', 'MARKET_CLOSE',
               'ORDER_PLACEMENT_ENABLED', 'MAX_CONCURRENT_POSITIONS', 'MAX_LOTS_PER_SIGNAL'),
    'trade_policy': ('EXIT_BUFFER', 'COST_RESERVE'),
    'stock_fast': ('EXIT_HHMM', 'HORIZONS'),
}
QUOTE_FIELDS = ('token', 'symbol', 'strike', 'opt_type', 'ltp', 'bid', 'ask', 'oi',
                'oi_change', 'volume', 'iv', 'delta', 'gamma', 'theta', 'vega')
CHARGE_FIELDS = ('brokerage', 'gst', 'stt', 'transactioncharges', 'stampduty', 'sebiturnovercharges', 'totalcharges')
BROKER_FIELDS = ('orderid', 'ordertag', 'exchangeorderid', 'tradingsymbol', 'symboltoken',
                 'exchange', 'transactiontype', 'producttype', 'ordertype', 'orderstatus',
                 'status', 'quantity', 'filledshares', 'unfilledshares', 'averageprice',
                 'price', 'updatetime', 'exchtime', 'exchorderupdatetime',
                 'fillid', 'filltime', 'fillsize', 'fillprice')


def enabled():
    return os.environ.get('KADIMA_REPLAY_CAPTURE', '0') == '1'


def health():
    with _lock:
        return dict(enabled=enabled(), run_id=_run, attempted=_seq, failures=_failures)


def safe(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        global _failures
        if not enabled():
            return None
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            with _lock:
                _failures += 1
                count = _failures
            if count == 1 or count % 100 == 0:
                try:
                    logging.getLogger(__name__).warning('Replay capture failed: %s (%d)', type(exc).__name__, count)
                except Exception:
                    pass
            return None
    return guarded


def plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            return {'unavailable': 'nonfinite', 'representation': repr(value)}
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict) or hasattr(value, 'keys'):
        return {str(k): plain(value[k]) for k in value.keys()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    raise TypeError('unsupported_evidence_value')


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def identity(row, producer):
    """Match PR20 audit_id for SCALP; never allocate an execution signal."""
    at = row.get('ts')
    episode = row.get('episode')
    if not at or not episode or not all(row.get(k) for k in ('exchange', 'token', 'symbol')):
        return None
    at = datetime.fromisoformat(at)
    if at.tzinfo is None:
        return None
    # Same frozen key as PR20 d584fe0 trade_audit._identity; full contract
    # remains in every event so consumers must reject conflicting identities.
    from .config import IST
    key = '|'.join((at.astimezone(IST).date().isoformat(), 'SCALP', episode[:240],
                    row['exchange'][:16], str(row['token'])[:80]))
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def source_manifest():
    """Disk blobs plus loaded code hashes, not a claim that GitHub HEAD was deployed."""
    root = Path(__file__).parent
    disk = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.py'))}
    loaded = {}
    for name, module in sorted(list(sys.modules.items())):
        if not name.startswith('kadima.') or module is None:
            continue
        hashes = {}
        for key, value in vars(module).copy().items():
            if isinstance(value, types.FunctionType) and value.__module__ == name:
                hashes[key] = hashlib.sha256(marshal.dumps(value.__code__)).hexdigest()
            elif isinstance(value, type) and value.__module__ == name:
                for member, method in vars(value).items():
                    if isinstance(method, (staticmethod, classmethod)):
                        method = method.__func__
                    if isinstance(method, types.FunctionType):
                        hashes[key+'.'+member] = hashlib.sha256(marshal.dumps(method.__code__)).hexdigest()
        loaded[name] = hashes
    return dict(disk_sha256=disk, loaded_code_sha256=loaded, python=sys.version,
                basis='FIRST_CAPTURE_DISK_AND_LOADED_CODE_NOT_GIT_ATTESTATION')


def policy_snapshot():
    out = {}
    for name, keys in POLICY_MODULES.items():
        module = sys.modules.get('kadima.'+name)
        out[name] = {k: plain(getattr(module, k)) for k in keys if module and hasattr(module, k)}
    out['fast_live_requested'] = os.environ.get('KADIMA_FAST_MOMENTUM_LIVE', '0') == '1'
    return out


def emit(kind, payload):
    global _seq, _manifest
    # Called only inside safe(). Freeze to JSON before caller can mutate inputs.
    payload = plain(payload)
    if len(json.dumps(payload, separators=(',', ':')).encode()) > MAX_EVENT_BYTES:
        raise ValueError('replay_event_too_large')
    with _lock:
        if _manifest is None:
            manifest = source_manifest()
            audit.log('REPLAY_RUN', run_id=_run, schema=1, source=manifest,
                      source_id=digest(manifest), process_id=os.getpid())
            _manifest = digest(manifest)
        _seq += 1
        audit.log('REPLAY_'+kind, run_id=_run, sequence=_seq, schema=1,
                  received_at=now_ist().isoformat(), source_id=_manifest, thread_id=threading.get_ident(),
                  capture_failures=_failures, evidence=payload)


@safe
def gate(index, direction, now, score, config, news_health, impact, reasons):
    emit('GATE', dict(index=index, direction=direction, evaluated_at=now, score=score,
        config=config, news_health=news_health, news_impact=impact, blocked_reasons=reasons))


@safe
def structure(index, direction, now, evidence):
    emit('STRUCTURE', dict(index=index, direction=direction, evaluated_at=now, evidence=evidence))


@safe
def decision(producer, row, chain, bars, spot, context=None, phase='SCAN'):
    policy = policy_snapshot()
    emit('DECISION', dict(decision_id=uuid.uuid4().hex, suggestion_id=identity(row, producer),
        producer=producer, phase=phase, decision_at=row.get('ts'), result=row, spot=spot,
        candle_inputs=bars, candle_basis='EXACT_INPUT_ROWS_NOT_RECONSTRUCTED',
        contract_index=vars(chain.index), expiry=chain._expiry,
        quotes=[{k: getattr(q, k, None) for k in QUOTE_FIELDS} for q in chain.quotes.values()],
        context=context, policy=policy, policy_id=digest(policy),
        maximum_hold_minutes=row.get('holding_minutes'),
        maximum_hold_basis='EXPLICIT_STOCK_POLICY' if row.get('holding_minutes') is not None else 'UNAVAILABLE_NO_FIXED_INDEX_POLICY',
        projection_horizon_minutes=(row.get('scenarios') or [{}])[-1].get('minutes'),
        full_replay_ready=False))


@safe
def stock_decision(row, chain, bars, data, controls):
    decision('stock_fast', row, chain, bars, data.get('spot'),
             context=dict(controls=controls, fast_metrics=data.get('fast_metrics'),
                          chain_at=data.get('chain_at'), spot_at=data.get('spot_at')))


@safe
def index_decision(row, state, bars, record):
    decision('index_fast', row, state.chain, bars, state.spot,
             phase='SCAN' if record else 'REVALIDATE')


@safe
def link(row, signal_id):
    producer = 'stock_fast' if row.get('asset_scope') == 'STOCK' else 'index_fast'
    emit('SIGNAL_LINK', dict(suggestion_id=identity(row, producer), signal_id=signal_id,
        episode=row.get('episode'), contract={k: row.get(k) for k in ('exchange', 'token', 'symbol', 'expiry')},
        at=row.get('ts')))


@safe
def quotes(chain):
    emit('QUOTES', dict(underlying=chain.index.name, exchange=chain.index.opt_exchange,
        expiry=chain._expiry, received_at=now_ist().isoformat(),
        basis='ACCEPTED_FULL_POLL_RECEIPT_NOT_EXCHANGE_TIME',
        quotes=[{k: getattr(q, k, None) for k in QUOTE_FIELDS} for q in chain.quotes.values()],
        coverage='CURRENT_LADDER_ONLY_NO_NEW_SUBSCRIPTIONS'))


@safe
def broker(pos, row, status, phase, **delta):
    emit('BROKER', dict(phase=phase, signal_id=pos.signal_id, intent=row['intent'],
        order_id=status.order_id, side=row['side'], exchange=pos.exchange, token=pos.token,
        symbol=pos.symbol, cumulative_qty=status.filled_qty, average_price=status.avg_price,
        status=status.status, dry_run=bool(row['dry_run']),
        broker_fields={k: status.raw[k] for k in BROKER_FIELDS if k in status.raw},
        delta=delta, charges={k: status.raw[k] for k in CHARGE_FIELDS if k in status.raw} or None,
        charges_basis='ORDER_STATUS_FIELDS_ONLY_NOT_RECONCILED',
        fill_basis='CUMULATIVE_ORDER_STATUS_NOT_COMPLETE_TRADE_BOOK'))


@safe
def held_quote(exchange, token, row):
    fields = ('symbolToken', 'tradingSymbol', 'ltp', 'tradeVolume', 'opnInterest',
              'exchFeedTime', 'exchTradeTime', 'depth')
    emit('HELD_QUOTE', dict(exchange=exchange, token=token,
        received_at=now_ist().isoformat(),
        quote={k: row[k] for k in fields if k in row},
        basis='EXISTING_FULL_POSITION_POLL_NOT_CONTINUOUS_TAPE'))
