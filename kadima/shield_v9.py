"""Bull/Bear Shield per KADIMA Master Checklist v9.0 (trader, 11 Oct 2026).

One definition shared by the engine (shadow, or live behind a switch), the
intraday trend lane and the hourly forecaster.

Score legs (v8.0 weights, v9.0 definitions):
  VWAP reclaim / rejection  25  index FUTURES close vs futures session VWAP.
                                The index's own "VWAP" is its close (no traded
                                volume) and never counts.
  EMA20 / EMA50             20  snapshot fast/slow EMA
  RSI                       20  bull > 55, bear < 45; 45-55 scores neither side
  Volume spike              15  futures 5-min volume above each of the previous 5
  ATM OI shift              15  bull = put writing or CE short covering,
                                bear = call writing or PE long unwinding
                                (+/-1.5% of spot, 10-minute window, 15%; Bank Nifty 12%)
  Failed breakdown/breakout  5
A missing leg sits out; the score is never rescaled.

Tiers: WATCH >= 40 (Bank Nifty 45); ACTIVE >= 70 held one extra 5-min candle
(Bank Nifty two); CONFIRMED >= 90 held. Bank Nifty also needs >= 2 of HDFC,
ICICI, Kotak, SBI aligned for ACTIVE and CONFIRMED. Mandatory rules: -20% on
scores after 13:00 on pre-expiry days; PUT OI > 500% on the day after 12:30
makes the OI leg sit out; macro window active (or its status unknown) caps the
tier at WATCH.

Pure functions plus two small history holders. No broker call, no order, no I/O.
"""
from __future__ import annotations

import math
import os
from collections import deque
from datetime import datetime, time as dtime, timedelta

WEIGHTS = dict(vwap=25.0, ema=20.0, rsi=20.0, volume=15.0, oi=15.0, failed_break=5.0)
RSI_BULL, RSI_BEAR = 55.0, 45.0
OI_PCT = {'BANKNIFTY': 12.0}
OI_PCT_DEFAULT = 15.0
OI_BAND = 0.015
PUT_OI_AMBIGUITY_AFTER = dtime(12, 30)
PUT_OI_AMBIGUITY_PCT = 500.0
PRE_EXPIRY_AFTER = dtime(13, 0)
PRE_EXPIRY_FACTOR = 0.8
WATCH = {'BANKNIFTY': 45.0}
WATCH_DEFAULT = 40.0
ACTIVE, CONFIRMED = 70.0, 90.0
EXTRA_HOLD = {'BANKNIFTY': 2}
EXTRA_HOLD_DEFAULT = 1
BANK_MIN = 2
MIDDAY = (dtime(11, 45), dtime(13, 15))
TREND_DAY_FRACTION = 0.6
FUTURES_EXCHANGE = {'NSE': 'NFO', 'BSE': 'BFO'}


def live_enabled():
    """KADIMA_SHIELD_V9_LIVE=1 makes the engine trade on this score. Default off:
    shadow only (logged and displayed, read by nothing that trades)."""
    return os.environ.get('KADIMA_SHIELD_V9_LIVE', '0') == '1'


def _finite(*values):
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


# ------------------------------------------------------------------ futures contract
def pick_future(rows, name, index_exchange, today):
    """Near-month index future for `name`. Rolls to the next contract ON expiry
    day (volume moves there), so expiry must be strictly after today."""
    seg = FUTURES_EXCHANGE.get(index_exchange)
    found = []
    for r in rows or ():
        try:
            if r.get('exch_seg') != seg or r.get('instrumenttype') != 'FUTIDX' or r.get('name') != name:
                continue
            if r.get('permitted_to_trade') is False or not r.get('token'):
                continue
            expiry = datetime.strptime(r['expiry'], '%d%b%Y').date()
            if expiry > today:
                found.append((expiry, r))
        except (KeyError, TypeError, ValueError):
            continue
    if not found:
        return None
    expiry, row = min(found, key=lambda x: x[0])
    return dict(token=str(row['token']), symbol=row.get('symbol'), exchange=seg, expiry=expiry.isoformat())


class FuturesBars:
    """Closed 5-min futures bars with the session VWAP at each close."""

    def __init__(self, contract):
        self.contract = contract
        self.bars = deque(maxlen=12)

    def add(self, closed_at, close, volume, session_vwap):
        """A bar for the same 5 minutes replaces the earlier (still-open) reading."""
        bar = dict(closed_at=closed_at, close=close, volume=volume, vwap=session_vwap)
        if self.bars and self.bars[-1]['closed_at'] == closed_at:
            self.bars[-1] = bar
        else:
            self.bars.append(bar)

    def at(self, closed_at):
        """Inputs for the score at an index bar close, or None when the futures bar
        for the same 5 minutes is missing (that leg then sits out)."""
        match = [i for i, b in enumerate(self.bars) if b['closed_at'] == closed_at]
        if not match:
            return None
        i = match[-1]
        bar = self.bars[i]
        prev = [b['volume'] for b in list(self.bars)[max(0, i - 5):i]]
        return dict(close=bar['close'], vwap=bar['vwap'], vol=bar['volume'], prev_vols=prev,
                    symbol=self.contract.get('symbol'))


# ------------------------------------------------------------------ option chain OI
class OIHistory:
    """Option-chain OI captured at each 5-min index close: 10 minutes = two closes."""

    def __init__(self):
        self.snaps = deque(maxlen=4)
        self.day_open = {}
        self.day = None

    def add(self, closed_at, quotes):
        if self.day != closed_at.date():
            self.day, self.day_open = closed_at.date(), {}
        snap = {}
        for q in quotes:
            if q.opt_type in ('CE', 'PE') and _finite(q.strike, q.oi) and q.oi > 0:
                snap[str(q.token)] = (q.opt_type, float(q.strike), float(q.oi))
                self.day_open.setdefault(str(q.token), float(q.oi))
        self.snaps.append((closed_at, snap))

    def side(self, underlying, spot, now):
        """'CE', 'PE' or None, with a reason when the leg sits out."""
        if not _finite(spot) or spot <= 0 or not self.snaps:
            return None, 'no_chain'
        current = self.snaps[-1][1]
        then = next((s for t, s in reversed(self.snaps) if (now - t).total_seconds() >= 600), None)
        if then is None:
            return None, 'oi_warmup'
        thr = OI_PCT.get(underlying, OI_PCT_DEFAULT) / 100
        bull = bear = 0
        for token, (typ, strike, oi) in current.items():
            if abs(strike - spot) > OI_BAND * spot:
                continue
            opened = self.day_open.get(token)
            if typ == 'PE' and opened and now.time() >= PUT_OI_AMBIGUITY_AFTER and \
                    oi > opened * (1 + PUT_OI_AMBIGUITY_PCT / 100):
                return None, 'put_oi_ambiguous'
            past = then.get(token)
            if not past:
                continue
            change = (oi - past[2]) / past[2]
            if change >= thr:
                bull, bear = (bull + 1, bear) if typ == 'PE' else (bull, bear + 1)      # writing
            elif change <= -thr:
                bull, bear = (bull + 1, bear) if typ == 'CE' else (bull, bear + 1)      # covering / unwinding
        return ('CE' if bull > bear else 'PE' if bear > bull else None), None


# ------------------------------------------------------------------ score and tier
def score(snap, oi_side=None, futures=None):
    """(direction, score, legs). legs: leg -> 'CE' | 'PE' (legs that sit out are absent)."""
    legs = {}
    fut = futures or {}
    if _finite(fut.get('vwap'), fut.get('close')) and fut['vwap'] > 0:
        if fut['close'] > fut['vwap']:
            legs['vwap'] = 'CE'
        elif fut['close'] < fut['vwap']:
            legs['vwap'] = 'PE'
    if getattr(snap, 'ema_bull', None) is not None:
        legs['ema'] = 'CE' if snap.ema_bull else 'PE'
    rsi = getattr(snap, 'rsi', None)
    if _finite(rsi):
        if rsi > RSI_BULL:
            legs['rsi'] = 'CE'
        elif rsi < RSI_BEAR:
            legs['rsi'] = 'PE'
    prev = fut.get('prev_vols') or []
    if legs.get('vwap') and len(prev) >= 5 and _finite(fut.get('vol'), *prev) and fut['vol'] > max(prev):
        legs['volume'] = legs['vwap']
    if oi_side in ('CE', 'PE'):
        legs['oi'] = oi_side
    if getattr(snap, 'failed_breakdown', None):
        legs['failed_break'] = 'CE'
    elif getattr(snap, 'failed_breakout', None):
        legs['failed_break'] = 'PE'
    bull = sum(WEIGHTS[k] for k, v in legs.items() if v == 'CE')
    bear = sum(WEIGHTS[k] for k, v in legs.items() if v == 'PE')
    return ('CE' if bull >= bear else 'PE'), max(bull, bear), legs


def capped(raw, closed_at, pre_expiry):
    return raw * PRE_EXPIRY_FACTOR if pre_expiry and closed_at.time() >= PRE_EXPIRY_AFTER else raw


def tier(underlying, history, banks=None, macro_clear=True):
    """history: [{'closed_at', 'direction', 'score'}] newest last, scores already capped.
    banks: {bank: bool} aligned with the latest direction (Bank Nifty only)."""
    if not history:
        return 'NONE', 'no_score'
    last = history[-1]
    need = EXTRA_HOLD.get(underlying, EXTRA_HOLD_DEFAULT) + 1

    def held(level):
        rows = history[-need:]
        if len(rows) < need:
            return False
        return all(r['direction'] == last['direction'] and r['score'] >= level for r in rows) and \
            all((b['closed_at'] - a['closed_at']).total_seconds() == 300 for a, b in zip(rows, rows[1:]))

    watch = WATCH.get(underlying, WATCH_DEFAULT)
    level = ('CONFIRMED' if held(CONFIRMED) else 'ACTIVE' if held(ACTIVE)
             else 'WATCH' if last['score'] >= watch else 'NONE')
    reason = None
    if level in ('ACTIVE', 'CONFIRMED') and underlying == 'BANKNIFTY' and \
            sum(bool(v) for v in (banks or {}).values()) < BANK_MIN:
        level, reason = 'WATCH', 'bank_confirmation_missing'
    if level in ('ACTIVE', 'CONFIRMED') and not macro_clear:
        level, reason = 'WATCH', 'macro_suppression_or_unknown'
    return level, reason


def alert_line(underlying, level, direction, spot, swing):
    """Checklist alert format. Levels are index points: stop beyond the 6-bar swing,
    targets at 2R and 3R (minimum reward:risk 1:2)."""
    if level not in ('ACTIVE', 'CONFIRMED') or not _finite(spot, swing):
        return '⚪ NO A-GRADE SETUP | Wait'
    risk = spot - swing if direction == 'CE' else swing - spot
    if risk <= 0:
        return '⚪ NO A-GRADE SETUP | Wait'
    sign = 1 if direction == 'CE' else -1
    t1, t2 = spot + sign * 2 * risk, spot + sign * 3 * risk
    head = ('\U0001F7E2 BULL' if direction == 'CE' else '\U0001F534 BEAR') + f' SHIELD {level}'
    return f'{head} | Entry: {spot:.0f} | Target: {t1:.0f}-{t2:.0f} | SL: {swing:.0f}'


# ------------------------------------------------------------------ shadow-only candidate filters
def candidate_filters(direction, legs, closed_at, day_range, avg_day_range, atm_iv):
    """Logged only, never applied (review guardrail: 30 resolved occurrences first).
    A value of None means the input was not available."""
    non_trend = any(legs.get(k) == direction for k in ('volume', 'oi', 'failed_break'))
    trend_day = None
    if _finite(day_range, avg_day_range) and avg_day_range > 0:
        trend_day = day_range >= TREND_DAY_FRACTION * avg_day_range
    return dict(non_trend_leg=non_trend, trend_day=trend_day,
                midday=MIDDAY[0] <= closed_at.time() < MIDDAY[1],
                atm_iv=round(atm_iv, 4) if _finite(atm_iv) else None,
                risk_sizing='applied_at_entry_by_each_lane')


def day_ranges(bars_5m, today):
    """(today's range so far, mean range of up to 5 earlier sessions) from 5-min bars."""
    by_day = {}
    for b in bars_5m or ():
        try:
            day = datetime.fromisoformat(b['start_ts']).date()
            hi, lo = float(b['high']), float(b['low'])
        except (KeyError, TypeError, ValueError):
            continue
        h, l = by_day.get(day, (hi, lo))
        by_day[day] = (max(h, hi), min(l, lo))
    today_range = (by_day[today][0] - by_day[today][1]) if today in by_day else None
    earlier = [h - l for d, (h, l) in sorted(by_day.items()) if d < today][-5:]
    return today_range, (sum(earlier) / len(earlier) if earlier else None)


def swing(bars_5m, direction, count=6):
    try:
        recent = list(bars_5m)[-count:]
        return min(float(b['low']) for b in recent) if direction == 'CE' else max(float(b['high']) for b in recent)
    except (KeyError, TypeError, ValueError):
        return None
