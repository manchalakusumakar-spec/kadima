"""Scalp v2 - early-entry index scalping (2026-10-20). No broker/order calls.

Why: the 5-13 Oct audits showed index scalps entering after the move. The v1
lane (fast_momentum.Lane) waits for a breakout candle AND a hold candle to
close on 1-minute bars, then a 3-minute premium warm-up, and it is only
evaluated when that index's option chain is polled (about every 20-25 s).
Signals fired 1.3-2.9 min after the first cross, 1-10 points below the
burst's high, with a stop only 7-13 NIFTY points away (inside normal noise).

What v2 changes (index scalps only; Gamma, CAS and stock lanes untouched):
 1. Trigger on 15-second bars built from the index ticks the engine already
    receives: two consecutive 15 s closes beyond the 5-minute base range,
    skipped when price is already more than one base-width past the level.
 2. Arm when price is near the base edge and ask the OI loop to poll that
    index first and stop sleeping early (priority_poll switch).
 3. Stop = breakout level -/+ 1.5 x 1-minute ATR (noise-based), converted to a
    premium stop exactly as v1 does, with the 35% hard cap of trend_rules.
 4. Day trend first: counter-trend scalps are blocked (SIDEWAYS: both sides),
    using trend_rules.day_trend (the 19-Oct trader rules).
 5. Score v2 rewards a clean, early, with-trend breakout and penalises
    extension and a big 3-minute run-up, instead of rewarding the chase.

NIFTY spot backtest, 1-19 Oct 2026, 2-point cost: current rules -0.46R per
breakout; v2 (15 s trigger + trend + 1.5 ATR stop) +0.06R, better in both
halves of the month, firing a median 90 s earlier. Index spot is only a proxy
for option prices, hence shadow first.

Modes (DATA_DIR/scalp_v2.json, re-read every 30 s; env KADIMA_SCALP_V2 and
KADIMA_SCALP_V2_POLL override):
  {"mode": "off|shadow|live", "priority_poll": "off|shadow|live"}
  shadow - v2 signals are computed, audited and written to the Trade Audit as
           "Scalping v2 (shadow)" beside the v1 rows; v1 keeps trading as before.
  live   - v2 replaces v1 for index scalps; v1 rows are no longer dispatched.
  priority_poll live - the OI loop polls armed/triggered indices first and
           wakes early on a trigger. shadow only logs what it would reorder.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from statistics import mean

from . import audit
from .config import DATA_DIR, IST
from .utils import now_ist

BAR_SEC = 15              # trigger bar length
BASE_MIN = 5              # closed 1-minute bars forming the breakout range
CONFIRM_BARS = 2          # consecutive 15 s closes beyond the level
MIN_BEYOND = 0.10         # x base width: a close must clear the level by this much
MAX_EXT = 1.0             # x base width: no trigger once price is this far past
MAX_EXT_ENTRY = 1.25      # x base width: dispatch refused if it ran this far by then
ARM_ZONE = 0.30           # x base width below/above the level = armed
ATR_N = 14                # 1-minute ATR length
STOP_ATR = 1.5            # stop = level -/+ 1.5 x ATR
RUNUP_MAX_ATR = 3.0       # skip when the last 3 minutes ran more than this
TRIGGER_TTL_SEC = 45      # a trigger must be acted on within this
COOLDOWN_SEC = 300        # one trigger per index and direction per 5 minutes
ARM_TTL_SEC = 90
WINDOW = ((9, 25), (15, 10))
MAX_SPREAD = 0.015
DEFAULTS = {'mode': 'shadow', 'priority_poll': 'shadow'}
MODES = ('off', 'shadow', 'live')

_cfg = {'at': 0.0, 'value': None}
_lock = threading.RLock()
_wake = threading.Event()
_states: dict = {}
_recorded: dict = {}


# ------------------------------------------------------------------ config
def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'scalp_v2.json')) as f:
                value.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError, AttributeError):
            pass
        value['mode'] = os.environ.get('KADIMA_SCALP_V2', value['mode'])
        value['priority_poll'] = os.environ.get('KADIMA_SCALP_V2_POLL', value['priority_poll'])
        for k, d in DEFAULTS.items():
            if value[k] not in MODES:
                value[k] = d
        _cfg.update(at=now, value=value)
    return _cfg['value']


def mode():
    return config()['mode']


def _finite(*values):
    try:
        return all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


def in_window(t):
    return WINDOW[0] <= (t.hour, t.minute) < WINDOW[1]


# ------------------------------------------------------------------ bars
class _Bar:
    __slots__ = ('start', 'o', 'h', 'l', 'c')

    def __init__(self, start, price):
        self.start, self.o, self.h, self.l, self.c = start, price, price, price, price

    def add(self, price):
        self.h, self.l, self.c = max(self.h, price), min(self.l, price), price


class IndexBars:
    """15 s and 1 min bars for one index, from ticks. Same session only."""

    def __init__(self, name, token):
        self.name, self.token = name, str(token)
        self.day = None
        self.live15 = None
        self.live1 = None
        self.bars15 = deque(maxlen=40)
        self.bars1 = deque(maxlen=90)
        self.seeded = False
        self.done = set()          # (direction, level) already triggered today
        self.trigger = None
        self.armed = None
        self.last_trigger = {}     # direction -> trigger end time

    def _reset(self, day):
        self.day, self.live15, self.live1 = day, None, None
        self.bars15.clear(); self.bars1.clear()
        self.seeded, self.done, self.trigger, self.armed = False, set(), None, None
        self.last_trigger = {}

    def seed(self, rows):
        """Closed 1-minute rows from storage (start_ts, open, high, low, close)."""
        out = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r['start_ts'])
                t = t if t.tzinfo else t.replace(tzinfo=IST)
                if t.date() != self.day:
                    continue
                b = _Bar(t, float(r['open'])); b.h, b.l, b.c = float(r['high']), float(r['low']), float(r['close'])
                out.append(b)
            except (KeyError, TypeError, ValueError):
                continue
        have = {b.start for b in self.bars1}
        for b in out:
            if b.start not in have:
                self.bars1.append(b)
        self.bars1 = deque(sorted(self.bars1, key=lambda b: b.start), maxlen=90)
        self.seeded = True

    def add_tick(self, ts, price):
        """Returns the 15 s bar that just closed, or None."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        if ts.date() != self.day:
            self._reset(ts.date())
        s15 = ts.replace(second=ts.second - ts.second % BAR_SEC, microsecond=0)
        s1 = ts.replace(second=0, microsecond=0)
        closed = None
        if self.live15 is None or s15 > self.live15.start:
            if self.live15 is not None:
                closed = self.live15
                self.bars15.append(closed)
            self.live15 = _Bar(s15, price)
        else:
            self.live15.add(price)
        if self.live1 is None or s1 > self.live1.start:
            if self.live1 is not None and (not self.bars1 or self.bars1[-1].start < self.live1.start):
                self.bars1.append(self.live1)
            self.live1 = _Bar(s1, price)
        else:
            self.live1.add(price)
        return closed

    # -------------------------------------------------------------- inputs
    def closed1(self, before):
        return [b for b in self.bars1 if b.start + timedelta(minutes=1) <= before]

    def atr(self, before):
        b = self.closed1(before)[-(ATR_N + 1):]
        if len(b) < 5:
            return None
        trs = [max(x.h - x.l, abs(x.h - p.c), abs(x.l - p.c)) for p, x in zip(b, b[1:])]
        return mean(trs) if trs else None

    def five_context(self, before):
        """Close of the last of 3 closed 5-min bars minus the first (v1 rule)."""
        groups: dict = {}
        for b in self.closed1(before):
            k = b.start.replace(minute=b.start.minute - b.start.minute % 5)
            groups.setdefault(k, []).append(b)
        done = [(k, v) for k, v in sorted(groups.items()) if k + timedelta(minutes=5) <= before and len(v) >= 4]
        if len(done) < 3:
            return None
        return done[-1][1][-1].c - done[-3][1][-1].c


# ------------------------------------------------------------------ trigger
def evaluate(bars: IndexBars, closed: _Bar, now=None):
    """Pure decision on one closed 15 s bar. Returns ('trigger'|'arm', info) or None."""
    end = closed.start + timedelta(seconds=BAR_SEC)
    if not in_window(end) or len(bars.bars15) < CONFIRM_BARS:
        return None
    minute = closed.start.replace(second=0)
    base = [b for b in bars.closed1(minute)][-BASE_MIN:]
    if len(base) < BASE_MIN or base[-1].start + timedelta(minutes=1) != minute:
        return None   # need the 5 minutes right before this one, no gaps
    hi, lo = max(b.h for b in base), min(b.l for b in base)
    width = mean(b.h - b.l for b in base)
    atr = bars.atr(minute)
    ctx = bars.five_context(minute)
    if not _finite(width, atr) or ctx is None:
        return None
    last = list(bars.bars15)[-CONFIRM_BARS:]
    three = [b for b in bars.bars15 if b.start <= end - timedelta(minutes=3)]
    ref = three[-1].c if three else None
    best = None
    for direction, level, sign in (('CE', hi, 1), ('PE', lo, -1)):
        beyond = [sign * (b.c - level) for b in last]
        ext = beyond[-1] / width
        if all(x > MIN_BEYOND * width for x in beyond) and ext <= MAX_EXT and sign * ctx > 0:
            if (direction, round(level, 2)) in bars.done:
                continue
            prev = bars.last_trigger.get(direction)
            if prev is not None and (end - prev).total_seconds() < COOLDOWN_SEC:
                continue
            runup = sign * (closed.c - ref) / atr if ref is not None else 0.0
            if runup > RUNUP_MAX_ATR:
                audit.log('SCALP_V2_SKIPPED', index=bars.name, direction=direction, reason='run_up_over_3_atr',
                          level=round(level, 2), runup_atr=round(runup, 2))
                bars.done.add((direction, round(level, 2)))
                continue
            info = dict(index=bars.name, direction=direction, level=round(level, 2), width=round(width, 2),
                        atr=round(atr, 2), spot=closed.c, extension_w=round(ext, 2), runup_atr=round(runup, 2),
                        spot_stop=round(level - sign * STOP_ATR * atr, 2), at=end.isoformat(),
                        base_start=base[0].start.isoformat())
            best = ('trigger', info)
        elif 0 <= sign * (level - closed.c) <= ARM_ZONE * width and sign * ctx > 0 and best is None:
            best = ('arm', dict(index=bars.name, direction=direction, level=round(level, 2), at=end.isoformat()))
    return best


def on_tick(name, token, ts, price):
    """Engine tick hook (tick thread). Arithmetic only, plus one storage read per
    index per day to seed 1-minute history. Never raises."""
    try:
        if mode() == 'off' or not _finite(price):
            return
        with _lock:
            bars = _states.get(name)
            if bars is None or bars.token != str(token):
                bars = _states[name] = IndexBars(name, token)
            closed = bars.add_tick(ts, float(price))
            if not bars.seeded:
                try:
                    from . import storage
                    bars.seed(storage.recent_candles(bars.token, 1, 90))
                except Exception:  # noqa: BLE001 - seeding is best effort
                    bars.seeded = True
            if closed is None:
                return
            result = evaluate(bars, closed)
            if result is None:
                return
            kind, info = result
            if kind == 'arm':
                bars.armed = dict(info, until=time.monotonic() + ARM_TTL_SEC)
                return
            bars.done.add((info['direction'], info['level']))
            bars.last_trigger[info['direction']] = datetime.fromisoformat(info['at'])
            bars.trigger = dict(info, mono=time.monotonic())
            bars.armed = None
        audit.log('SCALP_V2_TRIGGER', mode=mode(), **info)
        _wake.set()
    except Exception as exc:  # noqa: BLE001 - must never disturb the tick feed
        try:
            audit.log('scalp_v2_tick_error', index=name, error=type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass


def active_trigger(name):
    with _lock:
        bars = _states.get(name)
        t = bars.trigger if bars else None
        if t and 0 <= time.monotonic() - t['mono'] <= TRIGGER_TTL_SEC:
            return dict(t)
    return None


# ------------------------------------------------------------------ score
def score(trigger, trend_label, premium_move_pct=None, extension_now=None):
    """0-100. Rewards an early, clean, with-trend breakout; penalises chasing."""
    ext = trigger['extension_w'] if extension_now is None else extension_now
    s = 50.0
    s += 20.0 if ext <= 0.5 else max(0.0, 20.0 * (1 - (ext - 0.5) / 0.75))
    s += 15.0 if trend_label in ('UP', 'DOWN') else 5.0
    if premium_move_pct is not None:
        if premium_move_pct <= 6:
            s += 15.0 * max(0.0, min(premium_move_pct, 3.0)) / 3.0
        else:
            s -= min(15.0, (premium_move_pct - 6) * 2)
    if trigger['runup_atr'] > 2:
        s -= min(10.0, 10.0 * (trigger['runup_atr'] - 2))
    return round(max(0.0, min(100.0, s)), 2)


# ------------------------------------------------------------------ row
def build_row(st, trigger, lane=None, now=None):
    """A fast-lane-shaped row for this trigger (poll thread). Same field names the
    executor reads for kind 'fast_momentum'; `engine` marks it as v2."""
    from . import celestial, strike_selector, trade_policy, trend_rules
    from .config import GAMMA_INDICES
    from copy import copy
    now = now or now_ist()
    sign = 1 if trigger['direction'] == 'CE' else -1
    row = dict(underlying=st.name, strategy='fast_momentum', engine='scalp_v2', ts=now.isoformat(),
               stage='BLOCKED', blocked_reasons=[], direction=trigger['direction'], scenarios=[],
               trigger_at=trigger['at'], level=trigger['level'], spot_stop=trigger['spot_stop'],
               atr=trigger['atr'], width=trigger['width'], runup_atr=trigger['runup_atr'],
               episode='v2:' + st.name + ':' + trigger['direction'] + ':' + trigger['at'])
    try:
        chain = st.chain
        if not 0 <= time.monotonic() - chain._last_poll <= 30:
            raise ValueError('option_chain_stale')
        if not celestial.fresh(st.last_updated.isoformat() if st.last_updated else None, now):
            raise ValueError('underlying_quote_stale')
        spot = st.spot
        ext = sign * (spot - trigger['level']) / trigger['width']
        row['extension_w_at_entry'] = round(ext, 2)
        if ext <= 0:
            raise ValueError('breakout_failed_before_entry')
        if ext > MAX_EXT_ENTRY:
            raise ValueError('extended_before_entry')
        if sign * (spot - trigger['spot_stop']) <= 0:
            raise ValueError('invalid_structure')
        vwap = trend_rules.vwap_for(st.name, now) or getattr(getattr(st, 'snap', None), 'vwap', None)
        label = trend_rules.day_trend(st.name, chain.index.token, spot, vwap, now)
        row['trend'] = label
        expiry = datetime.strptime(chain._expiry, '%d%b%Y').replace(tzinfo=IST, hour=15, minute=30)
        days = (expiry - now).total_seconds() / 86400
        if days <= 0:
            raise ValueError('expired_contract')
        view = copy(chain)
        view.quotes = {k: copy(q) for k, q in chain.quotes.items() if q.tradeable
                       and all(celestial.finite(v, .000001) for v in (q.ltp, q.bid, q.ask))
                       and q.ask >= q.bid and (q.ask - q.bid) / q.ask <= MAX_SPREAD}
        picks = [r for r in strike_selector.rank(view, trigger['direction'], spot, days) if not r.rejected]
        if not picks:
            raise ValueError('no_liquid_contract')
        q = picks[0].quote
        row.update(symbol=q.symbol, token=q.token, exchange=chain.index.opt_exchange, strike=q.strike,
                   expiry=expiry.date().isoformat(), ltp=q.ltp, days=days,
                   quote_at=(now - timedelta(seconds=time.monotonic() - chain._last_poll)).isoformat())
        pace = None
        if lane is not None and st.name in getattr(lane, 'histories', {}):
            pace = lane.histories[st.name].metrics(chain, q, now)
        move = pace['premium_move_pct'] if pace else None
        if move is not None and move < 0:
            raise ValueError('premium_not_following')
        stop = trade_policy.premium_stop(spot, trigger['spot_stop'], q.strike, q.ltp, days, q.opt_type)
        entry = q.ask
        if _finite(stop, entry):
            stop = max(stop, entry * (1 - trend_rules.HARD_CAP))
        if not _finite(stop) or stop >= entry:
            raise ValueError('invalid_premium_stop')
        risk = trade_policy.risk_per_unit(entry, stop)
        row['score'] = score(trigger, label, move, ext)
        row.update(entry=entry, stop=round(stop, 2), t1=round(entry + risk, 2), t2=round(entry + 2 * risk, 2),
                   premium_move_pct=move, stage='SETUP_READY',
                   signal_delay_sec=round((now - datetime.fromisoformat(trigger['at'])).total_seconds(), 1))
        if trend_rules.against_trend(label, trigger['direction']):
            row['blocked_reasons'].append('counter_trend_scalp')
        from . import config as cfg
        from .oi_confirmation import oi_confirmation, scalp_oi_block
        if getattr(cfg, 'OI_CONFIRM_MODE', None) == 'live' and st.name in GAMMA_INDICES:
            confirmation = oi_confirmation(st.name, now)
            row['oi_confirmation'] = confirmation
            block = scalp_oi_block(trigger['direction'], now, confirmation)
            if block:
                row['blocked_reasons'].append(block)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        row['blocked_reasons'].append(str(exc) if type(exc) is ValueError else 'invalid_market_inputs')
    try:
        from . import fast_momentum
        row['blocked_reasons'].extend(fast_momentum.live_blocks(st.name, trigger['direction'], now,
                                                                row.get('score', 0)))
    except Exception:  # noqa: BLE001
        row['blocked_reasons'].append('live_gate_unavailable')
    if mode() != 'live':
        row['blocked_reasons'].append('scalp_v2_shadow')
    row['eligible'] = row['stage'] == 'SETUP_READY' and not row['blocked_reasons']
    return row


def _no_trigger(st, now):
    return dict(underlying=st.name, strategy='fast_momentum', engine='scalp_v2', ts=now.isoformat(),
                stage='BLOCKED', blocked_reasons=['scalp_v2_no_trigger'], direction=None,
                scenarios=[], eligible=False)


def merge(st, v1_row, lane=None, record=True, now=None):
    """Called from the engine's fast scan with the v1 row. off/shadow: returns the
    v1 row unchanged (shadow also builds, audits and records the v2 row). live:
    returns the v2 row for an active trigger, else a blocked placeholder."""
    try:
        m = mode()
        if m == 'off':
            return v1_row
        now = now or now_ist()
        trig = active_trigger(st.name)
        row = build_row(st, trig, lane, now) if trig else None
        if row is not None and record:
            key = (row['episode'], row['stage'], tuple(row['blocked_reasons']))
            if _recorded.get(st.name) != key:
                _recorded[st.name] = key
                audit.log('SCALP_V2_CANDIDATE', mode=m,
                          v1_stage=(v1_row or {}).get('stage'), v1_episode=(v1_row or {}).get('episode'),
                          **{k: v for k, v in row.items() if k != 'scenarios'})
                if m == 'shadow' and row.get('entry') is not None:
                    from . import trade_audit
                    trade_audit.safe_call(trade_audit.record, row, 'SCALP_V2', 'scalp_v2')
        if m == 'live':
            return row if row is not None else _no_trigger(st, now)
        return v1_row
    except Exception as exc:  # noqa: BLE001 - never break the established lane
        audit.log('scalp_v2_merge_error', index=getattr(st, 'name', None), error=type(exc).__name__)
        return v1_row


def audit_code(row):
    return 'SCALP_V2' if isinstance(row, dict) and row.get('engine') == 'scalp_v2' else 'SCALP'


# ------------------------------------------------------------------ OI loop
def order(states):
    """Index states for one OI-loop pass: triggered, then armed, first (live)."""
    values = list(states.values()) if isinstance(states, dict) else list(states)
    p = config()['priority_poll']
    if p == 'off' or mode() == 'off':
        return values
    now = time.monotonic()

    def rank(st):
        with _lock:
            b = _states.get(st.name)
            if b and b.trigger and now - b.trigger['mono'] <= TRIGGER_TTL_SEC:
                return 0
            if b and b.armed and now <= b.armed['until']:
                return 1
        return 2
    ranked = sorted(values, key=rank)   # stable: equal ranks keep the old order
    if p == 'shadow':
        if [s.name for s in ranked] != [s.name for s in values]:
            audit.log('scalp_v2_poll_order_shadow', would=[s.name for s in ranked][:3])
        return values
    return ranked


def wait(seconds, stop_event=None):
    """Replaces time.sleep(OI_POLL_SEC): returns early on a trigger when live."""
    if config()['priority_poll'] != 'live' or mode() == 'off':
        time.sleep(seconds)
        return
    _wake.clear()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return
        if _wake.wait(min(0.5, max(0.0, deadline - time.monotonic()))):
            audit.log('scalp_v2_poll_woken_early')
            return
