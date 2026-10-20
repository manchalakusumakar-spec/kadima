"""Persistent, read-only trading review. Never sends an order or changes a gate.

Plans are frozen on first observation. Actuals are projected from the existing
cumulative-fill ledger, excluding dry runs. Fill times are engine confirmation
times, not exchange execution timestamps. Quote outcomes are sampled observations.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
import threading
from datetime import date, datetime, timedelta, timezone

from . import storage
from .utils import now_ist

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGIES = {
    'INTRADAY': 'Intraday / Shield', 'GAMMA': 'Gamma Blast', 'CAS': 'CAS',
    'SCALP': 'Scalping', 'SCALP_V2': 'Scalping v2', 'MOVE_CAPTURE': 'Move Capture', 'GAP_AND_GO': 'Gap-and-Go',
    'MCX': 'MCX', 'MANUAL': 'Manual', 'UNKNOWN': 'Unknown',
}
ALIASES = {'bull_shield': 'INTRADAY', 'bear_shield': 'INTRADAY',
           'gamma_blast': 'GAMMA', 'cas': 'CAS', 'cas_blast': 'CAS',
           'cas_premium': 'CAS', 'cas_premium_awakening': 'CAS',
           'fast_momentum': 'SCALP', 'index_scalp': 'SCALP', 'index_scalper': 'SCALP',
           'move_capture': 'MOVE_CAPTURE', 'gap_and_go': 'GAP_AND_GO',
           'mcx': 'MCX', 'mcx_live': 'MCX', 'mcx_momentum': 'MCX',
           'MANUAL_ADOPTED': 'MANUAL', 'MANUAL_PROTECTED': 'MANUAL'}
STATUSES = ('SUGGESTED', 'PENDING', 'OPEN', 'CLOSED', 'UNFILLED', 'UNKNOWN', 'PAPER')
_health_lock = threading.Lock()
_health = {'failures': 0, 'last_failure_at': None, 'last_success_at': None}


def strategy_code(kind):
    return kind if kind in STRATEGIES else ALIASES.get(kind, 'UNKNOWN')


def number(value, minimum=0):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= minimum else None


def stamp(value):
    try:
        d = datetime.fromisoformat(value) if isinstance(value, str) else value
        return d.astimezone(IST) if isinstance(d, datetime) and d.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def text(value, maximum=160):
    return value[:maximum] if isinstance(value, str) else ''


def connection():
    c = storage.conn()
    # No executescript: it implicitly commits a caller's transaction.
    for sql in (
        '''CREATE TABLE IF NOT EXISTS trade_audit_plans (
          audit_id TEXT PRIMARY KEY, day TEXT NOT NULL, strategy TEXT NOT NULL,
          producer TEXT NOT NULL, episode TEXT NOT NULL, signal_id INTEGER,
          exchange TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT NOT NULL,
          underlying TEXT NOT NULL, direction TEXT, suggested_at TEXT NOT NULL,
          origin TEXT NOT NULL, score REAL, plan TEXT NOT NULL,
          stage TEXT, blockers TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          ltp REAL, quote_at TEXT, high_ltp REAL, t1_at TEXT, t2_at TEXT, sl_at TEXT)''',
        'CREATE INDEX IF NOT EXISTS trade_audit_day ON trade_audit_plans(day,strategy,suggested_at,audit_id)',
        'CREATE INDEX IF NOT EXISTS trade_audit_contract ON trade_audit_plans(day,exchange,token)',
        'CREATE UNIQUE INDEX IF NOT EXISTS trade_audit_signal ON trade_audit_plans(signal_id) WHERE signal_id IS NOT NULL',
        '''CREATE TABLE IF NOT EXISTS trade_audit_orders (
          intent TEXT PRIMARY KEY, signal_id INTEGER NOT NULL, side TEXT NOT NULL,
          limit_price REAL NOT NULL, quantity INTEGER NOT NULL, recorded_at TEXT NOT NULL)''',
    ):
        c.execute(sql)
    c.execute('CREATE INDEX IF NOT EXISTS trade_audit_orders_signal ON trade_audit_orders(signal_id,recorded_at)')
    return c


def safe_call(function, *args, **kwargs):
    """An observational failure must not suppress an existing-position exit."""
    c, busy = None, None
    try:
        c = storage.conn()
        if c.in_transaction:
            raise RuntimeError('audit_capture_inside_execution_transaction')
        busy = c.execute('PRAGMA busy_timeout').fetchone()[0]
        c.execute('PRAGMA busy_timeout=50')
        result = function(*args, **kwargs)
        with _health_lock:
            _health['last_success_at'] = now_ist().isoformat()
        return result
    except Exception as exc:
        with _health_lock:
            _health['failures'] += 1
            _health['last_failure_at'] = now_ist().isoformat()
            count = _health['failures']
        if count == 1 or count % 100 == 0:
            logging.getLogger(__name__).warning('Trade audit capture unavailable: %s (count %d)', type(exc).__name__, count)
        return None
    finally:
        if c is not None and busy is not None:
            try:
                c.execute('PRAGMA busy_timeout='+str(int(busy)))
            except Exception:
                pass  # A closed/broken audit connection must not block an exit.


def capture_health():
    with _health_lock:
        return dict(_health)


def _identity(row, strategy, at):
    exchange, token = text(row.get('exchange'), 16), text(str(row.get('token') or ''), 80)
    symbol = text(row.get('symbol') or row.get('contract_symbol'))
    if not exchange or not token or not symbol:
        return None
    episode = text(row.get('episode'), 240) or ('contract:' + exchange + ':' + token)
    key = '|'.join((at.date().isoformat(), strategy, episode, exchange, token))
    return hashlib.sha256(key.encode()).hexdigest()[:32], exchange, token, symbol, episode


def record(row, strategy=None, producer='engine', signal_id=None, origin='SUGGESTION'):
    """Capture a selected contract, even when blocked. Repeated polls never rewrite its plan."""
    if not isinstance(row, dict):
        return None
    at = stamp(row.get('ts') or row.get('last_updated'))
    if at is None:
        return None
    strategy = strategy_code(strategy or row.get('source_strategy') or row.get('strategy'))
    identity = _identity(row, strategy, at)
    if identity is None:
        return None
    aid, exchange, token, symbol, episode = identity
    sid = signal_id if signal_id is not None else row.get('signal_id')
    if sid is not None and (type(sid) is not int or sid <= 0):
        raise ValueError('audit_invalid_signal')
    entry = number(row.get('entry'), .000001)
    if strategy == 'SCALP' and origin == 'SUGGESTION' and entry is None:
        # Warm-up rows may identify a contract before a plan exists. Freezing
        # those would permanently hide the first complete plan for the episode.
        return None
    reference = number(row.get('option_ltp') if 'option_ltp' in row else row.get('ltp'), .000001)
    hold = number(row.get('holding_minutes'), .000001)
    plan = dict(entry=entry, entry_reference=reference,
                entry_basis='PROPOSED' if entry is not None else 'LTP_REFERENCE' if reference is not None else 'UNAVAILABLE',
                exit=number(row.get('planned_exit'), .000001), stop=number(row.get('stop'), .000001),
                t1=number(row.get('t1'), .000001), t2=number(row.get('t2'), .000001),
                holding_minutes=hold, holding_basis='MAXIMUM_HOLD_POLICY' if hold is not None else 'UNAVAILABLE',
                expiry=text(row.get('expiry'), 30), strike=number((row['strike'].get('strike') if isinstance(row.get('strike'), dict) else row.get('strike')) or row.get('best_strike'), .000001),
                session_exit=stamp(row.get('session_exit')).isoformat() if stamp(row.get('session_exit')) else None)
    # Long-premium observations only; malformed barriers are not target evidence.
    if entry is None or (plan['stop'] is not None and plan['stop'] >= entry):
        plan['stop'] = None
    for key in ('t1', 't2'):
        if entry is None or (plan[key] is not None and plan[key] <= entry):
            plan[key] = None
    if plan['t1'] is not None and plan['t2'] is not None and plan['t2'] <= plan['t1']:
        plan['t2'] = None
    c = connection()
    if sid is not None:
        episode, _ = _episode_for_signal(c, at.date().isoformat(), strategy, episode,
                                        exchange, token, sid)
        aid = _identity(dict(row, episode=episode), strategy, at)[0]
    blockers = json.dumps([text(x, 100) for x in row.get('blocked_reasons', []) if isinstance(x, str)][:30])
    with c:
        c.execute('''INSERT OR IGNORE INTO trade_audit_plans
          (audit_id,day,strategy,producer,episode,exchange,token,symbol,underlying,direction,
           suggested_at,origin,score,plan,stage,blockers,last_seen_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (aid, at.date().isoformat(), strategy, text(producer, 50), episode, exchange, token, symbol,
           text(row.get('underlying') or row.get('index')), text(row.get('direction'), 5),
           at.isoformat(), origin, number(row.get('score', row.get('gamma_score'))),
           json.dumps(plan, allow_nan=False), text(row.get('stage') or row.get('tier'), 60), blockers, at.isoformat()))
        old = c.execute('SELECT signal_id FROM trade_audit_plans WHERE audit_id=?', (aid,)).fetchone()
        if sid is not None:
            other = c.execute('SELECT audit_id FROM trade_audit_plans WHERE signal_id=?', (sid,)).fetchone()
            if other and other[0] != aid:
                raise ValueError('audit_signal_contract_or_episode_conflict')
            if old and old[0] not in (None, sid):
                raise ValueError('audit_episode_signal_conflict')
            c.execute('UPDATE trade_audit_plans SET signal_id=? WHERE audit_id=?', (sid, aid))
        c.execute('UPDATE trade_audit_plans SET stage=?,blockers=?,last_seen_at=? WHERE audit_id=? AND last_seen_at<=?',
                  (text(row.get('stage') or row.get('tier'), 60), blockers, at.isoformat(), aid, at.isoformat()))
    q_at = row.get('quote_at') or row.get('last_updated')
    if reference is not None and q_at:
        observe(exchange, [(token, reference)], q_at)
    from . import trade_learning
    trade_learning.guarded(trade_learning.capture_plan, aid, row)
    return aid


def record_gamma(producer, rows):
    from . import celestial
    # Persist every qualifying lead in this producer's bounded batch, including
    # other strikes and rows beyond the display's twenty-row limit.
    # Browser-only percentage scenarios are deliberately not persisted as plans.
    for row in celestial.ranked_candidates(candidates=rows):
        if row['final_score'] >= 60:
            record(dict(row, score=row['final_score']), 'GAMMA', producer)


def record_shield(payload, lead, pick, signal_id):
    # Read optional review metadata inside safe_call, not in the trading path.
    q = pick.quote
    return record(dict(payload, ts=now_ist().isoformat(), token=q.token,
        exchange=lead.chain.index.opt_exchange, symbol=q.symbol, option_ltp=q.ltp,
        expiry=getattr(lead.chain, '_expiry', ''), best_strike=q.strike),
        'INTRADAY', 'shield', signal_id)


def record_move(context, quote, chain, holding_minutes, signal_id=None):
    return record(dict(context, strike=quote.strike, expiry=getattr(chain, '_expiry', ''),
        holding_minutes=holding_minutes, ltp=quote.ltp, quote_at=context['ts']),
        'MOVE_CAPTURE', 'move_capture', signal_id)


def observe(exchange, quotes, quote_at, now=None):
    """Use only new, timestamped poll samples. Never infer an unseen wick or crossing."""
    at, now = stamp(quote_at), stamp(now or now_ist())
    if at is None or now is None or not 0 <= (now-at).total_seconds() <= 30:
        return
    quotes = {str(token): number(price, .000001) for token, price in quotes}
    quotes = {k: v for k, v in quotes.items() if v is not None}
    if not quotes:
        return
    from . import trade_learning
    trade_learning.guarded(trade_learning.observe, exchange, list(quotes.items()), quote_at, now)
    c = connection()
    tokens = list(quotes)
    for offset in range(0, len(tokens), 100):
        subset = tokens[offset:offset+100]
        records = c.execute('SELECT * FROM trade_audit_plans WHERE day=? AND exchange=? AND token IN ('+
            ','.join('?' for _ in subset)+')', (at.date().isoformat(), exchange, *subset)).fetchall()
        with c:
            for saved in records:
                r = dict(saved)
                previous = stamp(r['quote_at'])
                start = stamp(r['suggested_at'])
                if previous and at <= previous:
                    continue
                price = quotes[r['token']]
                p = json.loads(r['plan'])
                # Target/SL observations are bounded by the original horizon/session.
                deadline = stamp(p['session_exit']) or start.replace(hour=23, minute=59, second=59)
                if p['holding_minutes'] is not None:
                    deadline = min(deadline, start+timedelta(minutes=p['holding_minutes']))
                active = start <= at <= deadline and r['sl_at'] is None and r['t2_at'] is None
                t1 = r['t1_at'] or (at.isoformat() if active and p['t1'] and price >= p['t1'] else None)
                t2 = r['t2_at'] or (at.isoformat() if active and p['t2'] and price >= p['t2'] else None)
                sl = r['sl_at'] or (at.isoformat() if active and p['stop'] and price <= p['stop'] else None)
                # Scanners in separate processes can read the same old row.
                # Recheck time and preserve first hits/terminal state in SQLite
                # at write time; a later commit must not erase another sample.
                c.execute('''UPDATE trade_audit_plans SET ltp=?,quote_at=?,
                    high_ltp=CASE WHEN ? THEN MAX(COALESCE(high_ltp,?),?) ELSE high_ltp END,
                    t1_at=CASE WHEN sl_at IS NULL AND t2_at IS NULL THEN COALESCE(t1_at,?) ELSE t1_at END,
                    t2_at=CASE WHEN sl_at IS NULL AND t2_at IS NULL THEN COALESCE(t2_at,?) ELSE t2_at END,
                    sl_at=CASE WHEN sl_at IS NULL AND t2_at IS NULL THEN COALESCE(sl_at,?) ELSE sl_at END
                    WHERE audit_id=? AND (quote_at IS NULL OR quote_at<?)''',
                    (price, at.isoformat(), at >= start, price, price,
                     t1, t2, sl, r['audit_id'], at.isoformat()))


def _episode_for_signal(c, day, strategy, episode, exchange, token, signal_id, original_at=None):
    candidate = c.execute('''SELECT audit_id,signal_id,suggested_at FROM trade_audit_plans
        WHERE day=? AND strategy=? AND episode=? AND exchange=? AND token=?''',
        (day, strategy, episode, exchange, str(token))).fetchone()
    if candidate and (candidate['signal_id'] not in (None, signal_id) or
                      (original_at and stamp(candidate['suggested_at']) > original_at)):
        # A subsequent execution is a distinct row, not a second owner of a plan.
        return text(episode, 210) + '|signal:' + str(signal_id), None
    return episode, candidate


def record_submission(pos, side, quantity, price, intent):
    """Link by immutable signal+contract, never by a coincidentally matching ticker."""
    c = connection()
    if side in ('BUY', 'SELL'):
        signal = c.execute('SELECT ts,payload,kind FROM signals WHERE id=?', (pos.signal_id,)).fetchone()
        payload = {}
        if signal:
            try:
                payload = json.loads(signal['payload'])
            except (TypeError, ValueError):
                pass
        if not isinstance(payload, dict):
            payload = {}
        kind = payload.get('source_strategy') or (signal['kind'] if signal else pos.kind)
        strategy = strategy_code(kind)
        found = c.execute('SELECT * FROM trade_audit_plans WHERE signal_id=?', (pos.signal_id,)).fetchone()
        if found:
            if found['exchange'] != pos.exchange or found['token'] != str(pos.token):
                raise ValueError('audit_execution_contract_mismatch')
        else:
            # Preserve a producer-captured episode; a signal may have no option plan.
            at = stamp(signal['ts']) if signal else None
            day = (at or now_ist().astimezone(IST)).date().isoformat()
            episode = payload.get('episode') or ('contract:'+pos.exchange+':'+str(pos.token))
            episode, candidate = _episode_for_signal(c, day, strategy, episode,
                                                     pos.exchange, pos.token, pos.signal_id, at)
            if candidate:
                with c:
                    c.execute('UPDATE trade_audit_plans SET signal_id=? WHERE audit_id=? AND signal_id IS NULL',
                              (pos.signal_id, candidate[0]))
            else:
                same = str(payload.get('token') or '') == str(pos.token) and payload.get('exchange') == pos.exchange
                row = dict(payload) if same else {}
                row.update(ts=(at or now_ist()).isoformat(), episode=episode,
                           exchange=pos.exchange, token=str(pos.token), symbol=pos.symbol,
                           underlying=pos.index, direction=pos.direction)
                record(row, strategy, 'execution_ledger', pos.signal_id,
                       'PERSISTED_SIGNAL' if same else 'EXECUTION_ONLY')
    with c:
        c.execute('INSERT OR IGNORE INTO trade_audit_orders VALUES (?,?,?,?,?,?)',
                  (intent, pos.signal_id, side, price, quantity, now_ist().isoformat()))
    from . import trade_learning
    trade_learning.guarded(trade_learning.capture_submission, pos, side, quantity, price)


def strategy_for_position(pos):
    """Prefer the signal's recorded producer over the executor's reused lane name."""
    try:
        c = storage.conn()
        row = c.execute('SELECT kind,payload FROM signals WHERE id=?', (pos.signal_id,)).fetchone()
        if row:
            payload = json.loads(row['payload'])
            return strategy_code((payload.get('source_strategy') if isinstance(payload, dict) else None) or row['kind'])
    except (ValueError, TypeError):
        pass
    return strategy_code(pos.kind)


def _filters(day=None, strategy=None, status=None, symbol=None, limit=50, offset=0):
    day = day or now_ist().astimezone(IST).date().isoformat()
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('audit_invalid_date')
    date.fromisoformat(day)
    if strategy not in (None, '', 'ALL') and strategy not in STRATEGIES:
        raise ValueError('audit_invalid_strategy')
    if status not in (None, '', 'ALL') and status not in STATUSES:
        raise ValueError('audit_invalid_status')
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError('audit_invalid_page')
    if symbol is not None and (not isinstance(symbol, str) or len(symbol) > 80):
        raise ValueError('audit_invalid_symbol')
    return day, strategy, status, symbol, limit, offset


def report(day=None, strategy=None, status=None, symbol=None, limit=50, offset=0, now=None):
    day, strategy, status, symbol, limit, offset = _filters(day, strategy, status, symbol, limit, offset)
    now = stamp(now or now_ist())
    if now is None:
        raise ValueError('audit_invalid_time')
    c = connection()
    own_snapshot = not c.in_transaction
    if own_snapshot:
        c.execute('BEGIN')
    try:
        return _report(c, day, strategy, status, symbol, limit, offset, now)
    finally:
        if own_snapshot:
            c.rollback()


def _report(c, day, strategy, status, symbol, limit, offset, now):
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    where, params = ['a.day=?'], [day]
    if strategy and strategy != 'ALL': where.append('a.strategy=?'); params.append(strategy)
    if symbol: where.append('(instr(upper(a.symbol),upper(?))>0 OR instr(upper(a.underlying),upper(?))>0)'); params.extend([symbol, symbol])
    has_ledger = {'execution_orders', 'execution_fills'} <= tables
    fills = '''SELECT e.signal_id,
      SUM(CASE WHEN f.dry_run=0 AND e.side='BUY' THEN f.quantity ELSE 0 END) buy_qty,
      SUM(CASE WHEN f.dry_run=0 AND e.side='BUY' THEN f.quantity*f.price ELSE 0 END) buy_value,
      SUM(CASE WHEN f.dry_run=0 AND e.side='SELL' THEN f.quantity ELSE 0 END) sell_qty,
      SUM(CASE WHEN f.dry_run=0 AND e.side='SELL' THEN f.quantity*f.price ELSE 0 END) sell_value,
      SUM(CASE WHEN f.dry_run=0 AND e.side='SELL' THEN f.pnl ELSE 0 END) realized_pnl,
      MIN(CASE WHEN f.dry_run=0 AND e.side='BUY' THEN f.ts END) first_buy_at,
      MAX(CASE WHEN f.dry_run=0 AND e.side='SELL' THEN f.ts END) last_sell_at,
      SUM(CASE WHEN f.dry_run=1 THEN f.quantity ELSE 0 END) paper_qty
      FROM execution_fills f JOIN execution_orders e ON e.intent=f.intent
      WHERE e.signal_id IN (SELECT signal_id FROM base) GROUP BY e.signal_id''' if has_ledger else '''SELECT NULL signal_id,0 buy_qty,0 buy_value,0 sell_qty,0 sell_value,
      0 realized_pnl,NULL first_buy_at,NULL last_sell_at,0 paper_qty WHERE 0'''
    orders = '''SELECT signal_id,COUNT(*) order_count,
      MAX(CASE WHEN state!='terminal' THEN 1 ELSE 0 END) pending,
      MAX(CASE WHEN state IN ('unknown','submitting') THEN 1 ELSE 0 END) uncertain
      FROM execution_orders WHERE signal_id IN (SELECT signal_id FROM base) GROUP BY signal_id''' if has_ledger else 'SELECT NULL signal_id,0 order_count,0 pending,0 uncertain WHERE 0'
    cte = '''WITH base AS (SELECT a.* FROM trade_audit_plans a WHERE '''+' AND '.join(where)+'''),
      fills AS ('''+fills+'''), orders AS ('''+orders+'''), rows AS (
      SELECT a.*,p.status position_status,p.remaining_qty,p.exit_reason,
      f.buy_qty,f.buy_value,f.sell_qty,f.sell_value,f.realized_pnl,f.first_buy_at,f.last_sell_at,f.paper_qty,
      CASE WHEN o.uncertain=1 OR (f.paper_qty>0 AND (f.buy_qty>0 OR f.sell_qty>0)) THEN 'UNKNOWN'
           WHEN COALESCE(f.buy_qty,0)>0 OR COALESCE(f.sell_qty,0)>0 THEN
             CASE WHEN o.pending=1 THEN 'PENDING' WHEN p.status='closed' AND p.remaining_qty=0 THEN 'CLOSED' ELSE 'OPEN' END
           WHEN COALESCE(f.paper_qty,0)>0 THEN 'PAPER'
           WHEN o.pending=1 THEN 'PENDING' WHEN o.order_count>0 THEN 'UNFILLED'
           WHEN p.signal_id IS NOT NULL THEN 'UNKNOWN' ELSE 'SUGGESTED' END execution_status
      FROM base a LEFT JOIN positions p ON p.signal_id=a.signal_id
      LEFT JOIN fills f ON f.signal_id=a.signal_id LEFT JOIN orders o ON o.signal_id=a.signal_id)
    '''
    final_where = ' WHERE execution_status=?' if status and status != 'ALL' else ''
    final_params = params + ([status] if final_where else [])
    total = c.execute(cte+'SELECT COUNT(*) FROM rows'+final_where, final_params).fetchone()[0]
    raw = c.execute(cte+'SELECT * FROM rows'+final_where+' ORDER BY suggested_at DESC,audit_id LIMIT ? OFFSET ?',
                    (*final_params, limit, offset)).fetchall()
    rows = []
    for saved in raw:
        r = dict(saved); plan = json.loads(r.pop('plan')); r['plan'] = plan
        r['blocked_reasons'] = json.loads(r.pop('blockers'))
        r['strategy_label'] = STRATEGIES[r['strategy']]
        q = stamp(r['quote_at'])
        r['freshness'] = 'RECENT_POLL' if q and 0 <= (now-q).total_seconds() <= 30 else 'STALE'
        buy, sell = r.pop('buy_qty') or 0, r.pop('sell_qty') or 0
        buy_value, sell_value = r.pop('buy_value') or 0, r.pop('sell_value') or 0
        r['actual_entry'] = buy_value/buy if buy else None
        r['actual_exit'] = sell_value/sell if sell else None
        r['filled_buy_quantity'], r['filled_sell_quantity'] = buy, sell
        if not sell: r['realized_pnl'] = None
        first, last = stamp(r['first_buy_at']), stamp(r['last_sell_at'])
        r['actual_holding_seconds'] = (last-first).total_seconds() if first and last and last >= first and r['execution_status']=='CLOSED' else None
        r['elapsed_holding_seconds'] = max(0, (now-first).total_seconds()) if first and r['execution_status'] in ('OPEN','PENDING') else None
        r['holding_time_basis'] = 'ENGINE_FILL_CONFIRMATION'
        r['entry_difference'] = r['actual_entry']-plan['entry'] if r['actual_entry'] is not None and plan['entry'] is not None else None
        r['exit_difference'] = r['actual_exit']-plan['exit'] if r['actual_exit'] is not None and plan['exit'] is not None else None
        r['holding_difference_seconds'] = r['actual_holding_seconds']-plan['holding_minutes']*60 if r['actual_holding_seconds'] is not None and plan['holding_minutes'] is not None else None
        r['observed_result'] = 'SL_OBSERVED' if r['sl_at'] else 'T2_OBSERVED' if r['t2_at'] else 'T1_OBSERVED' if r['t1_at'] else 'NO_TARGET_OBSERVED'
        hit = stamp(r['sl_at'] or r['t2_at'] or r['t1_at'])
        r['time_to_result_seconds'] = (hit-stamp(r['suggested_at'])).total_seconds() if hit else None
        for target in ('t1', 't2', 'sl'):
            observed = stamp(r[target+'_at'])
            r['time_to_'+target+'_seconds'] = (observed-stamp(r['suggested_at'])).total_seconds() if observed else None
        limits = c.execute('SELECT side,limit_price,recorded_at FROM trade_audit_orders WHERE signal_id=? ORDER BY recorded_at,intent', (r['signal_id'],)).fetchall()
        entries = [x for x in limits if x['side']=='BUY']
        exits = [x for x in limits if x['side']=='SELL']
        r['entry_order_limit'] = entries[0]['limit_price'] if entries else None
        r['exit_order_limit'] = exits[-1]['limit_price'] if exits else None
        rows.append(r)
    return dict(ts=now.isoformat(), day=day, rows=rows, total=total, limit=limit, offset=offset,
                has_more=offset+len(rows)<total, strategies=STRATEGIES, statuses=STATUSES,
                capture=capture_health(), notes=[
                    'Original plans are frozen; missing proposals are not reconstructed from fills.',
                    'Actuals exclude simulated fills. Realized P&L is before fees.',
                    'Holding times use engine fill confirmations; exchange execution times are unavailable.',
                    'T1/T2/SL are observed quote crossings, not executable fills or complete wick history.'])


CSV_COLUMNS = (
    ('Strike / contract', 'symbol'), ('Trade type', 'strategy_label'), ('Suggested at IST', 'suggested_at'),
    ('Origin', 'origin'), ('Signal ID', 'signal_id'), ('Score', 'score'), ('Status', 'execution_status'),
    ('Proposed entry', 'plan.entry'), ('Entry reference', 'plan.entry_reference'), ('Entry basis', 'plan.entry_basis'),
    ('Actual entry', 'actual_entry'), ('Entry difference', 'entry_difference'),
    ('Entry order limit', 'entry_order_limit'), ('Last exit order limit', 'exit_order_limit'),
    ('Proposed exit', 'plan.exit'), ('Actual exit', 'actual_exit'), ('Exit difference', 'exit_difference'),
    ('Stop', 'plan.stop'), ('T1', 'plan.t1'), ('T2', 'plan.t2'),
    ('Proposed maximum hold minutes', 'plan.holding_minutes'), ('Actual hold seconds', 'actual_holding_seconds'),
    ('Hold difference seconds', 'holding_difference_seconds'), ('Holding time basis', 'holding_time_basis'),
    ('First BUY confirmation IST', 'first_buy_at'), ('Last SELL confirmation IST', 'last_sell_at'),
    ('Bought quantity', 'filled_buy_quantity'), ('Sold quantity', 'filled_sell_quantity'),
    ('Realized P&L before fees', 'realized_pnl'), ('Exit reason', 'exit_reason'),
    ('Current LTP', 'ltp'), ('Quote received IST', 'quote_at'), ('Quote status', 'freshness'),
    ('Observed outcome', 'observed_result'), ('T1 observed IST', 't1_at'), ('T2 observed IST', 't2_at'),
    ('SL observed IST', 'sl_at'), ('Time to observed result seconds', 'time_to_result_seconds'),
    ('Time to T1 seconds', 'time_to_t1_seconds'), ('Time to T2 seconds', 'time_to_t2_seconds'),
    ('Time to stop seconds', 'time_to_sl_seconds'),
    ('Stage', 'stage'), ('Blocked by', 'blocked_reasons'),   # 2026-10-20
)


def csv_value(row, key):
    value = row
    for part in key.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, list):   # blocked reasons: one readable cell
        value = ' | '.join(str(x) for x in value)
    # Keep numbers numeric; neutralize untrusted spreadsheet formula strings.
    if isinstance(value, str) and (re.match(r'^[\s\x00-\x1f]*[=+@-]', value) or value.startswith(('\t', '\r', '\n'))):
        value = "'"+value
    return '' if value is None else value


def export_report(day=None, strategy=None, status=None, symbol=None):
    c = connection()
    if c.in_transaction:
        raise ValueError('audit_export_transaction_busy')
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow([name for name, _ in CSV_COLUMNS])
    c.execute('BEGIN')
    try:
        offset = 0
        while True:
            data = report(day, strategy, status, symbol, limit=200, offset=offset)
            if data['total'] > 5000:
                raise ValueError('audit_export_too_large_refine_filters')
            for row in data['rows']:
                writer.writerow([csv_value(row, key) for _, key in CSV_COLUMNS])
            if not data['has_more']:
                break
            offset += len(data['rows'])
        return dict(filename='kadima_trade_audit_'+data['day']+'.csv', content=output.getvalue(), total=data['total'])
    finally:
        c.rollback()  # End the consistent read snapshot; never mutates execution data.


def backfill_positions(limit=500, after_signal_id=0):
    """Explicit offline/operator backfill. Never invent a historical proposed plan."""
    c = connection()
    if (type(limit) is not int or not 1 <= limit <= 1000 or
            type(after_signal_id) is not int or after_signal_id < 0):
        raise ValueError('audit_invalid_batch')
    rows = c.execute('''SELECT p.*,s.ts signal_ts,s.payload,s.kind signal_kind FROM positions p
                        LEFT JOIN signals s ON s.id=p.signal_id WHERE p.signal_id>?
                        ORDER BY p.signal_id LIMIT ?''', (after_signal_id, limit)).fetchall()
    count = 0
    for r in rows:
        if c.execute('SELECT 1 FROM trade_audit_plans WHERE signal_id=?', (r['signal_id'],)).fetchone():
            continue
        try:
            payload = json.loads(r['payload']) if r['payload'] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict): payload = {}
        strategy = strategy_code(payload.get('source_strategy') or r['signal_kind'] or r['kind'])
        same = payload.get('exchange') == r['exchange'] and str(payload.get('token') or '') == r['token']
        row = dict(payload) if same else {}
        at = stamp(r['signal_ts']) or stamp(r['entry_ts'])
        if at is None: continue
        # An import must not attach an old execution to a later same-day proposal.
        episode = text(payload.get('episode'), 200) + '|historical:' + str(r['signal_id'])
        row.update(ts=at.isoformat(), episode=episode,
                   exchange=r['exchange'], token=r['token'], symbol=r['symbol'],
                   underlying=r['index_name'], direction=r['direction'])
        record(row, strategy, 'history_import', r['signal_id'],
               'HISTORICAL_SIGNAL' if same else 'HISTORICAL_ACTUALS_ONLY')
        count += 1
    return dict(imported=count, scanned=len(rows), next_signal_id=rows[-1]['signal_id'] if rows else None)
