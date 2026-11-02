"""Durable sampled price references, isolated from execution and broker I/O.

Callbacks coalesce into bounded memory; one background writer per process uses a
separate SQLite WAL database. Opening references require a fresh exchange time.
Missing minutes remain missing. Historical records never authorize an entry.
"""
from __future__ import annotations

import atexit
import csv
from functools import wraps
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from .config import DATA_DIR, IST, MARKET_OPEN, MARKET_CLOSE
from .utils import now_ist

MAX_PENDING = 4096
FLUSH_SECONDS = 2
MINUTE_DAYS = 7
DAILY_DAYS = 180
MAX_DB_BYTES = 1024 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
ASSETS = ('STOCK', 'INDEX', 'OPTION')
EXCHANGES = ('NSE', 'BSE', 'NFO', 'BFO', 'MCX')
BAR_FIELDS = ('first_at', 'last_at', 'first', 'last', 'high', 'high_at',
              'low', 'low_at', 'first_volume', 'last_volume', 'volume_reset',
              'samples', 'sources', 'max_gap_seconds')
_writer = None
_writer_lock = threading.Lock()


def observational(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            logging.getLogger(__name__).warning('Price history adapter unavailable: %s', type(exc).__name__)
            return False
    return guarded


def database_path():
    return Path(DATA_DIR) / 'price_history.db'


def stamp(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value.astimezone(IST) if isinstance(value, datetime) and value.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def number(value, positive=True):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and (value > 0 if positive else value >= 0) else None
    except (ValueError, TypeError, OverflowError):
        return None


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, IST).isoformat() if ms is not None else None


def identity(exchange, token, symbol):
    return hashlib.sha256((exchange+'|'+token+'|'+symbol).encode()).hexdigest()[:32]


def normalize(row, now=None):
    now = stamp(now or now_ist())
    received, at = stamp(row.get('received_at')), stamp(row.get('at'))
    if (now is None or received is None or at is None or received.date() != at.date()
            or not 0 <= (now-received).total_seconds() <= 30
            or not 0 <= (received-at).total_seconds() <= 30):
        return None
    exchange, token, symbol = row.get('exchange'), row.get('token'), row.get('symbol')
    if (exchange not in EXCHANGES or row.get('asset') not in ASSETS
            or not all(isinstance(x, str) and 0 < len(x) <= 100 and
                       all(ord(c) >= 32 for c in x) for x in (token, symbol))):
        return None
    opening, closing = ((9, 0), (23, 59)) if exchange == 'MCX' else (MARKET_OPEN, MARKET_CLOSE)
    if not opening <= (at.hour, at.minute) <= closing:
        return None
    price = number(row.get('price'))
    if price is None or row.get('basis') not in ('EXCHANGE', 'RECEIVED'):
        return None
    ms = int(at.timestamp()*1000)
    volume = number(row.get('volume'), False)
    trusted = row['basis'] == 'EXCHANGE'
    result = dict(day=at.date().isoformat(), key=identity(exchange, token, symbol),
                  exchange=exchange, token=token, symbol=symbol,
                  underlying=str(row.get('underlying') or symbol)[:100], asset=row['asset'],
                  expiry=str(row.get('expiry') or '')[:30], strike=number(row.get('strike')),
                  side=str(row.get('side') or '')[:2], minute=ms//60000*60000,
                  first_at=ms, last_at=ms, first=price, last=price,
                  high=price, high_at=ms, low=price, low_at=ms,
                  first_volume=volume, last_volume=volume, volume_reset=False,
                  samples=1, sources=1 if trusted else 2, max_gap_seconds=0.,
                  opening_price=number(row.get('opening_price')) if trusted else None,
                  previous_close=number(row.get('previous_close')) if trusted else None,
                  reference_at=ms if trusted else None, reference_conflict=False)
    return result


def merge(a, b):
    """Commutative extrema/first/latest merge; called inside the writer transaction."""
    if a is None:
        return dict(b)
    out = dict(a)
    earlier, later = (a, b) if a['first_at'] <= b['first_at'] else (b, a)
    if b['first_at'] < a['first_at']:
        for field in ('first_at', 'first', 'first_volume'):
            out[field] = b[field]
    if b['last_at'] > a['last_at']:
        for field in ('last_at', 'last', 'last_volume'):
            out[field] = b[field]
    for value, at, best in (('high', 'high_at', max), ('low', 'low_at', min)):
        extreme = best(a[value], b[value])
        out[value] = extreme
        out[at] = min(x[at] for x in (a, b) if x[value] == extreme)
    out['volume_reset'] = bool(a['volume_reset'] or b['volume_reset'] or
        (earlier['last_at'] <= later['first_at'] and earlier['last_volume'] is not None
         and later['first_volume'] is not None and earlier['last_volume'] > later['first_volume']))
    out['samples'] = a['samples']+b['samples']
    out['sources'] = a['sources'] | b['sources']
    out['max_gap_seconds'] = max(a['max_gap_seconds'], b['max_gap_seconds'],
                               (later['first_at']-earlier['last_at'])/1000)
    if 'opening_price' in a:
        out['reference_conflict'] = bool(a['reference_conflict'] or b['reference_conflict'])
        for field in ('opening_price', 'previous_close'):
            av, bv = a.get(field), b.get(field)
            if av is not None and bv is not None and not math.isclose(av, bv, rel_tol=1e-9):
                out['reference_conflict'] = True
            if av is None and bv is not None:
                out[field] = bv
                out['reference_at'] = b['reference_at']
    return out


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=.1)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=FULL')
    c.execute('PRAGMA cache_size=-2048')
    c.execute('''CREATE TABLE IF NOT EXISTS price_days (
        day TEXT NOT NULL, key TEXT NOT NULL, exchange TEXT NOT NULL, token TEXT NOT NULL,
        symbol TEXT NOT NULL, underlying TEXT NOT NULL, asset TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(day,key)) WITHOUT ROWID''')
    c.execute('''CREATE TABLE IF NOT EXISTS price_minutes (
        day TEXT NOT NULL, key TEXT NOT NULL, minute INTEGER NOT NULL,
        first_at INTEGER, last_at INTEGER, first REAL, last REAL,
        high REAL, high_at INTEGER, low REAL, low_at INTEGER,
        first_volume REAL, last_volume REAL, volume_reset INTEGER,
        samples INTEGER, sources INTEGER, max_gap_seconds REAL,
        PRIMARY KEY(day,key,minute)) WITHOUT ROWID''')
    c.execute('''CREATE TABLE IF NOT EXISTS price_writers (
        writer TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    return c


class Recorder:
    def __init__(self, path=None, *, max_pending=MAX_PENDING):
        self.path = Path(path or database_path())
        self.max_pending = max_pending
        self.pending = {}
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.accepted = self.dropped = self.failures = self.rejected = 0
        self.last_success = None
        self.last_error = None
        self._maintenance_day = None
        self.writer_id = str(os.getpid())+'-'+str(time.time_ns())

    def submit(self, row, now=None):
        sample = normalize(row, now)
        with self.lock:
            if sample is None:
                self.rejected += 1
                return False
            key = (sample['day'], sample['key'], sample['minute'])
            if key not in self.pending and len(self.pending) >= self.max_pending:
                self.dropped += 1
                return False
            self.pending[key] = merge(self.pending.get(key), sample)
            self.accepted += 1
        return True

    def status(self):
        with self.lock:
            return dict(accepted=self.accepted, dropped=self.dropped, rejected=self.rejected,
                        failures=self.failures, pending_minutes=len(self.pending),
                        last_success=self.last_success, last_error=self.last_error)

    def flush(self, now=None):
        now = stamp(now or now_ist())
        with self.flush_lock:
            with self.lock:
                batch, self.pending = self.pending, {}
            c = None
            try:
                c = connect(self.path)
                if shutil.disk_usage(self.path.parent).free < MIN_FREE_BYTES:
                    raise RuntimeError('history_disk_space_low')
                c.execute('BEGIN IMMEDIATE')
                if self._maintenance_day != now.date():
                    c.execute('DELETE FROM price_minutes WHERE day<?',
                              ((now.date()-timedelta(days=MINUTE_DAYS-1)).isoformat(),))
                    c.execute('DELETE FROM price_days WHERE day<?',
                              ((now.date()-timedelta(days=DAILY_DAYS-1)).isoformat(),))
                    c.execute('DELETE FROM price_writers WHERE updated_at<?',
                              ((now-timedelta(days=2)).isoformat(),))
                pages = c.execute('PRAGMA page_count').fetchone()[0] - c.execute('PRAGMA freelist_count').fetchone()[0]
                if pages*c.execute('PRAGMA page_size').fetchone()[0] >= MAX_DB_BYTES:
                    raise RuntimeError('history_storage_budget_reached')
                for (day, key, minute), sample in sorted(batch.items()):
                    old = c.execute('SELECT payload FROM price_days WHERE day=? AND key=?', (day,key)).fetchone()
                    daily = merge(json.loads(old[0]) if old else None, sample)
                    c.execute('INSERT OR REPLACE INTO price_days VALUES (?,?,?,?,?,?,?,?)',
                              (day,key,daily['exchange'],daily['token'],daily['symbol'],daily['underlying'],
                               daily['asset'],json.dumps(daily, separators=(',', ':'), allow_nan=False)))
                    old = c.execute('SELECT * FROM price_minutes WHERE day=? AND key=? AND minute=?',
                                    (day,key,minute)).fetchone()
                    bar = merge(dict(old) if old else None, {f:sample[f] for f in BAR_FIELDS})
                    values = (day,key,minute,*(bar[f] for f in BAR_FIELDS))
                    c.execute('INSERT OR REPLACE INTO price_minutes VALUES ('+','.join('?' for _ in values)+')', values)
                status = dict(self.status(), last_success=now.isoformat(), last_error=None)
                c.execute('INSERT OR REPLACE INTO price_writers VALUES (?,?,?)',
                          (self.writer_id, now.isoformat(), json.dumps(status)))
                c.commit()
                self.last_success, self.last_error = now.isoformat(), None
                self._maintenance_day = now.date()
                return True
            except Exception as exc:
                if c is not None:
                    c.rollback()
                with self.lock:
                    self.failures += 1
                    self.last_error = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                    for key, sample in batch.items():
                        if key in self.pending or len(self.pending) < self.max_pending:
                            self.pending[key] = merge(sample, self.pending.get(key)) if key in self.pending else sample
                        else:
                            self.dropped += sample['samples']
                if self.failures == 1 or self.failures % 100 == 0:
                    logging.getLogger(__name__).warning('Price history unavailable: %s', self.last_error)
                return False
            finally:
                if c is not None:
                    c.close()

    def start(self):
        if self.thread is None:
            def run():
                while not self.stop_event.wait(FLUSH_SECONDS):
                    self.flush()
                self.flush()
            self.thread = threading.Thread(target=run, name='price-history-writer', daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        else:
            self.flush()


def capture(row, now=None):
    """Never propagate a recording failure into a scanner or an order exit."""
    global _writer
    if os.environ.get('KADIMA_PRICE_HISTORY_DISABLED') == '1':
        return False
    try:
        with _writer_lock:
            if _writer is None:
                _writer = Recorder()
                _writer.start()
                atexit.register(_writer.close)
        return _writer.submit(row, now)
    except Exception as exc:
        logging.getLogger(__name__).warning('Price history callback unavailable: %s', type(exc).__name__)
        return False


def broker_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
    except ValueError:
        return stamp(value)


@observational
def full_quote(row, *, exchange, token, symbol, underlying, asset, expiry='', strike=None, side='', now=None):
    """Use mapped identity, never a possibly abbreviated FULL tradingSymbol."""
    now = now or now_ist()
    if row.get('exchange', exchange) != exchange or str(row.get('symbolToken')) != str(token):
        return False
    received = stamp(row.get('_received_at')) or stamp(now)
    at = broker_time(row.get('exchFeedTime'))
    if row.get('exchFeedTime') and at is None:
        return False
    return capture(dict(exchange=exchange, token=str(token), symbol=symbol,
        underlying=underlying, asset=asset, expiry=expiry, strike=strike, side=side,
        at=at or received, received_at=received, basis='EXCHANGE' if at else 'RECEIVED',
        price=row.get('ltp'), volume=row.get('tradeVolume'),
        opening_price=row.get('open'), previous_close=row.get('close')), now)


@observational
def websocket(message, *, exchange, token, symbol, underlying, asset, now=None):
    now = now or now_ist()
    raw = number(message.get('exchange_timestamp'))
    if raw is None:
        return False
    try:
        at = datetime.fromtimestamp(raw/1000, IST)
    except (ValueError, OverflowError, OSError):
        return False
    def price(key):
        value = number(message.get(key))
        return value/100 if value is not None else None
    return capture(dict(exchange=exchange, token=str(token), symbol=symbol,
        underlying=underlying, asset=asset, at=at, received_at=now, basis='EXCHANGE',
        price=price('last_traded_price'), volume=message.get('volume_trade_for_the_day'),
        opening_price=price('open_price_of_the_day'), previous_close=price('closed_price')), now)


def _read(path=None):
    path = Path(path or database_path())
    if not path.exists():
        return None
    c = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    c.execute('BEGIN')
    return c


def percent(price, reference):
    return round((price/reference-1)*100, 4) if reference else None


def public_day(d, now):
    out = dict(d)
    for field in ('first_at', 'last_at', 'high_at', 'low_at', 'reference_at'):
        out[field] = iso(d.get(field))
    valid = not d['reference_conflict']
    out.update(move_from_open_pct=percent(d['last'],d['opening_price']) if valid else None,
               move_from_first_pct=percent(d['last'],d['first']),
               move_from_previous_close_pct=percent(d['last'],d['previous_close']) if valid else None,
               opening_gap_pct=percent(d['opening_price'],d['previous_close']) if valid and d['opening_price'] else None,
               reference_basis='BROKER_REPORTED_OPEN' if d['opening_price'] else 'OPEN_UNAVAILABLE',
               coverage='SAMPLED', freshness='RECENT_SAMPLE' if 0 <= now.timestamp()*1000-d['last_at'] <= 30000 else 'STALE')
    return out


def filters(day, asset, symbol, limit, offset):
    day = day or now_ist().date().isoformat()
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('invalid_price_day') from None
    if asset not in (None, '', 'ALL', *ASSETS) or not isinstance(symbol, str) or len(symbol)>80:
        raise ValueError('invalid_price_filter')
    if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or not 0<=offset<=100000:
        raise ValueError('invalid_price_page')
    where, values = ['day=?'], [day]
    if asset in ASSETS:
        where.append('asset=?'); values.append(asset)
    if symbol:
        where.append('(instr(upper(symbol),?)>0 OR instr(upper(underlying),?)>0)')
        values.extend([symbol.upper(),symbol.upper()])
    return day, ' AND '.join(where), values


def report(day=None, asset=None, symbol='', limit=50, offset=0, *, path=None, now=None):
    now = stamp(now or now_ist())
    day, where, values = filters(day,asset,symbol,limit,offset)
    c = _read(path)
    rows, total, writers = [], 0, []
    try:
        if c:
            total = c.execute('SELECT count(*) FROM price_days WHERE '+where, values).fetchone()[0]
            for saved in c.execute('SELECT payload FROM price_days WHERE '+where+' ORDER BY asset,symbol,key LIMIT ? OFFSET ?', (*values,limit,offset)):
                row = public_day(json.loads(saved[0]),now)
                row['minutes_recorded'] = c.execute('SELECT count(*) FROM price_minutes WHERE day=? AND key=?', (day,row['key'])).fetchone()[0]
                rows.append(row)
            writers = [dict(updated_at=r['updated_at'],**json.loads(r['payload']))
                       for r in c.execute('SELECT * FROM price_writers ORDER BY updated_at DESC LIMIT 20')]
    finally:
        if c: c.close()
    return dict(ts=now.isoformat(),day=day,rows=rows,total=total,limit=limit,offset=offset,
                has_more=offset+len(rows)<total,writers=writers,
                capture_enabled=os.environ.get('KADIMA_PRICE_HISTORY_DISABLED')!='1',
                local_writer=_writer.status() if _writer else None,
                retention=dict(minute_calendar_days=MINUTE_DAYS,daily_calendar_days=DAILY_DAYS),
                notes=['Sampled coverage only; missing minutes and unobserved instruments are not reconstructed.',
                       'Opening references require fresh exchange-dated quotes; first observed is a separate price.',
                       'Reference conflicts suppress reference-based percentages. Historical data cannot authorize orders.'])


def history(day, key, *, path=None, now=None):
    now = stamp(now or now_ist())
    filters(day,None,'',50,0)
    if not isinstance(key,str) or len(key)!=32 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('invalid_price_identity')
    c = _read(path)
    bars, daily = [], None
    try:
        if c:
            saved = c.execute('SELECT payload FROM price_days WHERE day=? AND key=?',(day,key)).fetchone()
            daily = public_day(json.loads(saved[0]),now) if saved else None
            for saved in c.execute('SELECT * FROM price_minutes WHERE day=? AND key=? ORDER BY minute LIMIT 1440',(day,key)):
                b = dict(saved)
                b['volume_delta'] = (b['last_volume']-b['first_volume'] if not b['volume_reset']
                    and b['first_volume'] is not None and b['last_volume'] is not None
                    and b['last_volume']>=b['first_volume'] else None)
                b['complete_minute_elapsed'] = b['minute']+60000 <= now.timestamp()*1000
                bars.append(b)
    finally:
        if c: c.close()
    # Close-to-close sampled returns require every intervening minute and no
    # observed gap over 30s. They never reuse a cached entry decision on restart.
    completed = {b['minute']:b for b in bars if b['complete_minute_elapsed']}
    returns = {}
    if completed:
        last = completed[max(completed)]
        for n in (1,3,5,15):
            seq = [completed.get(last['minute']-i*60000) for i in range(n+1)]
            good = all(x and x['max_gap_seconds']<=30 for x in seq)
            good = good and all(seq[i]['first_at']-seq[i+1]['last_at']<=30000 for i in range(n))
            returns[str(n)] = percent(last['last'],seq[-1]['last']) if good else None
    returns_asof = iso(completed[max(completed)]['last_at']) if completed else None
    for b in bars:
        for field in ('minute','first_at','last_at','high_at','low_at'):
            b[field] = iso(b[field])
    return dict(day=day,key=key,reference=daily,bars=bars,sampled_close_returns_pct=returns,
                returns_asof=returns_asof,
                notes=['Bars contain observed prices, not certified exchange OHLC.',
                       'Volume delta covers first-to-last samples within the minute only; missing intervals stay missing.'])


def export(day=None, asset=None, symbol='', *, path=None):
    day, where, values = filters(day,asset,symbol,200,0)
    c = _read(path)
    rows = []
    try:
        if c:
            count = c.execute('SELECT count(*) FROM price_days WHERE '+where,values).fetchone()[0]
            if count>10000:
                raise ValueError('narrow_price_export_filters_to_10000_records')
            now = now_ist()
            for saved in c.execute('SELECT payload FROM price_days WHERE '+where+' ORDER BY asset,symbol,key',values):
                row = public_day(json.loads(saved[0]),now)
                row['minutes_recorded'] = c.execute('SELECT count(*) FROM price_minutes WHERE day=? AND key=?',(day,row['key'])).fetchone()[0]
                rows.append(row)
    finally:
        if c: c.close()
    out = io.StringIO(newline='')
    keys = ('day','exchange','token','symbol','underlying','asset','opening_price','previous_close',
            'reference_at','reference_conflict','first','first_at','last','last_at','high','high_at',
            'low','low_at','move_from_open_pct','move_from_first_pct','move_from_previous_close_pct',
            'opening_gap_pct','minutes_recorded','max_gap_seconds','coverage','freshness')
    writer = csv.writer(out); writer.writerow(keys)
    for row in rows:
        writer.writerow([("'"+str(row.get(k)) if isinstance(row.get(k),str) and row[k].lstrip().startswith(('=','+','-','@')) else row.get(k)) for k in keys])
    return dict(filename='kadima_price_history_'+day+'.csv',content=out.getvalue(),total=len(rows))
