"""Continuous, evidence-only trade review; no broker, order or policy writes.

Freeze inputs before submission, review confirmed fills, and feed bounded past
observations back to the forecaster and strike diagnostics. Associations are
research candidates, not causal explanations or permission to trade.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta
import json
import logging
import math
import sqlite3
import threading
import time

from . import storage
from .utils import now_ist

VERSION = 'trade-learning-1'
CADENCE = 30
BATCH = 100
WINDOW_DAYS = 90
MAX_TRAINING_ROWS = 2000
MIN_TRADES = 30
MIN_DAYS = 5
_lock = threading.Lock()
_selections = OrderedDict()
_feedback = {}
_forecast = {}
_health = {'failures':0,'last_failure_at':None,'last_error_type':None}


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def stamp(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        return result if isinstance(result, datetime) and result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def fresh(value, now, seconds):
    at = stamp(value)
    return at is not None and 0 <= (now-at).total_seconds() <= seconds


def connection():
    c = storage.conn()
    # No executescript or implicit commit in the execution connection.
    for sql in (
        '''CREATE TABLE IF NOT EXISTS trade_learning_context (
          audit_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, evidence TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS trade_learning_decisions (
          signal_id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, audit_id TEXT,
          exchange TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT NOT NULL,
          evidence TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS trade_learning_pending (signal_id INTEGER PRIMARY KEY)''',
        '''CREATE TABLE IF NOT EXISTS trade_learning_reviews (
          signal_id INTEGER PRIMARY KEY, reviewed_at TEXT NOT NULL, day TEXT NOT NULL,
          strategy TEXT NOT NULL, underlying TEXT NOT NULL, symbol TEXT NOT NULL,
          outcome TEXT NOT NULL, evidence TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS learning_day ON trade_learning_reviews(day,strategy,signal_id)',
        '''CREATE TABLE IF NOT EXISTS trade_learning_samples (
          signal_id INTEGER PRIMARY KEY, first_at TEXT NOT NULL, last_at TEXT NOT NULL,
          low REAL NOT NULL, high REAL NOT NULL, samples INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS trade_learning_state (
          key TEXT PRIMARY KEY, value TEXT NOT NULL)''',
    ):
        c.execute(sql)
    return c


def guarded(function, *args, **kwargs):
    """Bound lock waits; failed review must never suppress an exit or submission."""
    c, timeout = None, None
    try:
        c = storage.conn()
        if c.in_transaction:
            raise RuntimeError('learning_inside_execution_transaction')
        timeout = c.execute('PRAGMA busy_timeout').fetchone()[0]
        c.execute('PRAGMA busy_timeout=25')
        return function(*args, **kwargs)
    except Exception as exc:
        with _lock:
            _health['failures']+=1
            _health['last_failure_at']=now_ist().isoformat()
            _health['last_error_type']=type(exc).__name__
        logging.getLogger(__name__).warning('Trade learning unavailable: %s', type(exc).__name__)
        return None
    finally:
        if c is not None and timeout is not None:
            try:
                c.execute('PRAGMA busy_timeout='+str(int(timeout)))
            except Exception:
                pass


def features(row):
    """Strict allowlist: no credentials, free-form news or post-trade outcomes."""
    out = {key: number(row.get(key)) for key in (
        'entry', 'stop', 't1', 't2', 'planned_exit', 'holding_minutes', 'score',
        'premium_move_pct', 'volume_ratio', 'oi_change', 'spot', 'vwap', 'rsi')}
    for key in ('direction_clear', 'oi_support', 'volume_spike', 'tradeable'):
        out[key] = row.get(key) if type(row.get(key)) is bool else None
    out['source_strategy'] = str(row.get('source_strategy') or row.get('strategy') or '')[:40]
    out['source_at'] = stamp(row.get('ts') or row.get('last_updated'))
    out['source_at'] = out['source_at'].isoformat() if out['source_at'] else None
    return out


def capture_plan(audit_id, row):
    at = now_ist()
    value = features(row)
    value['available_before_capture'] = fresh(value['source_at'], at, 30)
    c = connection()
    with c:
        c.execute('INSERT OR IGNORE INTO trade_learning_context VALUES (?,?,?)',
                  (audit_id, at.isoformat(), json.dumps(value, allow_nan=False)))


def remember_selection(chain, direction, spot, days, ranked):
    """Pure bounded memory adapter on the existing ranking path. No I/O."""
    try:
        now = now_ist()
        quotes = []
        for r in ranked[:40]:
            q = r.quote
            r.historical_review = strike_feedback(chain.index.name, direction, q)
            quotes.append(dict(token=str(q.token), symbol=q.symbol,
                strike=number(q.strike), premium=number(q.ltp), bid=number(q.bid), ask=number(q.ask),
                delta=number(q.delta), iv=number(q.iv), score=number(r.score), rejected=r.rejected))
        poll=getattr(chain,'_last_poll',0)
        age=time.monotonic()-poll
        if not poll or not 0<=age<=30:
            return
        value = dict(captured_at=now.isoformat(), exchange=chain.index.opt_exchange,
                     quote_at=(now-timedelta(seconds=age)).isoformat(),
                     direction=direction, spot=number(spot), days_to_expiry=number(days),
                     contracts=quotes, ranking_basis='TRADEABILITY_NOT_EXPECTED_PROFIT')
        with _lock:
            for q in quotes:
                key = (value['exchange'], q['token'], q['symbol'])
                _selections[key] = value
                _selections.move_to_end(key)
            while len(_selections) > 512:
                _selections.popitem(last=False)
    except Exception:
        pass  # Never change the rank order or fail a trading path.


def capture_submission(pos, side, quantity, price):
    c = connection()
    with c:
        c.execute('INSERT OR IGNORE INTO trade_learning_pending VALUES (?)', (pos.signal_id,))
    if side != 'BUY':
        return
    now = now_ist()
    with _lock:
        selected = deepcopy(_selections.get((pos.exchange, str(pos.token), pos.symbol)))
        forecast = deepcopy(_forecast)
    if (not selected or not fresh(selected['captured_at'], now, 30)
            or not fresh(selected.get('quote_at'),now,30)):
        selected = None
    if (not forecast or not fresh(forecast.get('issued_at'), now, 300)
            or not fresh(forecast.get('as_of'), now, 300)):
        forecast = {}
    asset = next((a for a in forecast.get('assets', []) if a.get('name') == pos.index), None)
    forecast_ref = dict(id=forecast.get('id'), issued_at=forecast.get('issued_at'),
                        model=forecast.get('model'), prompt_version=forecast.get('prompt_version'),
                        asset=asset) if asset else None
    plan = c.execute('SELECT * FROM trade_audit_plans WHERE signal_id=?', (pos.signal_id,)).fetchone()
    context = c.execute('SELECT * FROM trade_learning_context WHERE audit_id=?',
                        (plan['audit_id'],)).fetchone() if plan else None
    # A recovered plan captured after its source time cannot be passed off as
    # prospective evidence. The submission and selection snapshot still exist.
    prior_feedback=feedback(now)
    evidence = dict(version=VERSION, selection=selected, forecast=forecast_ref,
                    submitted_limit=number(price), submitted_quantity=quantity,
                    plan=json.loads(plan['plan']) if plan else None,
                    strategy=plan['strategy'] if plan else 'UNKNOWN',
                    underlying=pos.index, direction=pos.direction,
                    features=json.loads(context['evidence']) if context else None,
                    learning_feedback_as_of=prior_feedback.get('as_of'),
                    learning_policy_version=VERSION,
                    cost_basis='BROKER_FEES_UNAVAILABLE')
    with c:
        c.execute('INSERT OR IGNORE INTO trade_learning_decisions VALUES (?,?,?,?,?,?,?)',
                  (pos.signal_id, now.isoformat(), plan['audit_id'] if plan else None,
                   pos.exchange, str(pos.token), pos.symbol, json.dumps(evidence, allow_nan=False)))


def mark_dirty(signal_id):
    c = connection()
    with c:
        c.execute('INSERT OR IGNORE INTO trade_learning_pending VALUES (?)', (signal_id,))


def observe(exchange, quotes, quote_at, now=None):
    """Sample premiums only while an actual owned quantity exists, after a fill."""
    now = now or now_ist()
    if not fresh(quote_at, now, 30):
        return
    at = stamp(quote_at)
    c = connection()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='execution_fills'").fetchone():
        return
    prices={str(token):number(price) for token,price in quotes[:100]}
    prices={token:price for token,price in prices.items() if price is not None and price>0}
    if not prices:
        return
    # One query per quote batch, not one per F&O contract. Usually no owned
    # contract is present, so an entire ladder exits without any writes.
    owned = c.execute('''SELECT p.signal_id,p.token,MIN(f.ts) first_fill FROM positions p
        JOIN execution_orders e ON e.signal_id=p.signal_id AND e.side='BUY'
        JOIN execution_fills f ON f.intent=e.intent AND f.dry_run=0
        WHERE p.exchange=? AND p.token IN ('''+','.join('?' for _ in prices)+''') AND p.remaining_qty>0
          AND p.status IN ('open','entry_pending','exit_pending') GROUP BY p.signal_id''',
        (exchange,*prices)).fetchall()
    with c:
        for row in owned:
            if not stamp(row['first_fill']) or at < stamp(row['first_fill']):
                continue
            price=prices[row['token']]
            c.execute('''INSERT INTO trade_learning_samples VALUES (?,?,?,?,?,1)
                ON CONFLICT(signal_id) DO UPDATE SET
                  last_at=excluded.last_at,low=MIN(low,excluded.low),high=MAX(high,excluded.high),samples=samples+1
                WHERE julianday(excluded.last_at)>julianday(last_at)''',
                (row['signal_id'], at.isoformat(), at.isoformat(), price, price))


def _lesson(code, action, evidence, kind='OBSERVED'):
    return dict(code=code, action=action, evidence=evidence, status=kind)


def forecast_outcomes(reference, now):
    if not reference or not reference.get('id'):
        return dict(status='NOT_CAPTURED',checkpoints=[])
    try:
        from .forecast_observer import JOURNAL
        if not JOURNAL.exists():
            return dict(status='JOURNAL_UNAVAILABLE',checkpoints=[])
        c=sqlite3.connect(JOURNAL.as_uri()+'?mode=ro',uri=True,timeout=.05)
        try:
            rows=c.execute('''SELECT minute,target_at,base,expected,actual,actual_at
                              FROM targets WHERE run_id=? AND name=? ORDER BY minute''',
                           (reference['id'],reference['asset']['name'])).fetchall()
        finally:
            c.close()
        points=[]
        for minute,target,base,expected,actual,actual_at in rows:
            if (number(actual) is None or not stamp(target) or not stamp(actual_at)
                    or not stamp(target)<=stamp(actual_at)<=now
                    or (stamp(actual_at)-stamp(target)).total_seconds()>30):
                continue
            points.append(dict(minute=minute,target_at=target,expected=expected,actual=actual,
                actual_at=actual_at,absolute_error=abs(actual-expected),
                unchanged_price_error=abs(actual-base),
                direction_match=((expected>base)-(expected<base))==((actual>base)-(actual<base))))
        return dict(status='SCORED' if points else 'PENDING_OR_MISSING_SAMPLES',checkpoints=points,
                    basis='UNDERLYING_AT_FORECAST_HORIZON_NOT_TRADE_PNL')
    except (sqlite3.Error,ValueError,KeyError,ImportError,OSError):
        return dict(status='JOURNAL_UNAVAILABLE',checkpoints=[])


def review(signal_id, now=None):
    c=connection()
    if c.in_transaction:
        raise RuntimeError('learning_review_transaction_busy')
    c.execute('BEGIN')
    try:
        return _review(signal_id,now)
    finally:
        if c.in_transaction: c.rollback()


def contract_profile(chosen, plan, selection):
    delta=number(chosen.get('delta'))
    bid,ask=number(chosen.get('bid')),number(chosen.get('ask'))
    spread=100*(ask-bid)/((ask+bid)/2) if bid and ask and 0<bid<=ask else None
    hold=number(plan.get('holding_minutes'))
    days=number(selection.get('days_to_expiry'))
    return dict(delta_band='UNKNOWN' if delta is None else 'LOW' if abs(delta)<.4 else 'MID' if abs(delta)<=.65 else 'HIGH',
        spread_band='UNKNOWN' if spread is None else 'TIGHT' if spread<=.5 else 'MODERATE' if spread<=1.5 else 'WIDE',
        horizon='UNKNOWN' if hold is None else 'UP_TO_5M' if hold<=5 else 'UP_TO_15M' if hold<=15 else 'LONGER',
        expiry='UNKNOWN' if days is None else 'EXPIRY_DAY' if days<=1 else 'LATER')


def _review(signal_id, now=None):
    """Actual fill accounting, not selected-strike LTP or post-hoc price targets."""
    now = now or now_ist()
    c = connection()
    p = c.execute('SELECT * FROM positions WHERE signal_id=?', (signal_id,)).fetchone()
    if not p:
        with c:
            c.execute('DELETE FROM trade_learning_pending WHERE signal_id=?',(signal_id,))
        return None
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_ledger={'execution_orders', 'execution_fills'} <= tables
    orders = c.execute('SELECT * FROM execution_orders WHERE signal_id=?', (signal_id,)).fetchall() if has_ledger else []
    fills = c.execute('''SELECT f.*,e.side FROM execution_fills f JOIN execution_orders e
                         ON e.intent=f.intent WHERE e.signal_id=? ORDER BY f.ts,f.intent,f.cumulative_qty''',
                      (signal_id,)).fetchall() if has_ledger else []
    live = [f for f in fills if f['dry_run'] == 0]
    paper = [f for f in fills if f['dry_run'] != 0]
    buys, sells = ([f for f in live if f['side'] == side] for side in ('BUY', 'SELL'))
    bq, sq = sum(f['quantity'] for f in buys), sum(f['quantity'] for f in sells)
    bv, sv = sum(f['price']*f['quantity'] for f in buys), sum(f['price']*f['quantity'] for f in sells)
    uncertain = any(o['state'] in ('unknown', 'submitting') for o in orders)
    pending = any(o['state'] != 'terminal' for o in orders)
    valid = all(f['quantity'] > 0 and number(f['price']) is not None and f['price'] > 0 for f in live)
    closed = p['status'] == 'closed' and p['remaining_qty'] == 0
    balanced = bq > 0 and bq == sq
    gross = sv-bv if balanced and valid and closed and not pending and not paper else None
    if uncertain or (paper and live) or not valid or sq > bq or (closed and live and not balanced):
        outcome = 'UNRESOLVED'
        gross = None
    elif paper:
        outcome = 'PAPER'
    elif pending or (bq > sq and not closed):
        outcome = 'OPEN'
    elif gross is not None:
        outcome = 'GROSS_WIN' if gross > .005 else 'GROSS_LOSS' if gross < -.005 else 'GROSS_FLAT'
    else:
        outcome = 'UNFILLED' if orders else 'UNRESOLVED'
    d = c.execute('SELECT * FROM trade_learning_decisions WHERE signal_id=?', (signal_id,)).fetchone()
    evidence = json.loads(d['evidence']) if d else {}
    identity_ok = bool(d and (d['exchange'], d['token'], d['symbol']) == (p['exchange'], p['token'], p['symbol']))
    first = min((stamp(f['ts']) for f in buys if stamp(f['ts'])), default=None)
    last = max((stamp(f['ts']) for f in sells if stamp(f['ts'])), default=None)
    prospective = bool(identity_ok and first and stamp(d['captured_at']) and stamp(d['captured_at']) <= first)
    if not identity_ok:
        evidence = {}
    plan = evidence.get('plan') or {}
    feat = evidence.get('features') or {}
    if feat.get('available_before_capture') is not True:
        feat={}
    chosen = next((q for q in (evidence.get('selection') or {}).get('contracts', [])
                   if q['token'] == p['token'] and q['symbol'] == p['symbol']), {})
    entry, exit_price = bv/bq if bq else None, sv/sq if sq else None
    actual_hold = (last-first).total_seconds() if first and last and last >= first and gross is not None else None
    sampled = c.execute('SELECT * FROM trade_learning_samples WHERE signal_id=?', (signal_id,)).fetchone()
    should, avoid, missing = [], [], ['Broker fees unavailable; net outcome is unknown.']
    if not prospective:
        missing.append('No matching pre-fill decision snapshot; excluded from decision learning.')
    if not evidence.get('forecast'):
        missing.append('No eligible forecast was captured before submission.')
    if not chosen:
        missing.append('No fresh ranked-contract snapshot; strike-quality comparison unavailable.')
    if not sampled:
        missing.append('No owned-position premium samples; excursion and giveback unknown.')
    reward, risk, rr = None, None, None
    target = number(plan.get('exit')) or number(plan.get('t1'))
    stop = number(plan.get('stop'))
    if entry and target and stop and 0 < stop < entry < target:
        reward, risk = target-entry, entry-stop
        rr = reward/risk
        (avoid if rr < 2 else should).append(_lesson('ENTRY_RR_BELOW_2' if rr < 2 else 'ENTRY_RR_AT_LEAST_2',
            'Evaluate target/stop reward-to-risk using fill price and costs.', {'gross_rr':rr}))
    else:
        avoid.append(_lesson('RR_EVIDENCE_MISSING', 'Do not treat an absent target/stop as a passed reward-to-risk check.', {}))
    if chosen.get('premium') and entry:
        change = 100*(entry/chosen['premium']-1)
        if change > 0:
            avoid.append(_lesson('ENTRY_ABOVE_REFERENCE', 'Account for adverse movement from selection to fill.',
                                 {'entry_vs_reference_pct':change, 'basis':'LTP_REFERENCE_NOT_EXECUTABLE_ASK'}))
    if feat.get('oi_support') is True and feat.get('volume_spike') is True and gross is not None and gross > 0:
        should.append(_lesson('CONFIRMED_SETUP_WON', 'Retain OI/volume confirmation as a candidate feature to validate across trades.',
                              {'oi_support':True,'volume_spike':True}, 'ASSOCIATION_NOT_CAUSE'))
    if actual_hold is not None and number(plan.get('holding_minutes')) and actual_hold > plan['holding_minutes']*60:
        avoid.append(_lesson('HOLD_EXCEEDED_PLAN', 'Review exit timing and pending-order delays against the planned horizon.',
                             {'planned_minutes':plan['holding_minutes'],'actual_seconds':actual_hold}))
    if sampled and entry and exit_price and gross is not None and sampled['high'] > max(entry, exit_price):
        avoid.append(_lesson('SAMPLED_PROFIT_GIVEBACK', 'Test profit protection with executable quotes; the sampled peak was not a guaranteed fill.',
                             {'sampled_high':sampled['high'],'actual_exit':exit_price}, 'RESEARCH_CANDIDATE'))
    from .trade_audit import strategy_for_position, IST
    day = (first or stamp(p['entry_ts']) or now).astimezone(IST).date().isoformat()
    from types import SimpleNamespace
    strategy = strategy_for_position(SimpleNamespace(signal_id=signal_id, kind=p['kind']))
    result = dict(version=VERSION, signal_id=signal_id, reviewed_at=now.isoformat(),
        strategy=strategy, underlying=p['index_name'], symbol=p['symbol'], direction=p['direction'],
        outcome=outcome, gross_pnl=gross, net_pnl=None, cost_status='UNAVAILABLE',
        buy_quantity=bq, sell_quantity=sq, actual_entry=entry, actual_exit=exit_price,
        first_fill_at=first.isoformat() if first else None, last_fill_at=last.isoformat() if last else None,
        actual_holding_seconds=actual_hold, holding_basis='ENGINE_FILL_CONFIRMATION',
        prospective=prospective, rr_before_costs=rr, should=should, should_not=avoid,
        profile=contract_profile(chosen,plan,evidence.get('selection') or {}),
        missing=missing, sampled_excursion=dict(sampled) if sampled else None,
        decision=evidence, cause_status='UNCONFIRMED',
        forecast_review=forecast_outcomes(evidence.get('forecast'),now),
        learnable=prospective and gross is not None and strategy not in ('UNKNOWN','MANUAL'),
        source=dict(signal_id=signal_id, audit_id=d['audit_id'] if d else None,
                    fill_keys=[dict(intent=f['intent'], cumulative_qty=f['cumulative_qty']) for f in fills]))
    with c:
        c.execute('''INSERT INTO trade_learning_reviews VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(signal_id) DO UPDATE SET reviewed_at=excluded.reviewed_at,day=excluded.day,
          strategy=excluded.strategy,underlying=excluded.underlying,symbol=excluded.symbol,
          outcome=excluded.outcome,evidence=excluded.evidence''',
          (signal_id,now.isoformat(),day,strategy,p['index_name'],p['symbol'],outcome,json.dumps(result,allow_nan=False)))
        c.execute('DELETE FROM trade_learning_pending WHERE signal_id=?', (signal_id,))
    return result


def build_feedback(now):
    c = connection()
    cutoff = (now-timedelta(days=WINDOW_DAYS)).date().isoformat()
    raw = c.execute('''SELECT evidence FROM trade_learning_reviews WHERE day>=?
                       AND day<=? ORDER BY day DESC,signal_id DESC LIMIT ?''',
                    (cutoff, now.date().isoformat(), MAX_TRAINING_ROWS+1)).fetchall()
    groups, forecasts, seen_forecasts = {}, {}, set()
    for saved in raw[:MAX_TRAINING_ROWS]:
        r = json.loads(saved[0])
        if (not r['learnable'] or not fresh(r['reviewed_at'], now, 86400*WINDOW_DAYS)
                or not stamp(r['last_fill_at']) or stamp(r['last_fill_at']) > now):
            continue
        # Fixed, predeclared groups prevent a hunt through thousands of fitted rules.
        profile=r['profile']
        key = (r['strategy'], r['underlying'], r['direction'], *profile.values())
        g = groups.setdefault(key, dict(strategy=key[0],underlying=key[1],direction=key[2],
            profile=profile,
            n=0,wins=0,losses=0,flat=0,gross_pnl=0.0,days=set(),evidence_ids=[],issues={}))
        g['n'] += 1
        g['wins'] += r['outcome']=='GROSS_WIN'
        g['losses'] += r['outcome']=='GROSS_LOSS'
        g['flat'] += r['outcome']=='GROSS_FLAT'
        g['gross_pnl'] += r['gross_pnl']
        g['days'].add(stamp(r['last_fill_at']).date().isoformat())
        if len(g['evidence_ids']) < 10:
            g['evidence_ids'].append(r['signal_id'])
        for lesson in r['should_not']:
            code=lesson['code']; g['issues'][code]=g['issues'].get(code,0)+1
        ref=r['decision'].get('forecast') or {}
        for point in r.get('forecast_review',{}).get('checkpoints',[]):
            identity=(ref.get('id'),r['underlying'],point['minute'])
            if identity in seen_forecasts or not ref.get('id') or not stamp(point['actual_at']) or stamp(point['actual_at'])>now:
                continue
            seen_forecasts.add(identity)
            fk=(ref.get('model'),ref.get('prompt_version'),r['underlying'],point['minute'])
            fg=forecasts.setdefault(fk,dict(model=fk[0],prompt_version=fk[1],underlying=fk[2],minute=fk[3],
                n=0,error=0.0,baseline_error=0.0,direction_matches=0,days=set()))
            fg['n']+=1;fg['error']+=point['absolute_error'];fg['baseline_error']+=point['unchanged_price_error']
            fg['direction_matches']+=point['direction_match'];fg['days'].add(stamp(point['actual_at']).date().isoformat())
    values=[]
    for g in groups.values():
        g['sessions']=len(g.pop('days'))
        g['gross_win_fraction']=g['wins']/g['n']
        g['mean_gross_pnl']=g['gross_pnl']/g['n']
        g['status']='RESEARCH_READY' if g['n']>=MIN_TRADES and g['sessions']>=MIN_DAYS else 'COLLECTING'
        g['action']='REVIEW_BEFORE_PROMOTION' if g['status']=='RESEARCH_READY' else 'COLLECT_MORE_EVIDENCE'
        g['automatic_rule_change']=False
        values.append(g)
    values.sort(key=lambda g:(-g['n'],g['strategy'],g['underlying'],g['direction']))
    forecast_groups=[]
    for fg in forecasts.values():
        fg['sessions']=len(fg.pop('days'))
        fg['mae_points']=fg.pop('error')/fg['n']
        fg['unchanged_price_mae_points']=fg.pop('baseline_error')/fg['n']
        fg['status']='COLLECTING'
        if fg['n']>=MIN_TRADES and fg['sessions']>=MIN_DAYS:
            fg['status']='REVIEW_MODEL' if fg['mae_points']>fg['unchanged_price_mae_points'] else 'CHECK_LATER_PERIOD'
        forecast_groups.append(fg)
    forecast_groups.sort(key=lambda g:(-g['n'],str(g['model']),str(g['prompt_version']),g['underlying'],g['minute']))
    return dict(version=VERSION,as_of=now.isoformat(),mode='ADVISORY',
        window_days=WINDOW_DAYS,rows_considered=min(len(raw),MAX_TRAINING_ROWS),
        window_truncated=len(raw)>MAX_TRAINING_ROWS,groups=values[:40],
        groups_truncated=len(values)>40,minimum_trades=MIN_TRADES,minimum_sessions=MIN_DAYS,
        forecast_groups=forecast_groups[:48],forecast_groups_truncated=len(forecast_groups)>48,
        notes=['Wins/losses are before fees; net profitability is not established.',
               'Grouped associations are not causal explanations or calibrated predictions.',
               'Forecast evaluation covers captured traded contexts; overlapping forecasts are not independent.',
               'Live rules require later-period validation, cost evidence and a versioned release.'])


def feedback(now=None):
    now=now or now_ist()
    with _lock:
        value=deepcopy(_feedback)
    if not value or not fresh(value.get('as_of'),now,120):
        return dict(version=VERSION,mode='ADVISORY',status='STALE_OR_WARMING_UP',groups=[])
    return value


def strike_feedback(underlying,direction,quote=None,now=None):
    value=feedback(now)
    profile=contract_profile(dict(delta=getattr(quote,'delta',None),bid=getattr(quote,'bid',None),
                                  ask=getattr(quote,'ask',None)),{}, {}) if quote else None
    return dict(mode='ADVISORY',as_of=value.get('as_of'),status=value.get('status','AVAILABLE'),
        groups=[g for g in value['groups'] if g['underlying']==underlying and g['direction']==direction
                and (not profile or all(g['profile'][k]==profile[k] for k in ('delta_band','spread_band')))][:8],
        ranking_changed=False)


class Learner:
    def __init__(self):
        self.stop_event=threading.Event()
        self.thread=None

    def step(self,now=None):
        now=now or now_ist()
        c=connection()
        # Persistent rotating sweep recovers missed capture notifications and
        # reviews old trades with explicit missing evidence, including restarts.
        cursor=c.execute("SELECT value FROM trade_learning_state WHERE key='cursor'").fetchone()
        cursor=int(cursor[0]) if cursor else 0
        rows=c.execute('SELECT signal_id FROM positions WHERE signal_id>? ORDER BY signal_id LIMIT ?',
                       (cursor,BATCH)).fetchall()
        with c:
            c.executemany('INSERT OR IGNORE INTO trade_learning_pending VALUES (?)',[(r[0],) for r in rows])
            c.execute("INSERT OR REPLACE INTO trade_learning_state VALUES ('cursor',?)",
                      (str(rows[-1][0]) if len(rows)==BATCH else '0',))
        for row in c.execute('SELECT signal_id FROM trade_learning_pending ORDER BY signal_id LIMIT ?', (BATCH,)).fetchall():
            try:
                review(row[0],now)
            except (ValueError,TypeError,KeyError) as exc:
                # One malformed record must not starve the remaining queue.
                with _lock:
                    _health['failures']+=1
                    _health['last_failure_at']=now.isoformat()
                    _health['last_error_type']=type(exc).__name__
                with c:
                    c.execute('DELETE FROM trade_learning_pending WHERE signal_id=?',(row[0],))
        value=build_feedback(now)
        forecast={}
        try:
            from .forecast_observer import REPORT
            from .checklist_forecast import read_json
            forecast=read_json(REPORT)
        except (OSError,ValueError,ImportError):
            pass
        if not isinstance(forecast,dict): forecast={}
        with c:
            c.execute("INSERT OR REPLACE INTO trade_learning_state VALUES ('feedback',?)",
                      (json.dumps(value,allow_nan=False),))
            c.execute("INSERT OR REPLACE INTO trade_learning_state VALUES ('heartbeat',?)",(now.isoformat(),))
            with _lock:
                health=deepcopy(_health)
            c.execute("INSERT OR REPLACE INTO trade_learning_state VALUES ('health',?)",(json.dumps(health),))
        global _feedback,_forecast
        with _lock:
            _feedback=value
            _forecast=forecast
        return value

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread=threading.Thread(target=self.run,daemon=True,name='trade-learning')
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            guarded(self.step)
            self.stop_event.wait(CADENCE)

    def stop(self):
        self.stop_event.set()


def report(day=None,strategy=None,limit=50,offset=0,now=None):
    from .trade_audit import _filters
    day,strategy,_,_,limit,offset=_filters(day,strategy,None,None,limit,offset)
    now=now or now_ist()
    c=connection()
    where='day=?'; params=[day]
    if strategy and strategy!='ALL': where+=' AND strategy=?'; params.append(strategy)
    total=c.execute('SELECT COUNT(*) FROM trade_learning_reviews WHERE '+where,params).fetchone()[0]
    rows=c.execute('SELECT evidence FROM trade_learning_reviews WHERE '+where+
                   ' ORDER BY signal_id DESC LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
    state={r['key']:r['value'] for r in c.execute("SELECT * FROM trade_learning_state WHERE key IN ('heartbeat','feedback','health')")}
    return dict(ts=now.isoformat(),day=day,rows=[json.loads(r[0]) for r in rows],total=total,
        limit=limit,offset=offset,has_more=offset+len(rows)<total,mode='ADVISORY',
        worker_at=state.get('heartbeat'),worker_fresh=fresh(state.get('heartbeat'),now,120),
        pending=c.execute('SELECT COUNT(*) FROM trade_learning_pending').fetchone()[0],
        feedback=json.loads(state['feedback']) if state.get('feedback') else {},
        capture=json.loads(state['health']) if state.get('health') else {},
        execution_effect='No automatic threshold, size, stop or ranking changes.')
