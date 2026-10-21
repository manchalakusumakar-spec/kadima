"""Trader rules of 2026-10-19 for gamma trades. No broker/order calls.

1. Find the day trend first (UP / DOWN / SIDEWAYS per index).
2. Trade a gamma blast only alongside the trend (SIDEWAYS: both sides).
3. Stop-loss follows the hourly forecast band; when the band moves, the stop moves.
4. Enter at the minimum level of risk (near the band edge that holds the stop).

Thresholds are formula-driven from live data (expected daily range, 5-minute
ATR, the option premium's own noise). The only fixed number is the 35% hard
cap on premium loss, kept as a last-resort net against bad data.

Mode is read from DATA_DIR/trend_rules.json ({"mode": "off|shadow|live",
"entry": "half|band"}) or env KADIMA_TREND_RULES / KADIMA_TREND_ENTRY, and
re-read every 30 s so it can be flipped without a restart. "shadow" computes
and audits every decision but never blocks an entry or moves a stop.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from statistics import mean

from . import audit, storage
from .config import DATA_DIR, IST
from .utils import now_ist

HARD_CAP = 0.35            # max premium loss, fraction of entry (only fixed number)
NOISE_MULT = 2.0           # stop never inside 2x the premium's own 1-min noise
ATR_BUFFER = 0.25          # band-edge buffer = 0.25 x 5-min ATR (Sep stops.py decision)
ATR_BAND = 1.5             # fallback band (no forecast) = VWAP +/- 1.5 x 5-min ATR
TREND_SHARE = 0.25         # trend threshold = 25% of expected daily range
FLIP_CANDLES = 2           # completed 15-min closes needed to flip a trend
BAND_FRESH_SEC = 600       # forecast point must be for the next 10 minutes
STATE_FRESH_SEC = 90       # band stop older than this is ignored (fallback)
GAMMA_KINDS = ('gamma_blast',)
# Every feature is shadow until switched; the rejection watchdog only alerts, so it
# starts live. broker_stop is shadow-only in this build (live is refused in code).
FEATURE_DEFAULTS = {'mode': 'shadow', 'entry': 'half', 'cf_protect': 'shadow',
                    'reentry': 'shadow', 'monitor': 'shadow', 'watchdog': 'live',
                    'broker_stop': 'shadow', 'rule2': 'shadow'}
# rule2 (2026-10-21, trader): "gamma only with the day trend" can go live on its own,
# while the band stop and min-risk entry (rules 3-4) stay in shadow. mode=live still
# switches all four rules on together.
PROXY_INDEX = 'NIFTY'      # trend source for an index without enough candle history

_cfg = {'at': 0.0, 'value': None}
_lock = threading.Lock()
_inputs_cache: dict = {}
_trend: dict = {}


# ------------------------------------------------------------------ config
def _config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(FEATURE_DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'trend_rules.json')) as f:
                value.update({k: v for k, v in json.load(f).items() if k in value})
        except (OSError, ValueError, AttributeError):
            pass
        value['mode'] = os.environ.get('KADIMA_TREND_RULES', value['mode'])
        value['entry'] = os.environ.get('KADIMA_TREND_ENTRY', value['entry'])
        for key, default in FEATURE_DEFAULTS.items():
            if key != 'entry' and value[key] not in ('off', 'shadow', 'live'):
                value[key] = default
        if value['entry'] not in ('half', 'band'):
            value['entry'] = 'half'
        _cfg.update(at=now, value=value)
    return _cfg['value']


def mode():
    return _config()['mode']


def feature(name):
    """off | shadow | live for a protection feature (see FEATURE_DEFAULTS)."""
    return _config().get(name, 'shadow')


def live():
    return mode() == 'live'


def _finite(*values):
    try:
        return all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ inputs
def _rows(token, now):
    """Closed 5-minute candles (start, o, h, l, c) for ~30 days before now."""
    from . import hourly_forecast
    return hourly_forecast.history_rows(token, now, days=30)


def inputs(token, now):
    """Previous close, day open, expected daily range %, 15-min bars, ATR.
    Cached per token until a new 5-minute candle closes."""
    key = (token, now.replace(second=0, microsecond=0, minute=now.minute - now.minute % 5))
    with _lock:
        hit = _inputs_cache.get(token)
        if hit and hit[0] == key:
            return hit[1]
    try:
        rows = _rows(token, now)
    except Exception as exc:  # noqa: BLE001 - observational input, never fatal
        audit.log('trend_rules_input_error', token=token, error=str(exc)[:200])
        rows = []
    today = now.date()
    prior = [r for r in rows if r[0].date() < today]
    day = [r for r in rows if r[0].date() == today]
    by_day: dict = {}
    for r in prior:
        d = by_day.setdefault(r[0].date(), [r[2], r[3], r[4]])
        d[0], d[1], d[2] = max(d[0], r[2]), min(d[1], r[3]), r[4]
    ranges = [(h - l) / c * 100 for h, l, c in (by_day[k] for k in sorted(by_day)[-14:]) if _finite(c)]
    expected = mean(ranges) if len(ranges) >= 5 else None
    trs, prev = [], None
    for r in (prior[-20:] + day)[-15:]:
        trs.append(max(r[2] - r[3], abs(r[2] - prev) if prev else 0, abs(r[3] - prev) if prev else 0))
        prev = r[4]
    bars15: dict = {}
    for r in day:
        start = r[0].replace(hour=9, minute=15, second=0, microsecond=0)
        k = int((r[0] - start).total_seconds() // 900)
        b = bars15.setdefault(k, dict(o=r[1], h=r[2], l=r[3], c=r[4], n=0, end=start + timedelta(minutes=15 * (k + 1))))
        b.update(h=max(b['h'], r[2]), l=min(b['l'], r[3]), c=r[4], n=b['n'] + 1)
    done = [bars15[k] for k in sorted(bars15) if bars15[k]['end'] <= now]
    out = dict(prev_close=prior[-1][4] if prior else None,
               day_open=day[0][1] if day else None,
               expected_pct=expected,
               atr=mean(trs[1:]) if len(trs) >= 4 else None,
               opening=(done[0]['h'], done[0]['l']) if done else None,
               bars15=done)
    with _lock:
        _inputs_cache[token] = (key, out)
    return out


def vwap_for(name, now=None):
    """Latest fresh index VWAP the radar published (futures-translated when live)."""
    try:
        from . import celestial
        h = celestial.health('structure:' + name)
        if h and celestial.fresh(h.get('ts'), now) and _finite(h.get('vwap')):
            return float(h['vwap'])
    except Exception:  # noqa: BLE001
        pass
    return None


# ------------------------------------------------------------------ rule 1
def threshold_pct(data):
    exp = data.get('expected_pct')
    return TREND_SHARE * exp if _finite(exp) else 0.3   # 0.3% only until history exists


def raw_trend(spot, vwap, data):
    thr = threshold_pct(data)
    up = down = 0
    if _finite(data.get('prev_close')):
        move = (spot / data['prev_close'] - 1) * 100
        down += move <= -thr
        up += move >= thr
    if _finite(data.get('day_open')):
        move = (spot / data['day_open'] - 1) * 100
        down += move <= -thr / 2
        up += move >= thr / 2
    if _finite(vwap):
        down += spot < vwap
        up += spot > vwap
    if data.get('opening') and len(data.get('bars15') or []) >= 2:
        hi, lo = data['opening']
        last = data['bars15'][-1]['c']
        down += last < lo
        up += last > hi
    return 'DOWN' if down >= 3 else 'UP' if up >= 3 else 'SIDEWAYS'


def day_trend(name, token, spot, vwap, now=None):
    """Evidence-driven label with hysteresis: UP/DOWN only flip after
    FLIP_CANDLES completed 15-min closes beyond both VWAP and the day open."""
    now = now or now_ist()
    if not _finite(spot):
        return 'SIDEWAYS'
    data = inputs(token, now)
    raw = raw_trend(spot, vwap, data)
    with _lock:
        st = _trend.get(name)
        if st is None or st['day'] != now.date().isoformat():
            st = _trend[name] = dict(day=now.date().isoformat(), label='SIDEWAYS', since=None,
                                     seen=0, against=0)
        bars = data.get('bars15') or []
        if len(bars) > st['seen']:
            for b in bars[st['seen']:]:
                opp = ((st['label'] == 'UP' and _finite(vwap, data.get('day_open'))
                        and b['c'] < vwap and b['c'] < data['day_open']) or
                       (st['label'] == 'DOWN' and _finite(vwap, data.get('day_open'))
                        and b['c'] > vwap and b['c'] > data['day_open']))
                st['against'] = st['against'] + 1 if opp else 0
            st['seen'] = len(bars)
        old = st['label']
        if old == 'SIDEWAYS' or (raw != old and st['against'] >= FLIP_CANDLES):
            st['label'] = raw
        if st['label'] != old:
            st.update(since=now.isoformat(), against=0)
        label = st['label']
    if label != old:
        try:
            from . import celestial
            celestial.health('trend:' + name, dict(label=label, since=now.isoformat(),
                             frm=old, threshold_pct=round(threshold_pct(data), 3), mode=mode()))
        except Exception:  # noqa: BLE001 - display only
            pass
        audit.log('day_trend_changed', index=name, frm=old, to=label, spot=spot, vwap=vwap,
                  threshold_pct=round(threshold_pct(data), 3), mode=mode())
    return label


def _trend_or_proxy(index_name, token, label, now):
    """An index whose own history cannot vote (no previous close or no day open, e.g. a
    GAMMA_EXTRA index the engine only recently started recording) takes the NIFTY day
    trend, so rule 2 never fails open on missing data. Returns (label, source)."""
    data = inputs(token, now)
    if index_name == PROXY_INDEX or (_finite(data.get('prev_close')) and _finite(data.get('day_open'))):
        return label, 'own'
    try:
        from .config import GAMMA_INDICES
        proxy = GAMMA_INDICES[PROXY_INDEX]
        with _lock:
            st = _trend.get(PROXY_INDEX)
        if st and st.get('day') == now.date().isoformat():
            return st['label'], 'proxy_' + PROXY_INDEX
        pdata = inputs(proxy.token, now)
        bars = pdata.get('bars15') or []
        if bars:
            return raw_trend(bars[-1]['c'], vwap_for(PROXY_INDEX, now), pdata), 'proxy_' + PROXY_INDEX
    except Exception:  # noqa: BLE001 - fall back to the index's own label
        pass
    return label, 'own_incomplete'


def against_trend(label, direction):
    return (label == 'DOWN' and direction == 'CE') or (label == 'UP' and direction == 'PE')


# ------------------------------------------------------------------ rule 3
def forecast_point(name, now):
    from . import hourly_forecast
    try:
        r = hourly_forecast.conn().execute(
            "SELECT p.ts, p.forecast, p.upper, p.lower FROM hourly_forecast_points p "
            "JOIN hourly_forecasts f ON f.day=p.day AND f.index_name=p.index_name AND f.slot=p.slot "
            "WHERE p.day=? AND p.index_name=? AND f.status='live' AND p.ts>=? ORDER BY p.ts LIMIT 1",
            (now.date().isoformat(), name, now.isoformat())).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if r is None:
        return None
    ts = datetime.fromisoformat(r['ts'])
    ts = ts if ts.tzinfo else ts.replace(tzinfo=IST)
    if not 0 <= (ts - now).total_seconds() <= BAND_FRESH_SEC or not _finite(r['upper'], r['lower']):
        return None
    return dict(upper=float(r['upper']), lower=float(r['lower']), source='forecast')


def band(name, token, spot, vwap, now):
    data = inputs(token, now)
    atr = data.get('atr')
    b = forecast_point(name, now)
    if b is None and _finite(atr):
        centre = vwap if _finite(vwap) else spot
        b = dict(upper=centre + ATR_BAND * atr, lower=centre - ATR_BAND * atr, source='atr')
    if b is None or not b['upper'] > b['lower']:
        return None
    b['mid'] = (b['upper'] + b['lower']) / 2
    b['buffer'] = ATR_BUFFER * atr if _finite(atr) else 0.0
    return b


def stop_level(direction, b):
    return b['lower'] - b['buffer'] if direction == 'CE' else b['upper'] + b['buffer']


def premium_noise_pct(samples, now):
    """Mean absolute 1-minute premium change over the last 10 minutes, in %."""
    closes: dict = {}
    for ts, ltp in samples:
        if 0 <= (now - ts).total_seconds() <= 600 and _finite(ltp):
            closes[ts.replace(second=0, microsecond=0)] = ltp
    seq = [closes[k] for k in sorted(closes)]
    moves = [abs(b / a - 1) * 100 for a, b in zip(seq, seq[1:])]
    return mean(moves) if len(moves) >= 3 else None


def tracker_samples(tracker, token):
    try:
        with tracker._lock:
            h = tracker._hist.get(token)
            return list(h.samples) if h else []
    except AttributeError:
        return []


def bound_stop(stop, entry, ltp, noise, previous=None):
    """Hard cap below; never raised into the premium's own noise."""
    if not _finite(stop, entry):
        return None
    if _finite(ltp, noise) and (previous is None or stop > previous):
        ceiling = ltp * (1 - NOISE_MULT * noise / 100)
        stop = min(stop, max(ceiling, previous or 0))
    stop = max(stop, entry * (1 - HARD_CAP))   # hard cap always wins
    return stop if stop > 0 else None


# ------------------------------------------------------------------ rule 4 + entry
def _entry_gate_impl(index_name, token, direction, spot, vwap, now=None):
    """Called for gamma entries. Returns {'block': reason|None, 'stop_level': x|None}.
    In shadow mode it logs what it would have done and never blocks."""
    now = now or now_ist()
    if mode() == 'off':
        return {}
    label = day_trend(index_name, token, spot, vwap, now)
    label, source = _trend_or_proxy(index_name, token, label, now)
    b = band(index_name, token, spot, vwap, now)
    block = None
    if against_trend(label, direction):
        block = f'counter_trend_{label.lower()}_day'
    elif b is not None:
        level = stop_level(direction, b)
        if (direction == 'CE' and spot <= level) or (direction == 'PE' and spot >= level):
            block = 'spot_beyond_band_stop'
        elif (direction == 'CE' and spot > b['upper']) or (direction == 'PE' and spot < b['lower']):
            block = 'chasing_beyond_band'
        elif _config()['entry'] == 'half' and (
                (direction == 'CE' and spot > b['mid']) or (direction == 'PE' and spot < b['mid'])):
            block = 'waiting_for_pullback_to_band_half'
    audit.log('trend_rules_entry', index=index_name, direction=direction, trend=label,
              band=None if b is None else {k: round(v, 2) if isinstance(v, float) else v
                                           for k, v in b.items()},
              spot=spot, would_block=block, mode=mode(), trend_source=source,
              rule2=feature('rule2'))
    if not live():
        if feature('rule2') == 'live' and against_trend(label, direction):
            return {'trend': label, 'block': block if block and block.startswith('counter_trend')
                    else f'counter_trend_{label.lower()}_day'}
        return {'trend': label}
    return {'trend': label, 'block': block,
            'stop_level': stop_level(direction, b) if b is not None and block is None else None}


def _entry_stop_impl(stop, ltp, samples, now=None):
    """Bound a candidate's initial premium stop (hard cap + noise floor)."""
    if not live() or not _finite(stop, ltp):
        return stop
    return bound_stop(stop, ltp, ltp, premium_noise_pct(samples, now or now_ist()))


# ------------------------------------------------------------------ open positions
def _state_conn():
    c = storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS trend_band_state '
              '(signal_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    return c


def load_state(signal_id):
    row = _state_conn().execute('SELECT payload FROM trend_band_state WHERE signal_id=?',
                                (str(signal_id),)).fetchone()
    return json.loads(row[0]) if row else {}


def save_state(signal_id, state):
    c = _state_conn()
    with c:
        c.execute('INSERT OR REPLACE INTO trend_band_state VALUES (?,?)',
                  (str(signal_id), json.dumps(state, allow_nan=False, default=str)))


def _note_lock_impl(signal_id, price):
    st = load_state(signal_id)
    if _finite(price) and price > (st.get('lock') or 0):
        st['lock'] = price
        save_state(signal_id, st)


def days_to_expiry(chain, now):
    from . import config
    expiry = datetime.strptime(chain.scrips.nearest_expiry(chain.index), '%d%b%Y')
    expiry = expiry.replace(tzinfo=now.tzinfo, hour=config.MARKET_CLOSE[0], minute=config.MARKET_CLOSE[1])
    return (expiry - now).total_seconds() / 86400


def update_band_stop(pos, chain, spot, tracker, cost_reserve=0.01, now=None):
    """Re-derive this position's stop from the current band (both directions)."""
    from . import trade_policy
    now = now or now_ist()
    quote = chain.quotes.get(pos.token)
    if quote is None or not _finite(spot, quote.ltp, pos.entry_price):
        return
    name, token = pos.index, chain.index.token
    vwap = vwap_for(name, now)
    label = day_trend(name, token, spot, vwap, now)
    b = band(name, token, spot, vwap, now)
    st = load_state(pos.signal_id)
    old = st.get('stop')
    if b is None:
        return
    level = stop_level(pos.direction, b)
    breached = (pos.direction == 'CE' and spot <= level) or (pos.direction == 'PE' and spot >= level)
    stop = None
    if not breached:
        try:
            days = days_to_expiry(chain, now)
        except (AttributeError, TypeError, ValueError):
            return
        raw = trade_policy.premium_stop(spot, level, quote.strike, quote.ltp, days, pos.direction)
        noise = premium_noise_pct(tracker_samples(tracker, pos.token), now)
        stop = bound_stop(raw, pos.entry_price, quote.ltp, noise, old)
        st['noise'] = noise
    lock = st.get('lock') or 0
    if against_trend(label, pos.direction):
        be = pos.entry_price * (1 + cost_reserve)
        if be < quote.ltp:
            lock = max(lock, be)
    st.update(stop=stop, breached=breached, lock=lock, ts=now.isoformat(), trend=label,
              band=dict(lower=b['lower'], upper=b['upper'], source=b['source']))
    save_state(pos.signal_id, st)
    if breached != bool(st.get('logged_breach')) or (old and stop and abs(stop - old) >= 0.05):
        st['logged_breach'] = breached
        save_state(pos.signal_id, st)
        audit.log('band_stop_update', signal_id=pos.signal_id, index=name, direction=pos.direction,
                  stop=None if stop is None else round(stop, 2), previous=old, breached=breached,
                  lock=round(lock, 2), trend=label, band_source=b['source'], mode=mode())


def _managed_stop_impl(pos, ltp, tracker, retain=None, now=None):
    """(structural, effective, trail_active, reason) for a band-managed position, or
    None to keep the existing logic. Live mode only."""
    from . import celestial, trade_policy
    if not live() or getattr(pos, 'kind', None) not in GAMMA_KINDS:
        return None
    now = now or now_ist()
    st = load_state(pos.signal_id)
    try:
        ts = datetime.fromisoformat(st['ts'])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= (now - ts).total_seconds() <= STATE_FRESH_SEC:
        return None
    if st.get('breached'):
        return None, None, None, 'forecast_band_breached'
    base = st.get('stop')
    if not _finite(base, pos.entry_price, pos.peak_price):
        return None
    noise = st.get('noise')
    activation = max(trade_policy.ACTIVATION, NOISE_MULT * noise / 100) if _finite(noise) else None
    if celestial.enabled():
        eff, active = celestial.trail_stop(pos.entry_price, pos.peak_price, None, base,
                                           retain if retain is not None else .5,
                                           activation=activation)
    else:
        eff, active = trade_policy.ratchet(pos.entry_price, pos.peak_price, None, base,
                                           activation=activation)
    eff = max(eff, st.get('lock') or 0)
    return base, eff, active, None


# ------------------------------------------------------------------ safe wrappers
# A failure inside these rules must never stop KADIMA's existing entry or exit
# logic: on any error they fall back to "no change" and leave an audit line.
def _safe(name, fallback, fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as exc:  # noqa: BLE001
        try:
            audit.log('trend_rules_error', where=name, error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return fallback(*a, **k) if callable(fallback) else fallback


def entry_gate(*a, **k):
    return _safe('entry_gate', {}, _entry_gate_impl, *a, **k)


def entry_stop(stop, *a, **k):
    return _safe('entry_stop', lambda stop, *a, **k: stop, _entry_stop_impl, stop, *a, **k)


def note_lock(*a, **k):
    return _safe('note_lock', None, _note_lock_impl, *a, **k)


def managed_stop(*a, **k):
    return _safe('managed_stop', None, _managed_stop_impl, *a, **k)
