"""Sniper mode for Gamma Blast auto entries (trader decision 2026-10-24 01:33 IST).

"Only few but highly profit making and less risky trades." From the 21-23 Oct audit:
every Gamma T2 came before 10:30, mostly with the day trend, never on the first signal
of its index/side; stop-outs were late, repeated after an earlier stop on the same side,
or counter-trend. With mode=live, an auto Gamma Blast entry is allowed only when:

  1. Gamma Blast only (scalping stays off; nothing here touches other kinds)
  1b. index is in the allow-list (default NIFTY, BANKNIFTY, SENSEX; FINNIFTY lost money
     under every filter in the 6-month test)                      -> sniper_index_excluded
  2. 09:45 <= now < 10:30 IST                                   -> sniper_outside_window
  3. index day trend is UP/DOWN and the side agrees; SIDEWAYS = no trade
                                                                -> sniper_sideways_day / sniper_counter_trend
  3b. A + B filter (25-Oct 6-month test on NIFTY/BANKNIFTY/SENSEX/FINNIFTY, from the
     engine's own closed 5-min index candles + live spot):
       A: spot within A_PCT (0.1%) of the day high (CE) / day low (PE)
                                                                -> sniper_not_near_day_extreme
       B: index moved >= B_PCT (0.3%) in the trade direction over the last 15 min
                                                                -> sniper_move_too_small
       missing/stale candles block (fail closed)                -> sniper_ab_no_data
  4. (switch require_second_signal) not the first signal: another contract on the same index+side was suggested
     at least CONFIRM_GAP_SEC earlier today                     -> sniper_first_signal
     and the premium is not > CHASE_PCT above this contract's first suggestion
                                                                -> sniper_chasing_premium
  5. no stop observed on the same index+side today              -> sniper_side_stopped
  6. at most MAX_TRADES entries a day; after a losing Sniper trade the next one must be
     a different index or side                                  -> sniper_daily_cap / sniper_after_loss_same_side
  7. 1 lot (MAX_LOTS); skip when 1-lot stop risk > MAX_RISK_CASH_PCT of available cash
                                                                -> sniper_risk_over_cash_limit
Stops: the existing band (forecast/ATR level) stop with the 35% hard cap is kept.
Exits: the existing trail is kept, plus a no-progress exit (premium not up
NO_PROGRESS_GAIN_PCT within NO_PROGRESS_MIN minutes) for Sniper trades.

Sniper replaces two existing gamma timing blocks for its own trades (each with a switch):
  exempt_r3        the 30-min "exhaustion" block after a same-side +50% (it removed most
                   Sniper winners in the replay; rule 5 protects the side instead)
  exempt_pullback  "waiting_for_pullback_to_band_half" / "chasing_beyond_band" (rule 4's
                   chase guard replaces them). spot_beyond_band_stop still blocks.
OI/direction flags on the Celestial row become notes, not blockers (trend + 2nd signal
confirm instead); tradeable contract and clean spread stay required.

Manual Celestial buys are never touched. Config: DATA_DIR/sniper.json, re-read every 30 s,
{"mode": "off|shadow|live", ...}. shadow logs every decision and never blocks or exits.
Any internal error fails CLOSED for entries in live mode (a broken Sniper never lets the
old unfiltered flow back in) and never forces an exit.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta

from . import audit, storage
from .config import DATA_DIR, IST
from .utils import now_ist

DEFAULTS = {
    'mode': 'off',
    'window_start': '09:45',
    'window_end': '10:30',
    'max_trades': 2,
    'max_lots': 1,
    'max_risk_cash_pct': 15.0,
    'chase_pct': 15.0,
    'confirm_gap_sec': 60,
    'no_progress_min': 20,
    'no_progress_gain_pct': 10.0,
    'exempt_r3': True,
    'exempt_pullback': True,
    'relax_flags': True,
    'indices': 'NIFTY,BANKNIFTY,SENSEX',
    'ab_filter': True,
    'a_pct': 0.1,
    'b_pct': 0.3,
    'require_second_signal': True,
}
RELAXED_FLAGS = ('oi_unconfirmed', 'direction_unconfirmed')
R3_PREFIX = 'gamma_move_exhausted_'
PULLBACK_BLOCKS = ('waiting_for_pullback_to_band_half', 'chasing_beyond_band')

_cfg = {'at': 0.0, 'value': None}
_lock = threading.Lock()
_last_log: dict = {}
_np_logged: set = set()
_ab_last: dict = {}


# ------------------------------------------------------------------ config
def config():
    now = time.monotonic()
    if _cfg['value'] is None or now - _cfg['at'] > 30:
        value = dict(DEFAULTS)
        try:
            with open(os.path.join(DATA_DIR, 'sniper.json')) as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k not in value:
                    continue
                d = DEFAULTS[k]
                if isinstance(d, bool):
                    ok = isinstance(v, bool)
                elif isinstance(d, (int, float)):
                    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
                    v = float(v) if isinstance(d, float) else int(v)
                else:
                    ok = isinstance(v, str)
                if ok:
                    value[k] = v
        except (OSError, ValueError, AttributeError):
            pass
        value['mode'] = os.environ.get('KADIMA_SNIPER', value['mode'])
        if value['mode'] not in ('off', 'shadow', 'live'):
            value['mode'] = 'off'
        _cfg.update(at=now, value=value)
    return _cfg['value']


def mode():
    return config()['mode']


def live():
    return mode() == 'live'


def active():
    return mode() in ('shadow', 'live')


def _hm(text):
    h, m = text.split(':')
    return int(h), int(m)


def _dt(value):
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=IST)


# ------------------------------------------------------------------ state
def _conn():
    c = storage.conn()
    c.execute('''CREATE TABLE IF NOT EXISTS sniper_entries (
        signal_id INTEGER PRIMARY KEY, day TEXT NOT NULL, mode TEXT NOT NULL,
        index_name TEXT NOT NULL, direction TEXT NOT NULL, token TEXT,
        entered_at TEXT NOT NULL)''')
    return c


def _entries(day, entry_mode):
    """Today's Sniper entries with their position outcome (live: positions table)."""
    c = _conn()
    rows = c.execute(
        'SELECT s.signal_id, s.index_name, s.direction, s.entered_at, p.status, '
        'p.entry_price, p.exit_price FROM sniper_entries s '
        'LEFT JOIN positions p ON p.signal_id = s.signal_id '
        'WHERE s.day=? AND s.mode=? ORDER BY s.entered_at', (day, entry_mode)).fetchall()
    out = []
    for r in rows:
        status, entry, exit_ = r[4], r[5], r[6]
        if entry_mode == 'live' and status == 'closed' and not entry:
            continue        # entry order never filled: not a trade
        if entry_mode == 'live':
            is_open = status != 'closed'
            lost = bool(status == 'closed' and entry and exit_ is not None and exit_ < entry)
        else:               # shadow: no position exists, only the would-enter count matters
            is_open = lost = False
        out.append(dict(signal_id=r[0], index=r[1], direction=r[2], at=r[3],
                        open=is_open, lost=lost))
    return out


def is_sniper(signal_id):
    try:
        return _conn().execute('SELECT 1 FROM sniper_entries WHERE signal_id=? AND mode=?',
                               (signal_id, 'live')).fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def note_entry(signal_id, index_name, direction, token, now=None, entry_mode='live'):
    now = now or now_ist()
    c = _conn()
    with c:
        cur = c.execute('INSERT OR IGNORE INTO sniper_entries VALUES (?,?,?,?,?,?,?)',
                        (signal_id, now.date().isoformat(), entry_mode, index_name, direction,
                         str(token) if token is not None else None, now.isoformat()))
    if cur.rowcount != 1:
        return
    audit.log('sniper_entry', signal_id=signal_id, index=index_name, direction=direction,
              token=token, mode=entry_mode)


# ------------------------------------------------------------------ rules
def _plans(day, index_name, direction):
    c = storage.conn()
    try:
        return c.execute(
            'SELECT token, suggested_at, plan, sl_at FROM trade_audit_plans '
            'WHERE day=? AND underlying=? AND direction=?',
            (day, index_name, direction)).fetchall()
    except Exception:  # noqa: BLE001 - no audit table yet
        return []


def _trend(index_name, token, spot, vwap, now):
    from . import trend_rules
    label = trend_rules.day_trend(index_name, token, spot, vwap, now)
    label, _source = trend_rules._trend_or_proxy(index_name, token, label, now)
    return label


def _bars_today(index_token, now):
    """Today's closed 5-minute index candles (start, o, h, l, c) before now."""
    from . import hourly_forecast
    rows = hourly_forecast.history_rows(str(index_token), now, days=1)
    return [r for r in rows if r[0].date() == now.date()]


def ab_check(index_name, index_token, direction, spot, now):
    """Rule 3b. Returns a block reason or None; the measured values go to _ab_last."""
    cfg = config()
    if not cfg['ab_filter']:
        return None
    info = {'a_pct': cfg['a_pct'], 'b_pct': cfg['b_pct']}
    _ab_last[(index_name, direction)] = info
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        spot = 0.0
    if index_token is None or spot <= 0:
        return 'sniper_ab_no_data'
    bars = _bars_today(index_token, now)
    # a latest closed candle more than 10 minutes old means the feed is stale
    if len(bars) < 3 or bars[-1][0] + timedelta(minutes=15) < now:
        return 'sniper_ab_no_data'
    hi = max([b[2] for b in bars] + [spot])
    lo = min([b[3] for b in bars] + [spot])
    away = (hi - spot) / spot * 100 if direction == 'CE' else (spot - lo) / spot * 100
    info['away_pct'] = round(away, 4)
    if round(away, 4) > cfg['a_pct']:
        return 'sniper_not_near_day_extreme'
    cut = now - timedelta(minutes=15)
    ref = [b for b in bars if b[0] + timedelta(minutes=5) <= cut]
    if not ref:
        return 'sniper_ab_no_data'
    move = (spot / float(ref[-1][4]) - 1) * 100 * (1 if direction == 'CE' else -1)
    info['move15_pct'] = round(move, 4)
    if round(move, 4) < cfg['b_pct']:
        return 'sniper_move_too_small'
    return None


def check(index_name, direction, token, option_ltp, trend, now, entry_mode='live',
          index_token=None, spot=None):
    """Rules 1b-6. Returns a block reason or None."""
    cfg = config()
    if direction not in ('CE', 'PE'):
        return 'sniper_direction_invalid'
    allowed = [x.strip().upper() for x in cfg['indices'].split(',') if x.strip()]
    if allowed and str(index_name).upper() not in allowed:
        return 'sniper_index_excluded'
    if not _hm(cfg['window_start']) <= (now.hour, now.minute) < _hm(cfg['window_end']):
        return 'sniper_outside_window'
    if trend not in ('UP', 'DOWN'):
        return 'sniper_sideways_day'
    if (trend == 'DOWN') != (direction == 'PE'):
        return 'sniper_counter_trend'
    ab = ab_check(index_name, index_token, direction, spot, now)
    if ab:
        return ab
    day = now.date().isoformat()
    plans = _plans(day, index_name, direction)
    gap = timedelta(seconds=cfg['confirm_gap_sec'])
    earlier = [p for p in plans if str(p[0]) != str(token)
               and (_dt(p[1]) is not None and _dt(p[1]) <= now - gap)]
    if not earlier and cfg['require_second_signal']:
        return 'sniper_first_signal'
    first = None
    for p in plans:
        if str(p[0]) != str(token):
            continue
        try:
            plan = json.loads(p[2]) if p[2] else {}
        except ValueError:
            plan = {}
        price = plan.get('entry') or plan.get('entry_reference')
        at = _dt(p[1])
        if price and at is not None and at <= now and (first is None or at < first[0]):
            first = (at, float(price))
    if first and option_ltp and option_ltp > round(first[1] * (1 + cfg['chase_pct'] / 100), 2):
        return 'sniper_chasing_premium'
    if any(_dt(p[3]) is not None and _dt(p[3]) <= now for p in plans):
        return 'sniper_side_stopped'
    taken = _entries(day, entry_mode)
    if any(e['lost'] and e['index'] == index_name and e['direction'] == direction for e in taken):
        return 'sniper_side_stopped'
    if any(e['open'] and e['index'] == index_name and e['direction'] == direction for e in taken):
        return 'sniper_side_already_open'
    if len(taken) >= cfg['max_trades']:
        return 'sniper_daily_cap'
    return None


def _log(key, **fields):
    """One audit line per contract per decision change (gates are polled every few s)."""
    sig = (fields.get('block'), fields.get('override'))
    with _lock:
        if _last_log.get(key) == sig:
            return
        _last_log[key] = sig
        if len(_last_log) > 2000:
            _last_log.clear()
    audit.log('sniper_gate', **fields)


def _gate_impl(index_name, token, direction, spot, vwap, now, gate, selected, manual):
    if manual or not active():
        return {}
    cfg = config()
    m = mode()
    if selected is None:
        res = {'block': 'sniper_needs_celestial_candidate'}
        _log((index_name, direction, None, now.date()), index=index_name, direction=direction,
             mode=m, **res)
        return res if m == 'live' else {}
    ctoken = selected.get('token')
    trend = _trend(index_name, token, spot, vwap, now)
    _ab_last.pop((index_name, direction), None)
    block = check(index_name, direction, ctoken, selected.get('option_ltp'), trend, now,
                  'live' if m == 'live' else 'shadow', index_token=token, spot=spot)
    ab_info = _ab_last.get((index_name, direction)) or {}
    old = (gate or {}).get('block')
    override = None
    stop_level = (gate or {}).get('stop_level')
    if block is None and old:
        r3 = old.startswith(R3_PREFIX) and cfg['exempt_r3']
        pull = old in PULLBACK_BLOCKS and cfg['exempt_pullback']
        if r3 or pull:
            from . import trend_rules
            b = trend_rules.band(index_name, token, spot, vwap, now)
            level = trend_rules.stop_level(direction, b) if b is not None else None
            if level is not None and ((direction == 'CE' and spot <= level)
                                      or (direction == 'PE' and spot >= level)):
                block = 'spot_beyond_band_stop'
            else:
                override = old
                stop_level = level if trend_rules.live() else None
    _log((index_name, direction, ctoken, now.date()), index=index_name, direction=direction,
         token=ctoken, symbol=selected.get('symbol'), trend=trend, block=block,
         override=override, existing_block=old, mode=m, ltp=selected.get('option_ltp'),
         spot=spot, away_pct=ab_info.get('away_pct'), move15_pct=ab_info.get('move15_pct'))
    if m != 'live':
        if block is None:
            digest = hashlib.sha1(f'{ctoken}|{now.date().isoformat()}'.encode()).hexdigest()
            note_entry(-int(digest[:12], 16), index_name, direction, ctoken, now, 'shadow')
        return {}
    if block:
        return {'block': block}
    return {'override': override, 'stop_level': stop_level} if override else {'pass': True}


def gate(index_name, token, direction, spot, vwap, now, gate=None, selected=None, manual=False):
    """Entry gate for auto Gamma Blast. Live errors fail closed."""
    try:
        return _gate_impl(index_name, token, direction, spot, vwap, now, gate, selected, manual)
    except Exception as exc:  # noqa: BLE001
        try:
            audit.log('sniper_error', where='gate', error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return {'block': 'sniper_error_fail_closed'} if (not manual and live()) else {}


def size(kind, manual, limit_price, stop, lot_size, lots, cash):
    """Rule 7. Returns (lots, block_reason)."""
    if kind != 'gamma_blast' or manual or not active():
        return lots, None
    try:
        cfg = config()
        new_lots = min(lots, cfg['max_lots'])
        risk = (float(limit_price) - float(stop)) * int(lot_size) * new_lots
        cash_v = float(cash) if cash is not None else None
        block = None
        if cash_v is None or cash_v <= 0:
            block = 'sniper_cash_unreadable'
        elif risk > cash_v * cfg['max_risk_cash_pct'] / 100:
            block = 'sniper_risk_over_cash_limit'
        audit.log('sniper_size', lots_in=lots, lots=new_lots, risk=round(risk, 2),
                  cash=cash_v, limit_pct=cfg['max_risk_cash_pct'], block=block, mode=mode())
        if not live():
            return lots, None
        return new_lots, block
    except Exception as exc:  # noqa: BLE001
        audit.log('sniper_error', where='size', error=str(exc)[:200])
        return (lots, None) if not live() else (0, 'sniper_error_fail_closed')


def after_submit(kind, manual, pos, now=None):
    if kind != 'gamma_blast' or manual or not live() or getattr(pos, 'status', None) == 'closed':
        return
    try:
        note_entry(pos.signal_id, pos.index, pos.direction, pos.token, now)
    except Exception as exc:  # noqa: BLE001
        audit.log('sniper_error', where='after_submit', error=str(exc)[:200])


def no_progress(pos, now=None):
    """'sniper_no_progress' when a live Sniper trade is not up enough in time."""
    try:
        if not active() or getattr(pos, 'kind', None) != 'gamma_blast':
            return None
        if getattr(pos, 'status', None) != 'open' or not getattr(pos, 'entry_price', 0):
            return None
        if not is_sniper(pos.signal_id):
            return None
        cfg = config()
        now = now or now_ist()
        start = pos.entry_ts if isinstance(pos.entry_ts, datetime) else _dt(str(pos.entry_ts))
        if start is None or now - start < timedelta(minutes=cfg['no_progress_min']):
            return None
        peak = pos.peak_price or pos.entry_price
        if peak >= round(pos.entry_price * (1 + cfg['no_progress_gain_pct'] / 100), 2):
            return None
        if pos.signal_id not in _np_logged:
            _np_logged.add(pos.signal_id)
            audit.log('sniper_no_progress', signal_id=pos.signal_id, entry=pos.entry_price,
                      peak=peak, minutes=round((now - start).total_seconds() / 60, 1), mode=mode())
        return 'sniper_no_progress' if live() else None
    except Exception as exc:  # noqa: BLE001
        try:
            audit.log('sniper_error', where='no_progress', error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return None


def relax_reasons(reasons, notes):
    """Celestial row: OI/direction flags become notes while Sniper is live."""
    try:
        if not live() or not config()['relax_flags']:
            return reasons
        moved = [r for r in reasons if r in RELAXED_FLAGS]
        notes.extend(moved)
        return [r for r in reasons if r not in RELAXED_FLAGS]
    except Exception:  # noqa: BLE001
        return reasons
