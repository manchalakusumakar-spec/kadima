"""Expiry premium-awakening lane. Separate from Shield; no broker calls.

Numeric thresholds are initial hypotheses, not calibrated probabilities. Source
VWAP/time/depth are mandatory; successful REST receipt alone is not freshness.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
import time

from . import celestial, storage
from .config import IST
from .utils import now_ist


@dataclass(frozen=True)
class Policy:
    max_capital: float = 15_000.0
    max_premium: float = 50.0
    min_premium: float = 1.0
    max_spread_pct: float = 1.5
    min_velocity_pct: float = 3.0
    max_chase_multiple: float = 1.35
    add_multiple: float = 1.15
    max_add_multiple: float = 1.50
    stop_fraction: float = .05
    entry_end: tuple = (15, 25)
    force_exit: tuple = (15, 28)


POLICY = Policy()


def positive(*values):
    try:
        return all(not isinstance(v, bool) and math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


def source_time(value):
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            result = datetime.strptime(value, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
    if result.tzinfo is None:
        raise ValueError('source_time_without_timezone')
    return result.astimezone(IST)


def fresh(value, now, seconds=30):
    try:
        return 0 <= (now-source_time(value)).total_seconds() <= seconds
    except (ValueError, TypeError, OverflowError):
        return False


def quote_valid(q, now):
    return (positive(q.strike, q.ltp, q.bid, q.ask, q.option_vwap, q.bid_qty, q.ask_qty, q.volume)
            and q.ask >= q.bid and q.spread_pct <= POLICY.max_spread_pct
            and fresh(q.quote_at, now))


def live_blocks(now):
    reasons = []
    if os.environ.get('KADIMA_CAS_LIVE', '0') != '1':
        reasons.append('cas_observation_only')
    cfg = celestial.read_config()
    if not celestial.enabled(): reasons.append('rollout_disabled')
    if not cfg['auto_trade_enabled']: reasons.append('auto_paused')
    if cfg['kill_latched']: reasons.append('kill_latched')
    if not (9, 15) <= (now.hour, now.minute) < POLICY.entry_end:
        reasons.append('cas_entry_window_closed')
    return reasons


def connection():
    c = storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS cas_runs '
              '(episode TEXT PRIMARY KEY, signal_id INTEGER UNIQUE, payload TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS cas_locks '
              '(episode TEXT PRIMARY KEY, reason TEXT NOT NULL)')
    return c


def run(episode):
    row = connection().execute('SELECT * FROM cas_runs WHERE episode=?', (episode,)).fetchone()
    return dict(signal_id=row['signal_id'], **json.loads(row['payload'])) if row else None


def lock(episode, reason):
    c = connection()
    with c:
        c.execute('INSERT OR IGNORE INTO cas_locks VALUES (?,?)', (episode, reason))


def locked(episode):
    row = connection().execute('SELECT reason FROM cas_locks WHERE episode=?', (episode,)).fetchone()
    return row[0] if row else None


def reserve(row, allocation, max_lots):
    """Reserve a session/side once, before any order. Crash means no blind retry."""
    c = connection()
    with c:
        if run(row['episode']) or locked(row['episode']): return None
        cur = c.execute('INSERT INTO signals (ts,index_name,tier,score,direction,payload,kind) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (row['ts'], row['underlying'], 'CAS ENTRY', 0, row['direction'],
                         json.dumps(row, allow_nan=False), 'cas'))
        data = dict(token=row['token'], symbol=row['symbol'], exchange=row['exchange'],
                    anchor=row['entry'], allocation=allocation, max_lots=max_lots,
                    attempts=1, expiry=row['expiry'])
        c.execute('INSERT INTO cas_runs VALUES (?,?,?)',
                  (row['episode'], cur.lastrowid, json.dumps(data)))
        return cur.lastrowid


def reserve_add(episode):
    c = connection()
    with c:
        saved = run(episode)
        if not saved or saved['attempts'] != 1 or locked(episode): return False
        saved.pop('signal_id')
        saved['attempts'] = 2
        c.execute('UPDATE cas_runs SET payload=? WHERE episode=?', (json.dumps(saved), episode))
    return True


class Lane:
    def __init__(self):
        self.history = {}
        self.rows = {}
        self.day = None

    def metrics(self, expiry, q, now, record):
        if self.day != now.date():
            self.history.clear()
            self.day = now.date()
        key = (expiry, q.token, q.symbol)
        samples = self.history.setdefault(key, deque(maxlen=16))
        at = source_time(q.quote_at)
        if record and (not samples or at > samples[-1][0]):
            if samples and ((at-samples[-1][0]).total_seconds() > 90 or q.volume < samples[-1][3]):
                samples.clear()
            samples.append((at, q.ltp, q.option_vwap, q.volume))
        if len(samples) < 3 or (at, q.ltp, q.option_vwap, q.volume) != samples[-1]:
            return None
        recent = [s for s in samples if 0 <= (at-s[0]).total_seconds() <= 180]
        if len(recent) < 3 or (at-recent[0][0]).total_seconds() < 60:
            return None
        a, b, c = recent[-3:]
        dt = (c[0]-b[0]).total_seconds()
        if dt <= 0 or b[3] <= a[3] or c[3] <= b[3]: return None
        velocity = (c[1]/b[1]-1)*100*30/dt
        reclaim = any(s[1] <= s[2] for s in recent[:-1]
                      if (at-s[0]).total_seconds() <= 90) and c[1] > c[2]
        return dict(velocity_pct_30s=velocity, reclaimed=reclaim,
                    rising_twice=c[1] > b[1] > a[1], baseline=min(s[1] for s in recent),
                    incremental_volume=c[3]-b[3])

    def scan(self, st, positions, now=None, record=True):
        from .option_chain import OptionQuote
        now = (now or now_ist()).astimezone(IST)
        sample = getattr(st.chain, '_forecast_sample', None) or {}
        rows = []
        for direction in ('CE', 'PE'):
            row = dict(underlying=st.name, strategy='cas', direction=direction,
                       ts=now.isoformat(), stage='CAS WATCH', eligible=False, blocked_reasons=[])
            try:
                expiry = datetime.strptime(sample['expiry'], '%d%b%Y').date()
                episode = f'{expiry}:{st.name}:{direction}'
                row.update(episode=episode, expiry=expiry.isoformat())
                saved = run(episode)
                pos = positions.get(str(saved['signal_id'])) if saved else None
                if saved and (pos is None or pos.status == 'closed'):
                    lock(episode, 'cas_episode_finished_or_submission_interrupted')
                reason = locked(episode)
                if reason:
                    row['stage'] = 'CAS LOCK'
                    raise ValueError(reason)
                if expiry != now.date(): raise ValueError('not_contract_expiry_day')
                if not 0 <= time.monotonic()-sample['mono'] <= 30:
                    raise ValueError('option_sample_stale')
                if not fresh(st.last_updated, now) or not getattr(st, 'is_live_tick', False):
                    raise ValueError('underlying_quote_stale')
                if not fresh(st.technical_at, now, 240): raise ValueError('underlying_indicators_stale')
                snap = st.snap
                if not positive(st.spot, snap.vwap, snap.ema_fast):
                    raise ValueError('underlying_direction_unavailable')
                sign = 1 if direction == 'CE' else -1
                supports = sign*(st.spot-snap.vwap) > 0 and sign*(st.spot-snap.ema_fast) > 0
                row['underlying_supports'] = supports
                picks = []
                step = st.chain.index.strike_step
                if not positive(step): raise ValueError('strike_step_unavailable')
                atm = math.floor(st.spot/step + .5)*step
                for raw in sample.get('quotes', []):
                    q = OptionQuote(**raw)
                    if q.opt_type != direction or not quote_valid(q, now): continue
                    if saved and (q.token != saved['token'] or q.symbol != saved['symbol']): continue
                    if not saved and abs(q.strike-atm) > step: continue
                    metrics = self.metrics(sample['expiry'], q, now, record)
                    if metrics is None: continue
                    picks.append((q, metrics))
                if not picks: raise ValueError('source_vwap_depth_or_60s_history_unavailable')
                picks.sort(key=lambda p: (abs(p[0].strike-atm), p[0].spread_pct, -p[1]['velocity_pct_30s']))
                # Find an executable cheap candidate before falling back to a watch row.
                if not saved:
                    ready = [(q, m) for q, m in picks if POLICY.min_premium <= q.ask <= POLICY.max_premium
                             and m['reclaimed'] and m['velocity_pct_30s'] >= POLICY.min_velocity_pct
                             and q.ask <= m['baseline']*POLICY.max_chase_multiple]
                    if ready: picks = ready
                q, metrics = picks[0]
                row.update(metrics, token=q.token, symbol=q.symbol, strike=q.strike,
                           exchange=st.chain.index.opt_exchange, entry=q.ask, ltp=q.ltp,
                           bid=q.bid, ask=q.ask, ask_qty=q.ask_qty, quote_at=source_time(q.quote_at).isoformat(),
                           option_vwap=q.option_vwap, stop=round(q.ask*(1-POLICY.stop_fraction), 2),
                           fast_target_zone=[round(q.ask*1.25, 2), round(q.ask*1.50, 2)],
                           entry_trigger='Fresh option VWAP reclaim + positive premium velocity + underlying alignment',
                           invalidation='5% premium stop, option VWAP loss or underlying reversal',
                           trail_exit_cue='Retain 75% of gain after +25%; exit spike pullback or 15:28 IST')
                if saved:
                    row.update(signal_id=saved['signal_id'], stage='CAS ENTRY', filled_qty=pos.remaining_qty,
                               broker_status=pos.status, entry=pos.entry_price or saved['anchor'],
                               stop=pos.effective_stop or pos.structural_stop,
                               fast_target_zone=[round(saved['anchor']*1.25, 2), round(saved['anchor']*1.5, 2)])
                    row['exit_reason'] = exit_reason(pos, q.bid, row, now)
                    if row['exit_reason'] or pos.exit_reason or pos.status == 'exit_pending':
                        row['stage'] = 'CAS EXIT'
                        raise ValueError(row['exit_reason'] or pos.exit_reason or 'exit_pending')
                    if pos.status != 'open' or pos.remaining_qty <= 0:
                        raise ValueError('awaiting_broker_fill')
                    if saved['attempts'] >= 2: raise ValueError('cas_add_already_attempted')
                    if not (saved['anchor']*POLICY.add_multiple <= q.bid
                            and q.ask <= saved['anchor']*POLICY.max_add_multiple
                            and q.bid > pos.entry_price and metrics['rising_twice']):
                        raise ValueError('cas_add_expansion_unconfirmed')
                    row['stage'] = 'CAS ADD'
                else:
                    if q.ask >= metrics['baseline']*2 and metrics['baseline'] <= POLICY.max_premium:
                        lock(episode, 'large_move_already_observed')
                        row['stage'] = 'CAS LOCK'
                        raise ValueError('large_move_already_observed')
                    if not POLICY.min_premium <= q.ask <= POLICY.max_premium:
                        raise ValueError('premium_outside_cheap_band')
                    if not metrics['reclaimed']: raise ValueError('option_vwap_reclaim_not_observed')
                    if q.ask > metrics['baseline']*POLICY.max_chase_multiple:
                        raise ValueError('cas_entry_extended')
                if not supports: raise ValueError('underlying_opposes')
                if q.ltp <= q.option_vwap: raise ValueError('option_vwap_lost')
                if metrics['velocity_pct_30s'] < POLICY.min_velocity_pct:
                    raise ValueError('premium_velocity_not_awake')
                if not saved: row['stage'] = 'CAS ENTRY'
                row['blocked_reasons'].extend(live_blocks(now))
                impact = celestial.news_impact(st.name, direction, now)
                if impact['news_blocked']: row['blocked_reasons'].append('news_risk_block')
                row['eligible'] = not row['blocked_reasons']
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                row['blocked_reasons'].append(str(exc) if type(exc) is ValueError else 'invalid_cas_inputs')
            rows.append(row)
        if record: self.rows[st.name] = rows
        return rows


def exit_reason(pos, price, row, now):
    """Protection remains active independently of entry flags and Shield state."""
    if pos.kind != 'cas': return None
    clock = now.astimezone(IST)
    if (clock.hour, clock.minute) >= POLICY.force_exit or clock.date() != pos.entry_ts.astimezone(IST).date():
        return 'cas_expiry_decay_exit'
    if not positive(price, pos.entry_price): return None
    peak = max(pos.peak_price or pos.entry_price, price)
    stop = max(pos.effective_stop or 0, pos.structural_stop or 0,
               pos.entry_price*(1-POLICY.stop_fraction))
    if peak >= pos.entry_price*1.25:
        stop = max(stop, pos.entry_price+(peak-pos.entry_price)*.75)
    if price <= stop: return 'cas_premium_stop_or_profit_trail'
    if peak >= pos.entry_price*2 and price <= peak*.90:
        return 'cas_spike_pullback'
    if row and fresh(row.get('ts'), now) and fresh(row.get('quote_at'), now):
        if row.get('underlying_supports') is False: return 'cas_underlying_reversed'
        if positive(row.get('option_vwap')) and price < row['option_vwap']:
            return 'cas_option_vwap_lost'
    if (now-pos.entry_ts).total_seconds() >= 120 and peak < pos.entry_price*1.05:
        return 'cas_no_expansion_2min'
    return None
