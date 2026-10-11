"""INTRADAY TREND v2 - separate index intraday-trend lane. Observation by default.

Scope (trader's instruction, 10-11 Oct 2026): a strategy of its own, identity
``intraday_trend``. It does not read or change Gamma, CAS, Bull/Bear Shield or
the 5/10/15-minute fast scalping lane, and it has no order writer: an eligible
setup is handed to the shared ``AutoExecutor.evaluate_and_enter`` exactly like
the other lanes, so every shared safeguard stays authoritative.

v2 (11 Oct 2026) rebuilds the entry and exit rules on the trader's Master
Checklist v8.0 after the v1 replay lost on both calibration and validation days.
The rules come from the checklist, not from fitting the replay:
  setups        pullback_recovery (09:45-14:30) and afternoon_breakout (13:00-14:30);
                opening_continuation removed (bought the first spike, 0 of 6 won)
  confirmation  the setup bar must be confirmed by the NEXT closed 5-min bar (s.10)
  structure     CE needs a higher high + higher low, PE a lower high + lower low;
                the opening range side is recorded as evidence only
  Shield        Master Checklist v9.0 score (VWAP and volume from index futures,
                EMA20/50, RSI > 55 / < 45, OI = writing / covering / unwinding,
                failed break) >= 70 in the trade direction on the setup bar and the
                confirmation bar (Bank Nifty: one more bar, >= 2 of HDFC, ICICI,
                Kotak, SBI aligned, OI velocity 12%); -20% after 13:00 pre-expiry;
                PUT OI > 500% after 12:30 makes the OI leg sit out
  macro         no new entry in a FOMC/RBI/major US data window, or when unknown
  stop          beyond the structure level by max(5 pts, 0.25 x avg 5-min range)
  target        full exit at 2R (minimum RR 1:2; one lot cannot be part-booked)
  invalidation  exit if the EMA trend flips against the trade within 2 candles
  CE wall       exit a CE trade when a CE strike within 50 pts builds >= 150% OI
  unchanged     no-progress exit at 45 min below +5%, 15:10 time exit, 1 lot,
                at most 3 episodes per underlying per day, strike claims
Known limits: index volume is zero, so the volume leg never scores and the VWAP
leg compares spot with the last close until index-futures VWAP is wired; the
engine's per-bar Shield history and bank alignment are not wired into the live
hook, so the live lane fails closed (shield_unavailable). v2 runs offline only
(replay / practice mode). Live entry would also need KADIMA_INTRADAY_TREND_LIVE=1.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import time
import uuid
from copy import copy
from datetime import datetime, time as dtime, timedelta

from . import storage, trade_policy
from .config import IST
from .utils import now_ist

STRATEGY = 'intraday_trend'
POLICY = dict(
    id='intraday_trend_v2',
    interval_min=5,
    first_decision=(9, 30),
    last_entry=(14, 30),
    pullback_window=((9, 45), (14, 30)),
    afternoon_window=((13, 0), (14, 30)),
    consolidation_from=(12, 0),
    max_extension_ranges=1.0,      # close beyond level by at most 1x avg bar range
    max_pullback_ranges=2.0,       # pullback depth at most 2x avg bar range
    max_consolidation_ranges=3.0,  # afternoon range width at most 3x avg bar range
    ema_period=9,
    technical_max_age_sec=240,
    chain_max_age_sec=30,
    spot_max_age_sec=30,
    context_max_age_sec=30,
    max_spread_pct=1.5,
    max_stop_pct=35.0,             # premium stop at most 35% below entry
    lots=1,
    max_episodes_per_underlying_per_day=3,
    no_progress_min=45,
    no_progress_gain_pct=5.0,
    time_exit=(15, 10),
    decision_retention_days=30,
    # v2 (11 Oct 2026): rules taken from the trader's Master Checklist v8.0, not fitted to data.
    confirm_next_bar=True,         # s.10: enter only after trigger confirmation; never the first spike
    shield_min_score=70.0,         # Bull Shield v2.1 ACTIVE: >=70 in the setup direction ...
    shield_hold_bars=2,            # ... on the setup bar AND one extra 5-min bar
    shield_hold_bars_banknifty=3,  # Bank Nifty variant: two extra candles
    bank_min_confirm=2,            # Bank Nifty variant: >=2 of HDFC, ICICI, Kotak, SBI
    pre_expiry_cap_after=(13, 0),  # postmortem rule: -20% on Shield scores after 13:00 pre-expiry
    pre_expiry_cap_factor=0.8,
    stop_buffer_ranges=0.25,       # stop beyond the structure level, not on it
    stop_buffer_min_points=5.0,
    target_r=2.0,                  # minimum RR 1:2; full exit at 2R (1 lot cannot be split)
    invalidation_bars=2,           # EMA flips against the trade within 2 candles -> exit
    # trader's checklist (11 Oct 2026, 23:39) and v8.0 additions
    rsi_bull=55.0, rsi_bear=45.0,  # Bull Shield RSI > 55, Bear Shield RSI < 45
    rsi_recovery_min=50.0,         # v8.0 RSI recovery gate (CE never below 50)
    oi_velocity_pct=15.0, oi_velocity_pct_banknifty=12.0,
    put_oi_ambiguity_after=(12, 30), put_oi_ambiguity_pct=500.0,
    ce_wall_build_pct=150.0, ce_wall_points=50.0,   # fresh CE wall -> exit (1 lot: full, not 40%)
    macro_unknown_blocks=True,     # macro suppression window active or unknown -> no new entry
)
_SOURCE_SHA = hashlib.sha256(open(__file__, 'rb').read()).hexdigest()
POLICY_ID = POLICY['id'] + ':' + hashlib.sha256(
    json.dumps(POLICY, sort_keys=True).encode()).hexdigest()[:12]


def live_requested():
    return os.environ.get('KADIMA_INTRADAY_TREND_LIVE', '0') == '1'


def _t(hm):
    return dtime(*hm)


def _finite(*values):
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


# ------------------------------------------------------------------ closed bars
def closed_bars(rows, now, interval=5):
    """Today's closed bars only, in time order. A bar is used only once
    start + interval <= now, so nothing after the decision time is read."""
    out = []
    for source in rows:
        r = dict(source)
        try:
            t = datetime.fromisoformat(r['start_ts'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('invalid_bars')
        if t.tzinfo is None:
            raise ValueError('invalid_bars')
        if t.date() != now.date() or t + timedelta(minutes=interval) > now:
            continue
        if r.get('closed', 1) not in (1, True) or not trade_policy.positive(
                r.get('open'), r.get('high'), r.get('low'), r.get('close')):
            raise ValueError('invalid_bars')
        if not r['low'] <= min(r['open'], r['close']) <= max(r['open'], r['close']) <= r['high']:
            raise ValueError('invalid_bars')
        r['at'] = t
        out.append(r)
    out.sort(key=lambda r: r['at'])
    if not out:
        raise ValueError('candle_warmup')
    age = (now - out[-1]['at']).total_seconds() - interval * 60
    if not 0 <= age <= interval * 60 + 15:
        raise ValueError('stale_candles')
    return out


def _contiguous(bars, interval=5):
    return all((b['at'] - a['at']).total_seconds() == interval * 60 for a, b in zip(bars, bars[1:]))


def _ema(values, period):
    k = 2 / (period + 1)
    out, ema = [], None
    for v in values:
        ema = v if ema is None else v * k + ema * (1 - k)
        out.append(ema)
    return out


def _avg_range(bars):
    return statistics.mean(b['high'] - b['low'] for b in bars)


# ------------------------------------------------------------------ trend check
def trend_alignment(direction, snap):
    """EMA33 vs EMA83 and RSI 50 on the 3-min snapshot. No VWAP leg: index spot
    has no volume, so the snapshot's 'VWAP' is only the last candle's close
    (audit finding, 11 Oct). Switch to index-futures VWAP once that is live."""
    try:
        fast, slow, rsi = snap.ema_fast, snap.ema_slow, snap.rsi
        if not _finite(fast, slow, rsi) or fast <= 0 or slow <= 0 or not 0 <= rsi <= 100:
            return dict(ema=False, rsi=False)
        bull = direction == 'CE'
        return dict(ema=fast > slow if bull else fast < slow, rsi=rsi > 50 if bull else rsi < 50)
    except AttributeError:
        return dict(ema=False, rsi=False)


# ------------------------------------------------------------------ setups
def _pullback_recovery(bars, last, avg):
    lo_t, hi_t = POLICY['pullback_window']
    if not _t(lo_t) <= last['at'].time() < _t(hi_t):
        return None, 'outside_pullback_window'
    if len(bars) < POLICY['ema_period'] + 1:
        return None, 'ema_warmup'
    window = bars[-(POLICY['ema_period'] + 1):]
    if not _contiguous(window):
        return None, 'candle_gap'
    emas = _ema([b['close'] for b in bars], POLICY['ema_period'])
    prior, ema_prior, ema_last = bars[-4:-1], emas[-4:-1], emas[-1]
    if len(prior) < 3 or not _contiguous(bars[-4:]):
        return None, 'candle_gap'
    before = bars[-5] if len(bars) >= 5 else None   # bar before the 3-bar pullback
    if last['close'] > ema_last and last['close'] > bars[-2]['high']:
        touched = any(b['low'] <= e for b, e in zip(prior, ema_prior))
        swing_hi = max(b['high'] for b in ([before] if before else []) + prior)
        depth = swing_hi - min(b['low'] for b in prior)
        if touched and depth <= POLICY['max_pullback_ranges'] * avg:
            return dict(direction='CE', level=bars[-2]['high'],
                        spot_stop=min(b['low'] for b in prior)), None
        return None, 'no_shallow_pullback_to_ema'
    if last['close'] < ema_last and last['close'] < bars[-2]['low']:
        touched = any(b['high'] >= e for b, e in zip(prior, ema_prior))
        swing_lo = min(b['low'] for b in ([before] if before else []) + prior)
        depth = max(b['high'] for b in prior) - swing_lo
        if touched and depth <= POLICY['max_pullback_ranges'] * avg:
            return dict(direction='PE', level=bars[-2]['low'],
                        spot_stop=max(b['high'] for b in prior)), None
        return None, 'no_shallow_pullback_to_ema'
    return None, 'no_recovery_close'


def _afternoon_breakout(bars, last, avg):
    lo_t, hi_t = POLICY['afternoon_window']
    if not _t(lo_t) <= last['at'].time() < _t(hi_t):
        return None, 'outside_afternoon_window'
    base = [b for b in bars[:-1] if b['at'].time() >= _t(POLICY['consolidation_from'])]
    if len(base) < 6 or not _contiguous(base + [last]):
        return None, 'consolidation_incomplete'
    hi, lo = max(b['high'] for b in base), min(b['low'] for b in base)
    if hi - lo > POLICY['max_consolidation_ranges'] * avg:
        return None, 'no_consolidation'
    if last['close'] > hi:
        return dict(direction='CE', level=hi, spot_stop=last['low']), None
    if last['close'] < lo:
        return dict(direction='PE', level=lo, spot_stop=last['high']), None
    return None, 'no_close_beyond_consolidation'


# v2: opening_continuation removed (0 wins in 6 replayed trades; it bought the first
# spike before confirmation, against checklist s.10).
FAMILIES = (('afternoon_breakout', _afternoon_breakout),
            ('pullback_recovery', _pullback_recovery))


def shield_score(snap, oi_side=None, futures=None):
    """Bull/Bear Shield score (trader's checklist, 11 Oct 2026), v8.0 weights:
      VWAP reclaim/rejection 25  - index-FUTURES session VWAP only; the index's own
                                   "VWAP" is the close (no volume) and never counts
      EMA20/50 20                - snapshot fast/slow EMAs
      RSI 20                     - bull > 55, bear < 45; 45-55 scores neither side
      Volume spike 15            - futures 5-min volume above each of the previous 5
      OI shift 15                - oi_side(): bull = put writing / short covering,
                                   bear = call writing / long unwinding
      Failed breakdown/breakout 5
    futures: dict(vwap, close, vol, prev_vols) or None (those legs sit out, no rescaling).
    Returns (direction, score, legs)."""
    bull = bear = 0.0
    legs = {}
    fut = futures or {}
    if _finite(fut.get('vwap'), fut.get('close')) and fut['vwap'] > 0:
        side = 'CE' if fut['close'] > fut['vwap'] else 'PE' if fut['close'] < fut['vwap'] else None
        if side:
            legs['vwap'] = side
    if getattr(snap, 'ema_bull', None) is not None:
        legs['ema'] = 'CE' if snap.ema_bull else 'PE'
    rsi = getattr(snap, 'rsi', None)
    if rsi is not None and _finite(rsi):
        legs['rsi'] = 'CE' if rsi > POLICY['rsi_bull'] else 'PE' if rsi < POLICY['rsi_bear'] else None
    prev = fut.get('prev_vols') or []
    if len(prev) >= 5 and _finite(fut.get('vol'), *prev) and fut['vol'] > max(prev) and legs.get('vwap'):
        legs['volume'] = legs['vwap']
    if oi_side in ('CE', 'PE'):
        legs['oi'] = oi_side
    if getattr(snap, 'failed_breakdown', None):
        legs['failed_break'] = 'CE'
    elif getattr(snap, 'failed_breakout', None):
        legs['failed_break'] = 'PE'
    weights = dict(vwap=25.0, ema=20.0, rsi=20.0, volume=15.0, oi=15.0, failed_break=5.0)
    for leg, side in legs.items():
        if side == 'CE':
            bull += weights[leg]
        elif side == 'PE':
            bear += weights[leg]
    return ('CE' if bull >= bear else 'PE'), max(bull, bear), legs


def oi_side(rows, history, spot, underlying, now, day_open=None):
    """ATM OI direction from the polled option chain (+/-1.5% of spot), 10-min window.
      bull: put writing (PE OI up) or call short covering (CE wall OI down)
      bear: call writing (CE OI up) or put long unwinding (PE wall OI down)
    Threshold 15% (Bank Nifty 12%, checklist variant). PUT OI ambiguity rule: after
    12:30, a put strike whose OI is up >500% on the day makes the OI leg sit out.
    rows: latest poll [{token, strike, opt_type, oi}], history: {token: [(ts, oi)]},
    day_open: {token: first OI of the day}. Returns 'CE', 'PE' or None."""
    if not rows or not _finite(spot) or spot <= 0:
        return None
    thr = (POLICY['oi_velocity_pct_banknifty'] if underlying == 'BANKNIFTY'
           else POLICY['oi_velocity_pct']) / 100
    bull = bear = 0
    for r in rows:
        strike, typ, oi = r.get('strike'), r.get('opt_type'), r.get('oi')
        if typ not in ('CE', 'PE') or not _finite(strike, oi) or abs(strike - spot) > 0.015 * spot:
            continue
        opened = (day_open or {}).get(r['token'])
        if typ == 'PE' and opened and now.time() >= _t(POLICY['put_oi_ambiguity_after']) \
                and oi > opened * (1 + POLICY['put_oi_ambiguity_pct'] / 100):
            return None
        then = None
        for ts, past in history.get(r['token'], ()):
            if (now - ts).total_seconds() >= 600:
                then = past
            else:
                break
        if not then:
            continue
        change = (oi - then) / then
        if change >= thr:
            bull, bear = (bull + 1, bear) if typ == 'PE' else (bull, bear + 1)
        elif change <= -thr:
            bull, bear = (bull + 1, bear) if typ == 'CE' else (bull, bear + 1)
    return 'CE' if bull > bear else 'PE' if bear > bull else None


def structure_ok(bars, direction):
    """Checklist market structure on 5-min swings: CE needs a higher high and a higher
    low versus the previous 6 bars; PE a lower high and a lower low."""
    if len(bars) < 12:
        return False
    recent, prior = bars[-6:], bars[-12:-6]
    if direction == 'CE':
        return max(b['high'] for b in recent) > max(b['high'] for b in prior) and \
            min(b['low'] for b in recent) > min(b['low'] for b in prior)
    return max(b['high'] for b in recent) < max(b['high'] for b in prior) and \
        min(b['low'] for b in recent) < min(b['low'] for b in prior)


def opening_range_side(bars):
    """Opening range (09:15-09:30) position of the latest close: evidence only."""
    opening = [b for b in bars if b['at'].time() < _t(POLICY['first_decision'])]
    if len(opening) != 3:
        return None
    if bars[-1]['close'] > max(b['high'] for b in opening):
        return 'CE'
    if bars[-1]['close'] < min(b['low'] for b in opening):
        return 'PE'
    return 'inside'


def _shield_gate(direction, shield, underlying, bar_close, days_to_expiry):
    """Checklist v8.0: score >= shield_min_score in the trade direction on the setup
    bar and the confirmation bar (Bank Nifty: one more), consecutive 5-min closes.
    shield: [{'closed_at': datetime, 'direction': 'CE'|'PE', 'score': float}, ...]."""
    if not shield:
        return 'shield_unavailable'
    need = POLICY['shield_hold_bars_banknifty' if underlying == 'BANKNIFTY' else 'shield_hold_bars']
    rows = sorted(shield, key=lambda r: r['closed_at'])[-need:]
    if len(rows) < need or rows[-1]['closed_at'] != bar_close:
        return 'shield_not_held'
    for i, r in enumerate(rows):
        if i and (r['closed_at'] - rows[i - 1]['closed_at']).total_seconds() != 300:
            return 'shield_not_held'
        score = r['score']
        if days_to_expiry == 1 and r['closed_at'].time() >= _t(POLICY['pre_expiry_cap_after']):
            score *= POLICY['pre_expiry_cap_factor']
        if r['direction'] != direction:
            return 'shield_direction_against'
        if not _finite(score) or score < POLICY['shield_min_score']:
            return 'shield_below_active'
    return None


def detect(rows, spot, snap, technical_at, now, shield=None, banks=None,
           days_to_expiry=None, underlying=None, macro_active=None):
    """v2: a setup found on the previous closed 5-min bar and confirmed by the
    latest closed bar (checklist s.10), then the Bull Shield and bank gates.

    Returns (setup, evaluations). The same confirmation bar always yields the same
    episode, so a restart or repeated poll can never create a second opportunity."""
    bars = closed_bars(rows, now)
    last = bars[-1]
    if last['at'].time() < _t(POLICY['first_decision']):
        raise ValueError('before_first_decision')
    if last['at'].time() >= _t(POLICY['last_entry']):
        raise ValueError('after_last_entry')
    if len(bars) < 2 or not _contiguous(bars[-2:]):
        raise ValueError('candle_warmup')
    setup_bars, setup_bar = bars[:-1], bars[-2]
    avg = _avg_range(setup_bars[-6:])
    if not _finite(avg) or avg <= 0:
        raise ValueError('flat_underlying')
    bar_close = last['at'] + timedelta(minutes=5)
    if macro_active or (macro_active is None and POLICY['macro_unknown_blocks']):
        raise ValueError('macro_suppression_window_or_unknown')
    evaluations = {}
    for family, fn in FAMILIES:
        setup, reason = fn(setup_bars, setup_bar, avg)
        if setup is None:
            evaluations[family] = reason
            continue
        direction, sign = setup['direction'], 1 if setup['direction'] == 'CE' else -1
        if sign * (last['close'] - setup_bar['close']) <= 0 or sign * (last['close'] - setup['level']) <= 0:
            evaluations[family] = 'not_confirmed_by_next_bar'
            continue
        if sign * (last['close'] - setup['level']) > POLICY['max_extension_ranges'] * avg:
            evaluations[family] = 'breakout_extended'
            continue
        structure = min(setup['spot_stop'], last['low']) if sign > 0 else max(setup['spot_stop'], last['high'])
        buffer = max(POLICY['stop_buffer_min_points'], POLICY['stop_buffer_ranges'] * avg)
        spot_stop = structure - sign * buffer
        if not trade_policy.positive(spot) or sign * (spot - setup['level']) <= 0:
            evaluations[family] = 'breakout_invalidated_at_decision'
            continue
        if sign * (spot - spot_stop) <= 0:
            evaluations[family] = 'invalid_structure'
            continue
        if technical_at is None or not 0 <= (now - technical_at).total_seconds() <= POLICY['technical_max_age_sec']:
            evaluations[family] = 'trend_snapshot_stale'
            continue
        aligned = trend_alignment(direction, snap)
        if not all(aligned.values()):
            evaluations[family] = 'trend_not_aligned:' + ','.join(k for k, v in aligned.items() if not v)
            continue
        if not structure_ok(bars, direction):
            evaluations[family] = 'structure_not_hh_hl' if direction == 'CE' else 'structure_not_lh_ll'
            continue
        gate = _shield_gate(direction, shield, underlying, bar_close, days_to_expiry)
        if gate:
            evaluations[family] = gate
            continue
        if underlying == 'BANKNIFTY':
            if banks is None:
                evaluations[family] = 'bank_confirmation_unavailable'
                continue
            if sum(banks.get(b) == direction for b in ('HDFCBANK', 'ICICIBANK', 'KOTAKBANK', 'SBIN')) \
                    < POLICY['bank_min_confirm']:
                evaluations[family] = 'bank_confirmation_missing'
                continue
        evaluations[family] = 'SETUP'
        return dict(family=family, direction=direction, level=setup['level'],
                    spot_stop=round(spot_stop, 2), structure_level=structure,
                    setup_bar_start=setup_bar['at'].isoformat(), bar_start=last['at'].isoformat(),
                    decided_at=bar_close.isoformat(), avg_range=round(avg, 2),
                    opening_range=opening_range_side(bars)), evaluations
    return None, evaluations


# ------------------------------------------------------------------ persistence
def connection():
    c = storage.conn()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS intraday_trend_episodes (
        episode TEXT PRIMARY KEY, underlying TEXT NOT NULL, day TEXT NOT NULL,
        signal_id INTEGER NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS intraday_trend_decisions (
        decision_id TEXT PRIMARY KEY, ts TEXT NOT NULL, episode TEXT, underlying TEXT NOT NULL,
        stage TEXT NOT NULL, policy_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
        payload TEXT NOT NULL);
    ''')
    return c


def episodes_today(underlying, now):
    return connection().execute('SELECT COUNT(*) FROM intraday_trend_episodes WHERE underlying=? AND day=?',
                                (underlying, now.date().isoformat())).fetchone()[0]


def signal_id(row):
    """One immutable signal per episode; repeated calls return the same id."""
    c = connection()
    with c:
        found = c.execute('SELECT signal_id FROM intraday_trend_episodes WHERE episode=?',
                          (row['episode'],)).fetchone()
        if found:
            return found[0]
        cur = c.execute('INSERT INTO signals (ts,index_name,tier,score,direction,payload,kind) '
                        'VALUES (?,?,?,?,?,?,?)', (row['ts'], row['underlying'], 'TREND', 0.0,
                        row['direction'], json.dumps(row, default=str), STRATEGY))
        c.execute('INSERT INTO intraday_trend_episodes VALUES (?,?,?,?,?)',
                  (row['episode'], row['underlying'], row['ts'][:10], cur.lastrowid, row['ts']))
        return cur.lastrowid


def record(stage, row, **extra):
    """Append-only decision record (never updated). Retention is bounded."""
    payload = dict(row, **extra)
    c = connection()
    with c:
        c.execute('INSERT INTO intraday_trend_decisions VALUES (?,?,?,?,?,?,?,?)',
                  (uuid.uuid4().hex, payload.get('ts') or now_ist().isoformat(), payload.get('episode'),
                   payload.get('underlying', ''), stage, POLICY_ID, _SOURCE_SHA,
                   json.dumps(payload, default=str)))


def prune(now):
    cutoff = (now - timedelta(days=POLICY['decision_retention_days'])).isoformat()
    c = connection()
    with c:
        c.execute('DELETE FROM intraday_trend_decisions WHERE ts < ?', (cutoff,))


def recent_decisions(limit=50):
    rows = connection().execute('SELECT ts, stage, payload FROM intraday_trend_decisions '
                                'ORDER BY ts DESC LIMIT ?', (min(int(limit), 200),)).fetchall()
    return [dict(json.loads(r[2]), recorded_at=r[0], record_stage=r[1]) for r in rows]


# ------------------------------------------------------------------ gates
def live_blocks(now=None):
    from . import celestial
    reasons = []
    if not live_requested():
        reasons.append('intraday_trend_observation_only')
    if celestial.enabled():
        cfg = celestial.read_config()
        if not cfg['auto_trade_enabled']:
            reasons.append('auto_paused')
        if cfg['kill_latched']:
            reasons.append('kill_latched')
    return reasons


def reentry_block(positions, underlying, setup, now):
    """Fresh re-entry only after a confirmed exit and a genuinely new setup."""
    exits = []
    for p in list((positions or {}).values()):
        if getattr(p, 'kind', None) != STRATEGY or getattr(p, 'index', None) != underlying:
            continue
        if getattr(p, 'status', None) != 'closed':
            return 'previous_intraday_trend_exit_unconfirmed'
        if getattr(p, 'exit_ts', None):
            exits.append(p.exit_ts)
    found = connection().execute(
        "SELECT MAX(exit_ts) FROM positions WHERE kind=? AND index_name=? AND status='closed' "
        "AND exit_ts LIKE ?", (STRATEGY, underlying, now.date().isoformat() + '%')).fetchone()
    if found and found[0]:
        exits.append(datetime.fromisoformat(found[0]))
    if exits and datetime.fromisoformat(setup['bar_start']) < max(exits):
        return 'setup_not_new_since_previous_exit'
    return None


def context_valid(context, underlying, direction, now):
    """Executor-side check of a dispatched context (freshness, identity, live gates)."""
    try:
        stamp = datetime.fromisoformat(context['ts'])
        return (context.get('eligible') is True and context.get('strategy') == STRATEGY
                and context.get('underlying') == underlying and context.get('direction') == direction
                and stamp.tzinfo is not None
                and 0 <= (now - stamp).total_seconds() <= POLICY['context_max_age_sec']
                and not live_blocks(now))
    except (TypeError, KeyError, ValueError, AttributeError):
        return False


def still_valid(latest, original, underlying, direction, now):
    keys = ('episode', 'token', 'symbol', 'exchange', 'direction', 'spot_stop')
    return (context_valid(latest, underlying, direction, now)
            and all(latest.get(k) == original.get(k) for k in keys))


INTRADAY_SPREAD_EXIT_PCT = 3.0


def celestial_exit_guard(pos, now):
    """In Celestial mode intraday positions skip Celestial's scalping-style exits
    (fake index VWAP, 1-min EMA9, opposite OI/Gamma, news) and keep only the
    liquidity guard: exit if the option's spread widens above 3%."""
    from . import celestial
    quote = celestial.health('quote:' + pos.exchange + ':' + pos.token)
    if quote and celestial.fresh(quote.get('ts'), now):
        bid, ask = quote.get('bid'), quote.get('ask')
        if bid and ask and 200 * (ask - bid) / (ask + bid) > INTRADAY_SPREAD_EXIT_PCT:
            return 'spread_widened_above_3pct'
    return None


def fresh_ce_wall(spot, rows_now, oi_at_entry):
    """Checklist OI wall rule: a CE strike within ce_wall_points of spot whose OI has
    grown by >= ce_wall_build_pct since entry."""
    if not _finite(spot) or not rows_now or not oi_at_entry:
        return False
    for r in rows_now:
        base = oi_at_entry.get(r.get('token'))
        if r.get('opt_type') == 'CE' and _finite(r.get('strike'), r.get('oi'), base) and base > 0 \
                and abs(r['strike'] - spot) <= POLICY['ce_wall_points'] \
                and r['oi'] >= base * (1 + POLICY['ce_wall_build_pct'] / 100):
            return True
    return False


def exit_reason(pos, now, snap=None, ltp=None, ce_wall=False):
    """Intraday-trend holding policy only; every other kind returns None.
    v2 adds: full exit at target_r x initial risk (checklist minimum RR 1:2), and
    exit when the EMA trend flips against the trade within invalidation_bars
    candles of entry (checklist auto-invalidation)."""
    if getattr(pos, 'kind', None) != STRATEGY:
        return None
    if now.astimezone(IST).time() >= _t(POLICY['time_exit']):
        return 'intraday_trend_time_exit'
    if not pos.entry_ts or not pos.entry_price:
        return None
    stop = getattr(pos, 'structural_stop', None)
    if ltp is not None and _finite(ltp, stop) and 0 < stop < pos.entry_price and \
            ltp >= pos.entry_price + POLICY['target_r'] * (pos.entry_price - stop):
        return 'intraday_trend_target_2r'
    if ce_wall and getattr(pos, 'direction', None) == 'CE':
        return 'intraday_trend_fresh_ce_wall'
    held = (now - pos.entry_ts).total_seconds()
    if snap is not None and held <= POLICY['invalidation_bars'] * 300 and \
            getattr(pos, 'direction', None) in ('CE', 'PE') and \
            trend_alignment(pos.direction, snap)['ema'] is False:
        return 'intraday_trend_shield_invalidated'
    peak = pos.peak_price or pos.entry_price
    if held >= POLICY['no_progress_min'] * 60 and peak < pos.entry_price * (1 + POLICY['no_progress_gain_pct'] / 100):
        return 'intraday_trend_no_progress_45min'
    return None


# ------------------------------------------------------------------ strike claims
# Trader's rule (11 Oct 2026): when engines want the same strike, before 15:15
# Gamma > Scalping > Intraday (CAS leads from 15:15, after intraday's 15:10 exit).
# Gamma and scalping take profits in 2-5 minutes; intraday holds longer, so
# intraday steers clear: it never takes a strike another engine holds, or one
# Gamma or scalping currently has selected, and tries its next-ranked contract.
# This only READS the other engines' state; it changes nothing in them.
ENGINE_OF_KIND = {'gamma_blast': 'GAMMA', 'fast_momentum': 'SCALPING', 'cas': 'CAS', 'cas_blast': 'CAS',
                  'bull_shield': 'SHIELD', 'bear_shield': 'SHIELD', 'gap_and_go': 'GAP',
                  'mcx_momentum': 'MCX', 'forecast_manual': 'MANUAL', 'MANUAL_ADOPTED': 'MANUAL',
                  STRATEGY: 'INTRADAY'}


def strike_claims(executor):
    """{(exchange, token): reason} for contracts intraday must not take.
    Returns None if any source cannot be read, so the caller fails closed."""
    from . import celestial
    claims = {}
    try:
        for p in list(executor.state.positions.values()):
            if getattr(p, 'status', None) != 'closed':
                engine = ENGINE_OF_KIND.get(getattr(p, 'kind', None), 'OTHER')
                claims[(str(p.exchange), str(p.token))] = 'held_by_' + engine
        for row in list((getattr(executor, 'fast_rows', None) or {}).values()):
            if row.get('eligible') is True and row.get('token') and row.get('exchange'):
                claims.setdefault((str(row['exchange']), str(row['token'])), 'strike_claimed_by_SCALPING')
        if celestial.enabled():
            for row in celestial.top20():
                if row.get('eligible') is True and row.get('exchange') and row.get('token'):
                    claims.setdefault((str(row['exchange']), str(row['token'])), 'strike_claimed_by_GAMMA')
    except Exception:          # unknown claims: do not risk taking a contested strike
        return None
    return claims


# ------------------------------------------------------------------ lane
class Lane:
    """Setup observation (on each closed 5-min bar) kept separate from order
    admission (on the next fresh chain poll). Every setup gets a recorded outcome."""

    def __init__(self):
        self.pending = {}   # underlying -> setup row awaiting a fresh chain poll
        self.rows = {}      # underlying -> latest row, for display
        self.last_attempt = {}
        self._pruned = None

    def observe_close(self, st, now=None):
        now = now or now_ist()
        if self._pruned != now.date():
            prune(now)
            self._pruned = now.date()
        row = dict(underlying=st.name, strategy=STRATEGY, policy_id=POLICY_ID, ts=now.isoformat(),
                   stage='NO_SETUP', blocked_reasons=[], evaluations={})
        try:
            # v2 gates need the engine's per-bar Shield history and bank alignment;
            # until those are wired (not in this release) they are None and the lane
            # fails closed with shield_unavailable. Practice mode runs offline.
            setup, evaluations = detect(storage.recent_candles(st.chain.index.token, 5, 90),
                                        st.spot, st.snap, getattr(st, 'technical_at', None), now,
                                        shield=getattr(st, 'trend_shield', None),
                                        banks=getattr(st, 'trend_banks', None),
                                        days_to_expiry=getattr(st, 'trend_days_to_expiry', None),
                                        underlying=st.name,
                                        macro_active=getattr(st, 'trend_macro_active', None))
            row['evaluations'] = evaluations
            if st.name in self.pending:
                self._close(st.name, 'SETUP_EXPIRED', reason='superseded_by_next_bar')
            if setup is None:
                row['blocked_reasons'].append('no_setup')
            else:
                episode = ':'.join(('IT', st.name, setup['family'], setup['direction'], setup['bar_start']))
                row.update(setup, episode=episode, stage='SETUP_OBSERVED')
                self.pending[st.name] = row
                record('SETUP_OBSERVED', row)
        except ValueError as exc:
            row['blocked_reasons'].append(str(exc) if str(exc) in {
                'invalid_bars', 'candle_warmup', 'stale_candles', 'before_first_decision',
                'after_last_entry', 'flat_underlying',
                'macro_suppression_window_or_unknown'} else 'invalid_market_inputs')
        except (TypeError, KeyError, AttributeError, OverflowError):
            row['blocked_reasons'].append('invalid_market_inputs')
        self.rows[st.name] = row
        return row

    def prepare(self, st, positions, now=None, claims=None):
        """Rank liquid contracts against this policy on a fresh chain poll.
        claims: strike_claims() result; None means unreadable and blocks entry."""
        from . import strike_selector
        now = now or now_ist()
        setup = self.pending.get(st.name)
        if setup is None:
            return None
        row = {k: v for k, v in setup.items() if k not in ('blocked_reasons',)}
        row.update(ts=now.isoformat(), blocked_reasons=[], contracts=[], stage='BLOCKED', eligible=False)
        try:
            if now >= datetime.fromisoformat(setup['decided_at']) + timedelta(minutes=5):
                self._close(st.name, 'SETUP_EXPIRED', reason='setup_expired_next_bar')
                raise ValueError('setup_expired_next_bar')
            chain = st.chain
            if not 0 <= time.monotonic() - chain._last_poll <= POLICY['chain_max_age_sec']:
                raise ValueError('option_chain_stale')
            if st.last_updated is None or not 0 <= (now - st.last_updated).total_seconds() <= POLICY['spot_max_age_sec']:
                raise ValueError('underlying_quote_stale')
            sign = 1 if setup['direction'] == 'CE' else -1
            if sign * (st.spot - setup['level']) <= 0:
                self._close(st.name, 'SETUP_FAILED', reason='breakout_failed_before_entry')
                raise ValueError('breakout_failed_before_entry')
            block = reentry_block(positions, st.name, setup, now)
            if block:
                raise ValueError(block)
            if episodes_today(st.name, now) >= POLICY['max_episodes_per_underlying_per_day'] and \
                    connection().execute('SELECT 1 FROM intraday_trend_episodes WHERE episode=?',
                                         (setup['episode'],)).fetchone() is None:
                raise ValueError('daily_episode_cap_reached')
            if claims is None:
                raise ValueError('strike_claims_unavailable')
            expiry = datetime.strptime(chain._expiry, '%d%b%Y').replace(tzinfo=IST, hour=15, minute=30)
            days = (expiry - now).total_seconds() / 86400
            if days <= 0:
                raise ValueError('expired_contract')
            view = copy(chain)
            view.quotes = {k: copy(q) for k, q in chain.quotes.items()}
            ranked = strike_selector.rank(view, setup['direction'], st.spot, days)
            chosen = None
            for r in ranked:
                q, evidence = r.quote, dict(token=r.quote.token, symbol=r.quote.symbol,
                                            strike=r.quote.strike, rank_score=round(r.score, 1))
                if (chain.index.opt_exchange, str(q.token)) in claims:
                    evidence['reason'] = claims[(chain.index.opt_exchange, str(q.token))]
                elif r.rejected:
                    evidence['reason'] = 'ranking:' + str(r.rejected)
                elif not getattr(q, 'tradeable', True) or not trade_policy.positive(q.ltp, q.bid, q.ask) \
                        or q.ask < q.bid:
                    evidence['reason'] = 'quote_not_tradeable'
                elif (q.ask - q.bid) / q.ask * 100 > POLICY['max_spread_pct']:
                    evidence['reason'] = 'spread_too_wide'
                else:
                    stop = trade_policy.premium_stop(st.spot, setup['spot_stop'], q.strike, q.ltp,
                                                     days, q.opt_type)
                    if not trade_policy.positive(stop) or stop >= q.ask:
                        evidence['reason'] = 'invalid_premium_stop'
                    elif (q.ask - stop) / q.ask * 100 > POLICY['max_stop_pct']:
                        evidence['reason'] = 'stop_wider_than_policy'
                    else:
                        risk = trade_policy.risk_per_unit(q.ask, stop)
                        evidence.update(entry=q.ask, stop=round(stop, 2),
                                        t1=round(q.ask + risk, 2), t2=round(q.ask + 2 * risk, 2))
                        if chosen is None:   # first passing contract in rank order
                            evidence['reason'] = 'SELECTED'
                            chosen = (q, stop, risk, days)
                        else:
                            evidence['reason'] = 'PASSED_NOT_SELECTED'
                row['contracts'].append(evidence)   # every ranked contract gets a reason
            if chosen is None:
                raise ValueError('no_contract_meets_intraday_policy')
            q, stop, risk, days = chosen
            row.update(token=q.token, symbol=q.symbol, exchange=chain.index.opt_exchange, strike=q.strike,
                       expiry=expiry.date().isoformat(), days=days, entry=q.ask, stop=round(stop, 2),
                       t1=round(q.ask + risk, 2), t2=round(q.ask + 2 * risk, 2),
                       exit_policy=dict(time_exit='15:10', no_progress_min=POLICY['no_progress_min'],
                                        no_progress_gain_pct=POLICY['no_progress_gain_pct'],
                                        trail='shared_ratchet', lots=POLICY['lots']),
                       stage='SETUP_READY')
        except ValueError as exc:
            row['blocked_reasons'].append(str(exc))
        except (TypeError, KeyError, AttributeError, OverflowError):
            row['blocked_reasons'].append('invalid_market_inputs')
        if row['stage'] == 'SETUP_READY':
            row['blocked_reasons'].extend(live_blocks(now))
        row['eligible'] = row['stage'] == 'SETUP_READY' and not row['blocked_reasons']
        self.rows[st.name] = row
        if st.name in self.pending:
            self.last_attempt[st.name] = row
        return row

    def _close(self, underlying, stage, **extra):
        """A setup that never reached the executor still gets an outcome record,
        carrying the last admission attempt's reasons."""
        setup = self.pending.pop(underlying, None)
        if setup is not None:
            last = self.last_attempt.pop(underlying, None) or {}
            record(stage, setup, last_blocked_reasons=last.get('blocked_reasons', []),
                   last_contracts=last.get('contracts', []), **extra)

    def revalidate(self, st, executor):
        """Fresh re-evaluation used by the executor right before submission,
        including a fresh read of other engines' strike claims."""
        return self.prepare(st, executor.state.positions, claims=strike_claims(executor))

    def settle(self, underlying, row, result):
        """Record the dispatch outcome; the setup is spent unless the shared
        ledger allows a delivery-failure re-send for the same signal."""
        latest = self.rows.get(underlying) or {}
        record('DISPATCH', row, dispatch=result,   # contract actually used after the final revalidation
               submitted={k: latest.get(k) for k in ('token', 'symbol', 'strike', 'entry', 'stop', 'contracts')})
        if not (result or {}).get('retryable'):
            self.pending.pop(underlying, None)
            self.last_attempt.pop(underlying, None)
