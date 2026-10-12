"""Additive observation boundary. No broker imports, subscriptions or fitting.

The composed host must supply a lock respected by every cache writer. A private
observer lock cannot make an uncooperative trading cache safe. Legacy hosts fail
closed rather than claiming atomicity.
"""
from copy import deepcopy
from pathlib import Path
from queue import Queue, Full, Empty
import sqlite3
import time
from types import SimpleNamespace
from threading import Lock
from .core import EvidenceError, instant
from .cache_adapter import capture, ENRICHMENT, MAX_BARS


class CandleReader:
    def __init__(self, path, *, budget_seconds=.1):
        self.path = Path(path).resolve(strict=True)
        if not 0 < budget_seconds <= .25:
            raise EvidenceError('reader budget must be <=250ms')
        self.budget = budget_seconds

    def __call__(self, token, limit, until):
        if not isinstance(limit, int) or not 1 <= limit <= MAX_BARS:
            raise EvidenceError('invalid bounded candle limit')
        end = instant(until)
        deadline = time.monotonic() + self.budget
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True,
                             timeout=self.budget)
        try:
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
            db.row_factory = sqlite3.Row
            # The native primary key bounds the token/interval range. Complete
            # rows only; no initialization, migration or history backfill.
            rows = db.execute('SELECT interval,start_ts,open,high,low,close,closed '
                'FROM candles WHERE token=? AND interval=5 AND closed=1 '
                'AND start_ts<? ORDER BY start_ts DESC LIMIT ?',
                (str(token), end.isoformat(), limit)).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            db.close()


def frozen_engine(engine, writer_lock):
    if writer_lock is None:
        raise EvidenceError('verified cache-writer lock required')
    if not writer_lock.acquire(timeout=.01):
        raise EvidenceError('cache busy; observation skipped')
    try:
        states = {}
        for name in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            st = engine.states.get(name)
            if st is not None:
                states[name] = SimpleNamespace(spot=st.spot,
                    last_updated=st.last_updated, is_live_tick=st.is_live_tick,
                    chain=SimpleNamespace(index=SimpleNamespace(token=str(st.chain.index.token))))
        radar=getattr(engine, 'constituent_radar', None)
    finally:
        writer_lock.release()
    copied=None
    if radar is not None:
        if not radar.lock.acquire(timeout=.01):
            raise EvidenceError('radar busy; observation skipped')
        try:
            if len(radar.members)>50 or len(radar.states)>50:
                raise EvidenceError('radar cache exceeds all-50 bound')
            members={token:{'symbol':m['symbol'],'weight':m['weight']}
                     for token,m in radar.members.items()}
            radar_states={}
            for token,st in radar.states.items():
                if len(st.get('samples',()))>62:
                    raise EvidenceError('radar history exceeds 62 samples')
                radar_states[token]={'price':st['price'],'ts':st['ts'],
                    'samples':[tuple(p) for p in st.get('samples',())]}
            copied=SimpleNamespace(lock=Lock(),reason=radar.reason,source=radar.source,
                valid_from=radar.valid_from,valid_until=radar.valid_until,
                session=radar.session,members=members,states=radar_states)
        finally:
            radar.lock.release()
    return SimpleNamespace(states=states,constituent_radar=copied)


def map_enrichments(records):
    """Accept explicit per-feature producer evidence, never infer from _last_poll.

    Native host fields without independent timestamps remain absent. Values
    must already have declared expiry/contract scope (or VWAP volume mapping).
    encode() applies the feature-specific causal/freshness checks later.
    """
    result = {}
    for name in ENRICHMENT:
        raw = records.get(name)
        if raw is None:
            result[name] = {'status': 'MISSING'}
            continue
        allowed={'status','value','observed_at','available_at','source',
                 'contract_scope','volume_instrument','mapping'}
        if not isinstance(raw,dict) or set(raw)-allowed:
            raise EvidenceError('bounded primitive enrichment mapping required')
        if any(not isinstance(v,(str,int,float,bool,type(None))) or
               (isinstance(v,str) and len(v)>512) for v in raw.values()):
            raise EvidenceError('enrichment exceeds primitive bounds')
        row = dict(raw)
        if row.get('status') == 'AVAILABLE':
            observed, available = instant(row['observed_at']), instant(row['available_at'])
            if observed > available:
                raise EvidenceError('feature receipt precedes event')
            if name != 'vwap_proxy_gap_bps' and not row.get('contract_scope'):
                raise EvidenceError('contract scope required')
            if name == 'vwap_proxy_gap_bps' and not (row.get('volume_instrument') and row.get('mapping')):
                raise EvidenceError('declared volume source and mapping required')
        result[name] = row
    return result


class ObservationHook:
    """Invoke observe() ONLY from the existing forecast worker, never tick/order.

    Bounded queue drops observations under load. Consumer owns its journal and
    runs feature computation separately; publication remains gated. No model means CAPTURE_ONLY.
    """
    def __init__(self):
        self.queue = Queue(maxsize=1)
        self.dropped = 0

    def observe(self, engine, writer_lock, records=None):
        if self.queue.full():
            self.dropped += 1
            return False
        item = (frozen_engine(engine, writer_lock), map_enrichments(records or {}))
        try:
            self.queue.put_nowait(item)
            return True
        except Full:
            self.dropped += 1
            return False

    def consume(self, reader, manifest, *, source_id, config_id, clock,
                journal, save_packet, model=None):
        try:
            engine, enrichments = self.queue.get_nowait()
        except Empty:
            return {'state': 'WAITING_FOR_OBSERVATION'}
        packet = capture(engine, reader, manifest, source_id=source_id,
                         config_id=config_id, clock=clock, enrichments=enrichments)
        save_packet(deepcopy(packet))
        if model is None:
            return {'state': 'CAPTURE_ONLY', 'snapshot_id': packet['id'],
                    'fitted_live_candidate': False}
        raise EvidenceError('live model publication unavailable: eligible real-market fit and publication receipt required')
