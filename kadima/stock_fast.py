"""Stock premium momentum: independent detection, shared guarded execution.

No broker method is called at import time, by the producer or by a GET route.
Installation never arms trading. Closed candles are fetched only for current
premium candidates; REST samples are never manufactured into OHLC candles.
"""
from collections import OrderedDict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import math
import re
import threading
import time

MAX_ROOTS = 512
MAX_PAYLOAD = 100_000
HORIZONS = (5, 10, 15)
DEFAULTS = {"live_enabled": False, "holding_minutes": 15, "revision": 0}
# Strategy exit-request deadline, NOT an assertion about exchange closing time.
# Keep a buffer before the broker's published 15:20 intraday F&O square-off.
EXIT_HHMM = (15, 15)


def _now():
    from .utils import now_ist
    return now_ist()


def _finite(value, minimum=0):
    return (type(value) in (int, float) and math.isfinite(value)
            and value >= minimum)


def _public_reason(exc, fallback='stock_fast_invalid_inputs'):
    # Never surface provider bodies, credentials or arbitrary exception content.
    text = str(exc)
    from .preparation_diagnostics import KNOWN_MESSAGES
    if text in KNOWN_MESSAGES:
        return KNOWN_MESSAGES[text]
    return text if type(exc) is ValueError and re.fullmatch(r'[a-z_][a-z0-9_]{0,99}', text) else fallback


def _fresh(stamp, now, seconds=30):
    try:
        parsed = datetime.fromisoformat(stamp)
        return parsed.tzinfo is not None and 0 <= (now-parsed).total_seconds() <= seconds
    except (ValueError, TypeError):
        return False


def _connection():
    from . import storage
    c = storage.conn()
    for sql in (
        'CREATE TABLE IF NOT EXISTS stock_fast_snapshots '
        '(name TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS stock_fast_controls '
        '(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS stock_fast_policies '
        '(signal_id INTEGER PRIMARY KEY, episode TEXT UNIQUE NOT NULL, '
        'hold_minutes INTEGER NOT NULL, session_exit TEXT NOT NULL)',
    ):
        c.execute(sql)
    return c


def validate_controls(value):
    if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
            or type(value['live_enabled']) is not bool
            or type(value['holding_minutes']) is not int
            or value['holding_minutes'] not in HORIZONS
            or type(value['revision']) is not int or value['revision'] < 0):
        raise ValueError('stock_fast_invalid_controls')
    return value


def _read_controls(c):
    row = c.execute('SELECT payload FROM stock_fast_controls WHERE id=1').fetchone()
    return validate_controls(json.loads(row[0]) if row else DEFAULTS.copy())


def controls():
    return _read_controls(_connection())


def update_controls(patch, expected_revision):
    """Authenticated operator intent only; never changes global broker flags."""
    from . import celestial, fast_momentum
    if (not isinstance(patch, dict) or not patch
            or set(patch) - {'live_enabled', 'holding_minutes'}
            or type(expected_revision) is not int or expected_revision < 0):
        raise ValueError('stock_fast_invalid_control_patch')
    rollout_ready = celestial.enabled() and fast_momentum.live_requested()
    kill_latched = celestial.read_config()['kill_latched'] if patch.get('live_enabled') else False
    c = _connection()
    c.execute('BEGIN IMMEDIATE')
    try:
        cfg = _read_controls(c)
        if cfg['revision'] != expected_revision:
            raise ValueError('stock_fast_config_changed_refresh_required')
        updated = validate_controls(dict(cfg, **patch))
        if updated['live_enabled'] and not cfg['live_enabled']:
            # A request can still be blocked at trade time. It is not a fill.
            if not rollout_ready:
                raise ValueError('stock_fast_rollout_or_fast_master_not_enabled')
            if kill_latched:
                raise ValueError('stock_fast_kill_latched')
        updated['revision'] += 1
        c.execute('INSERT OR REPLACE INTO stock_fast_controls VALUES (1,?)',
                  (json.dumps(updated, allow_nan=False),))
        c.commit()
        return updated
    except Exception:
        c.rollback()
        raise


class PremiumWindow:
    """Bounded received-quote history; rejects stale, reset and duplicate samples."""
    def __init__(self):
        self.samples = deque(maxlen=64)

    def add(self, stamp, ltp, bid, ask, volume):
        values = (ltp, bid, ask)
        if (stamp.tzinfo is None or not all(_finite(v, .000001) for v in values)
                or ask < bid or not _finite(volume)):
            self.samples.clear()
            return None
        if self.samples:
            previous = self.samples[-1]
            gap = (stamp-previous[0]).total_seconds()
            if gap == 0:
                return None
            if gap < 0 or gap > 90 or stamp.date() != previous[0].date() or volume < previous[4]:
                self.samples.clear()
        self.samples.append((stamp, ltp, bid, ask, volume))
        while len(self.samples) > 1 and (stamp-self.samples[1][0]).total_seconds() >= 180:
            self.samples.popleft()
        base = self.samples[0]
        span = (stamp-base[0]).total_seconds()
        if not 180 <= span <= 270:
            return None
        middle = next((s for s in reversed(self.samples)
                       if (stamp-s[0]).total_seconds() >= 60), None)
        if middle is None or middle[0] <= base[0] or middle[4] <= base[4]:
            return None
        prior = (middle[4]-base[4])/(middle[0]-base[0]).total_seconds()
        recent = (volume-middle[4])/(stamp-middle[0]).total_seconds()
        return dict(premium_move_pct=100*(ltp/middle[1]-1),
                    bid_move_pct=100*(bid/middle[2]-1),
                    ask_move_pct=100*(ask/middle[3]-1), volume_ratio=recent/prior,
                    baseline_at=middle[0].isoformat(), observed_at=stamp.isoformat())


def accelerated(metric):
    return (isinstance(metric, dict) and _finite(metric.get('premium_move_pct'), 2)
            and _finite(metric.get('volume_ratio'), 1.5)
            and _finite(metric.get('bid_move_pct'), .000001)
            and _finite(metric.get('ask_move_pct'), .000001))


class Recorder:
    """Called for every complete stock ladder, before and independently of Gamma."""
    def __init__(self):
        self.windows = {}
        self.day = None

    def invalidate(self, name):
        # Retain genuinely observed common-token history across a temporary gap.
        # PremiumWindow itself resets on >90s gaps, day/identity/volume changes.
        c = _connection()
        with c:
            c.execute('DELETE FROM stock_fast_snapshots WHERE name=?', (name,))

    def publish(self, chain, spot, stamp, vwap, spot_context=None, gamma=None):
        now = _now()
        if self.day != now.date():
            self.windows.clear()
            self.day = now.date()
        name = chain.index.name
        if not _fresh(stamp, now) or not _finite(spot, .000001) or len(chain.quotes) != 6:
            self.invalidate(name)
            return
        if name not in self.windows and len(self.windows) >= MAX_ROOTS:
            raise ValueError('stock_fast_root_budget_exceeded')
        at = datetime.fromisoformat(stamp)
        prior = self.windows.get(name, {})
        kept, metrics = {}, {}
        for q in chain.quotes.values():
            key = (chain._expiry, str(q.token), q.symbol)
            window = prior.get(key, PremiumWindow())
            metric = window.add(at, q.ltp, q.bid, q.ask, q.volume)
            kept[key] = window
            if metric is not None:
                metrics[str(q.token)] = metric
        self.windows[name] = kept
        data = dict(index=asdict(chain.index), spot=spot, spot_at=stamp,
                    chain_at=stamp, session_vwap=vwap, ladder_width=1,
                    quotes=[asdict(q) for q in chain.quotes.values()],
                    fast_metrics=metrics, source='received_full_quote_samples')
        if isinstance(spot_context, dict):
            for key in ('day_open','previous_close','cumulative_volume'):
                value = spot_context.get(key)
                if _finite(value, 0 if key == 'cumulative_volume' else .000001):
                    data[key] = value
        if isinstance(gamma, dict):
            data['gamma'] = gamma
        encoded = json.dumps(data, allow_nan=False)
        if len(encoded) > MAX_PAYLOAD:
            raise ValueError('stock_fast_snapshot_too_large')
        c = _connection()
        with c:
            c.execute('DELETE FROM stock_fast_snapshots WHERE observed_at < ?',
                      (now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),))
            c.execute('INSERT OR REPLACE INTO stock_fast_snapshots VALUES (?,?,?)',
                      (name, stamp, encoded))


def observations(now=None):
    now = now or _now()
    rows = _connection().execute('SELECT name,observed_at,payload FROM stock_fast_snapshots').fetchall()
    if len(rows) > MAX_ROOTS:
        raise ValueError('stock_fast_snapshot_budget_exceeded')
    result = []
    for name, stamp, encoded in rows:
        if _fresh(stamp, now):
            data = json.loads(encoded)
            if data['index']['name'] != name or data['chain_at'] != stamp:
                raise ValueError('stock_fast_snapshot_identity_mismatch')
            result.append(data)
    return result


def still_current(data):
    row = _connection().execute('SELECT observed_at FROM stock_fast_snapshots WHERE name=?',
                                (data['index']['name'],)).fetchone()
    return bool(row and row[0] == data['chain_at'] and _fresh(row[0], _now()))


def session_exit(now):
    return now.replace(hour=EXIT_HHMM[0], minute=EXIT_HHMM[1], second=0, microsecond=0)


def entry_time_allowed(now, minutes):
    return (now.weekday() < 5 and (now.hour, now.minute) >= (9, 15)
            and now + timedelta(minutes=minutes) < session_exit(now))


def plan(data, chain, bars, cfg, now=None):
    """No network/side effects. Heuristic hypotheses, not calibrated probabilities."""
    from . import fast_momentum as fast, strike_selector, trade_policy
    from .config import MARKET_CLOSE
    now = now or _now()
    row = dict(underlying=chain.index.name, strategy='fast_momentum', asset_scope='STOCK', ts=now.isoformat(), quote_at=data['chain_at'],
               stage='WATCH', eligible=False, blocked_reasons=[], scenarios=[],
               direction=None, holding_minutes=cfg['holding_minutes'], control_revision=cfg['revision'])
    try:
        if not all(_fresh(data.get(k), now) for k in ('chain_at', 'spot_at')):
            raise ValueError('stock_fast_stale_snapshot')
        if not entry_time_allowed(now, cfg['holding_minutes']):
            raise ValueError('stock_fast_horizon_crosses_session_exit')
        setup = fast.setup(bars[1], bars[5], data['spot'], now)
        row.update({k: v for k, v in setup.items() if k != 'bars'})
        row['stage'] = 'ARMED'
        base_volume = sum(b['volume'] for b in setup['bars'][:-2])/5
        if base_volume <= 0 or setup['bars'][-2]['volume']/base_volume < 2:
            raise ValueError('stock_fast_underlying_volume_unconfirmed')
        row['underlying_volume_ratio'] = setup['bars'][-2]['volume']/base_volume
        expiry = datetime.strptime(chain._expiry, '%d%b%Y').replace(
            tzinfo=now.tzinfo, hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1])
        days = (expiry-now).total_seconds()/86400
        if days <= 0:
            raise ValueError('expired_contract')
        picks = []
        for pick in strike_selector.rank(chain, setup['direction'], data['spot'], days):
            q = pick.quote
            metric = data['fast_metrics'].get(str(q.token))
            if (pick.rejected or not accelerated(metric)
                    or not _fresh(metric.get('observed_at'), now)
                    or not all(_finite(v, .000001) for v in (q.bid, q.ask)) or q.ask < q.bid
                    or not _finite(q.iv, .000001) or not _finite(abs(q.delta) if q.delta is not None else None, .000001)
                    or (q.ask-q.bid)/((q.ask+q.bid)/2) > .015):
                continue
            picks.append((q, metric))
        if not picks:
            raise ValueError('stock_fast_no_accelerating_liquid_contract')
        q, metric = picks[0]
        stop = trade_policy.premium_stop(data['spot'], setup['spot_stop'], q.strike,
                                          q.ltp, days, setup['direction'])
        if not _finite(stop, .000001) or stop >= q.ask:
            raise ValueError('invalid_premium_stop')
        risk = trade_policy.risk_per_unit(q.ask, stop)
        score = round(50 + min(25, metric['premium_move_pct']/4*25)
                      + min(25, (metric['volume_ratio']-1)/2*25), 2)
        scenarios = fast.scenarios(q, data['spot'], setup['bars'], days, now)
        chosen = next((s for s in scenarios if s['minutes'] == cfg['holding_minutes']), None)
        if chosen is None:
            raise ValueError('stock_fast_horizon_unavailable')
        net_gain = chosen['central']*(1-trade_policy.EXIT_BUFFER)-q.ask*(1+trade_policy.COST_RESERVE)
        if not _finite(risk, .000001) or not _finite(net_gain, .000001) or net_gain/risk < 1.5:
            raise ValueError('scenario_net_reward_risk_below_1_5')
        row.update(symbol=q.symbol, token=q.token, exchange=chain.index.opt_exchange,
                   strike=q.strike, expiry=expiry.date().isoformat(), days=days,
                   ltp=q.ltp, entry=q.ask, stop=stop, score=score,
                   t1=round(q.ask+risk, 2), t2=round(q.ask+2*risk, 2),
                   scenarios=scenarios, model_net_reward_risk=round(net_gain/risk, 2),
                   episode='stock:'+chain.index.opt_exchange+':'+chain.index.name+':'
                           +setup['direction']+':'+setup['breakout_at']+':'+chain._expiry,
                   session_exit=session_exit(now).isoformat(), stage='ENTRY_CANDIDATE', **metric)
        row['blocked_reasons'].extend(fast.live_blocks(chain.index.name, row['direction'], now, score))
        if not cfg['live_enabled']:
            row['blocked_reasons'].append('stock_fast_live_disabled')
        row['eligible'] = not row['blocked_reasons']
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        row['blocked_reasons'].append(_public_reason(exc))
    return row


def bind_policy(row):
    from .fast_momentum import signal_id
    from . import execution_ledger
    sid = signal_id(row)
    submitted = execution_ledger.seen(sid)
    c = _connection()
    with c:
        c.execute('INSERT OR IGNORE INTO stock_fast_policies VALUES (?,?,?,?)',
                  (sid, row['episode'], row['holding_minutes'], row['session_exit']))
        if not submitted:
            c.execute('UPDATE stock_fast_policies SET hold_minutes=?, session_exit=? WHERE signal_id=?',
                      (row['holding_minutes'], row['session_exit'], sid))
    return sid


def deadline_reason(pos, now=None):
    """Persisted per-signal horizon survives pause, control changes and restart."""
    now = now or _now()
    row = _connection().execute('SELECT hold_minutes,session_exit FROM stock_fast_policies WHERE signal_id=?',
                                (pos.signal_id,)).fetchone()
    if row is None:
        return None
    if now >= datetime.fromisoformat(row[1]):
        return 'stock_fast_session_exit'
    if pos.entry_ts and now >= pos.entry_ts + timedelta(minutes=row[0]):
        return 'stock_fast_holding_deadline'
    return None


class Worker:
    """One independent preparation loop; orders use the existing shared executor."""
    def __init__(self, executor, scrips):
        from .stock_execution import StockExecutionWorker
        self.executor = executor
        self.helper = StockExecutionWorker(executor, scrips)
        self.stop_event = threading.Event()
        self.attempted = {}
        self.history_cache = OrderedDict()
        self.last_history = 0.0
        self.thread = None
        self.preparation_stage = 'idle'
        self.last_served = {}

    def history(self, index):
        from .session import SESSION
        from .stock_execution import parse_candles
        now = _now()
        key = (index.exchange, index.token)
        cached = self.history_cache.get(key)
        if cached and 0 <= time.monotonic()-cached[0] < 15:
            return cached[1]
        result = {}
        for interval, label in ((1, 'ONE_MINUTE'), (5, 'FIVE_MINUTE')):
            if self.stop_event.wait(max(0, 1.2-(time.monotonic()-self.last_history))):
                raise ValueError('stock_fast_worker_stopping')
            self.last_history = time.monotonic()
            SESSION.ensure()
            response = SESSION.api.getCandleData(dict(exchange=index.exchange,
                symboltoken=index.token, interval=label,
                fromdate=now.strftime('%Y-%m-%d 09:15'), todate=now.strftime('%Y-%m-%d %H:%M')))
            if not isinstance(response, dict) or response.get('status') is not True:
                raise ValueError('stock_fast_history_request_failed')
            # A halt or missing opening bar must not poison the entire day.
            # Restart indicator warmup after the latest gap; never fill gaps.
            result[interval] = parse_candles(response.get('data'), interval, _now(),
                                           recent_contiguous=True)
        self.history_cache[key] = (time.monotonic(), result)
        self.history_cache.move_to_end(key)
        while len(self.history_cache) > 32:
            self.history_cache.popitem(last=False)
        return result

    def protect(self):
        with self.executor._lock:
            held = list(self.executor.state.positions.values())
        for pos in held:
            if pos.kind != 'fast_momentum' or pos.status not in ('open', 'entry_pending'):
                continue
            reason = deadline_reason(pos)
            if reason:
                # The existing ledger handles partial fills and duplicate exit requests.
                self.executor.manual_exit(pos.signal_id, reason)

    def process(self, data):
        from . import audit
        from .indicators import Snapshot
        name = data['index']['name']
        self.preparation_stage = 'controls'
        cfg = controls()
        self.preparation_stage = 'contract_restore'
        chain = self.helper.restore_chain(data)
        self.preparation_stage = 'history'
        bars = self.history(chain.index)
        self.preparation_stage = 'snapshot_revalidation'
        if not still_current(data):
            raise ValueError('stock_fast_snapshot_changed_during_history')
        self.preparation_stage = 'planning'
        row = plan(data, chain, bars, cfg)
        if self.stop_event.is_set():
            row.update(eligible=False, blocked_reasons=row['blocked_reasons'] + ['stock_fast_worker_stopping'])
        if row['eligible']:
            self.preparation_stage = 'policy_binding'
            sid = bind_policy(row)
            row['signal_id'] = sid
            def revalidate():
                current = controls()
                if self.stop_event.is_set() or current['revision'] != cfg['revision'] or not still_current(data):
                    return None
                return plan(data, chain, bars, current)
            chain._entry_guard = lambda: (not self.stop_event.is_set() and still_current(data)
                and controls()['revision'] == cfg['revision'])
            self.preparation_stage = 'executor_dispatch'
            result = self.executor.evaluate_and_enter(name, row['direction'], chain,
                data['spot'], Snapshot(), row['days'], 'fast_momentum', sid,
                'EXPERIMENTAL', row['score'], fast_context=revalidate)
            row['dispatch'] = result
            audit.log('STOCK_FAST_DISPATCH_RESULT', signal_id=sid, index=name, **result)
        self.preparation_stage = 'audit_result'
        self.executor.fast_rows[name] = row
        audit.log('STOCK_FAST_CANDIDATE', **row)
        return row

    def cycle(self):
        from . import audit, celestial
        self.protect()  # Never conditional on the live-entry switch or market window.
        now = _now()
        data_rows = observations(now)
        warm = sum(bool(d.get('fast_metrics')) for d in data_rows)
        candidates = []
        for data in data_rows:
            name = data['index']['name']
            metrics = list(data.get('fast_metrics', {}).values())
            ready = [m for m in metrics if accelerated(m)]
            if ready:
                candidates.append(data)
            elif self.attempted.get(name) != data['chain_at']:
                self.executor.fast_rows[name] = dict(underlying=name, strategy='fast_momentum', asset_scope='STOCK',
                    stage='WATCH', ts=now.isoformat(), quote_at=data['chain_at'], eligible=False,
                    direction=None, scenarios=[], blocked_reasons=[
                        'stock_fast_premium_acceleration_unconfirmed' if metrics else 'stock_fast_history_warmup'])
        candidates.sort(key=lambda d: (self.last_served.get(d['index']['name'], 0),
            -max(m['premium_move_pct']*min(m['volume_ratio'], 5)
                 for m in d['fast_metrics'].values() if accelerated(m))))
        prepared = 0
        failed = 0
        for data in candidates:
            name = data['index']['name']
            if self.stop_event.is_set() or prepared >= 4:
                break  # Preparation budget is reported; universe observation is not truncated.
            if self.attempted.get(name) == data['chain_at']:
                continue
            self.attempted[name] = data['chain_at']
            self.last_served[name] = time.monotonic()
            prepared += 1
            try:
                self.process(data)
            except Exception as exc:
                from .preparation_diagnostics import describe
                failed += 1
                reason = _public_reason(exc, 'stock_fast_preparation_error')
                diagnostic = describe(exc, self.preparation_stage, reason)
                self.executor.fast_rows[name] = dict(underlying=name, strategy='fast_momentum', asset_scope='STOCK',
                    stage='BLOCKED', ts=_now().isoformat(), quote_at=data['chain_at'], eligible=False,
                    scenarios=[], blocked_reasons=[reason], **diagnostic)
                audit.log('STOCK_FAST_PREPARATION_REJECTED', index=name,
                          quote_at=data['chain_at'], **diagnostic)
        names = {d['index']['name'] for d in data_rows}
        self.attempted = {n: t for n, t in self.attempted.items() if n in names}
        self.last_served = {n: t for n, t in self.last_served.items() if n in names}
        for name, row in list(self.executor.fast_rows.items()):
            if row.get('asset_scope') == 'STOCK' and str(row.get('quote_at', ''))[:10] != now.date().isoformat():
                self.executor.fast_rows.pop(name, None)
            elif row.get('asset_scope') == 'STOCK' and not _fresh(row.get('quote_at'), _now()):
                row.update(eligible=False, stage='STALE', blocked_reasons=['stock_fast_stale_snapshot'])
        cfg = controls()
        celestial.health('stock_fast', dict(ok=failed == 0, preparation_failures=failed,
            live_requested=cfg['live_enabled'],
            holding_minutes=cfg['holding_minutes'], fresh_roots=len(data_rows), warmed_roots=warm,
            premium_candidates=len(candidates), prepared_this_cycle=prepared,
            preparation_pending=sum(self.attempted.get(d['index']['name']) != d['chain_at'] for d in candidates),
            quote_freshness_seconds=30, all_strikes=False,
            coverage='resolved_stock_roots_nearest_expiry_atm_plus_minus_one',
            execution='shared_guarded_executor', live_ready_not_implied=True))

    def run(self):
        from . import audit, celestial
        while not self.stop_event.is_set():
            try:
                self.cycle()
            except Exception as exc:
                audit.log('STOCK_FAST_WORKER_ERROR', error_type=type(exc).__name__)
                celestial.health('stock_fast', dict(ok=False, reason='worker_error'))
            self.stop_event.wait(2)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name='stock-fast-worker')
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
