"""Fresh market evidence and persisted one-minute premium EMA exit checks."""
import json
from datetime import datetime

from . import celestial
from .utils import now_ist

# 2026-10-19 early-exit fix: EMA/VWAP breaks need this many consecutive
# completed one-minute candles; while a trade is in profit these signals
# only lock breakeven (see auto_executor) instead of exiting outright.
CONFIRM_CANDLES = 2
SOFT_REASONS = frozenset({
    'premium_below_completed_1m_ema9', 'underlying_lost_vwap',
    'opposite_gamma_confirmed', 'opposite_shield_active',
    'directional_oi_reversed', 'fresh_catalyst_risk_block'})


def connection():
    c = celestial.connection()
    c.execute('CREATE TABLE IF NOT EXISTS celestial_exit_state '
              '(signal_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    return c


def publish_quote(exchange, token, row):
    depth = row.get('depth') or {}
    bids, asks = depth.get('buy') or [], depth.get('sell') or []
    bid = float(bids[0]['price']) if bids else None
    ask = float(asks[0]['price']) if asks else None
    if not celestial.finite(bid, .000001) or not celestial.finite(ask, .000001) or ask < bid:
        bid = ask = None
    celestial.health('quote:' + exchange + ':' + token, dict(bid=bid, ask=ask))


def publish_structure(chain, spot, spot_at, result, *, vwap=None, shield_score=None, shield_direction=None):
    components = getattr(result, 'components', None)
    velocity = getattr(components, 'oi_velocity', None)
    celestial.health('structure:' + chain.index.name, dict(
        spot=spot, spot_at=spot_at, vwap=vwap,
        direction=getattr(result, 'direction', None), score=getattr(result, 'score', 0),
        oi_direction=getattr(result, 'direction', None) if velocity else None,
        shield_score=shield_score, shield_direction=shield_direction))


def evaluate(pos, ltp, now=None):
    now = now or now_ist()
    c = connection()
    saved = c.execute('SELECT payload FROM celestial_exit_state WHERE signal_id=?', (pos.signal_id,)).fetchone()
    state = json.loads(saved[0]) if saved else {}
    minute = now.replace(second=0, microsecond=0)
    last = datetime.fromisoformat(state['ts']) if state else None
    previous_minute = datetime.fromisoformat(state['minute']) if state else None
    evidence = celestial.health('structure:' + pos.index)
    live = bool(evidence and celestial.fresh(evidence['ts'], now)
                and celestial.fresh(evidence.get('spot_at'), now))
    beyond = None
    if live:
        vwap, spot = evidence.get('vwap'), evidence.get('spot')
        if celestial.finite(vwap, .000001) and celestial.finite(spot, .000001):
            beyond = (pos.direction == 'CE' and spot < vwap) or (pos.direction == 'PE' and spot > vwap)
    # Gaps reset the indicator, never fill missing candles with invented prices.
    if last is None or not 0 <= (now-last).total_seconds() <= 90 or last.date() != now.date():
        state = dict(minute=minute.isoformat(), ts=now.isoformat(), close=ltp, count=0, ema=None,
                     below=0, vwap_breaks=0, beyond=None)
    elif minute > previous_minute:
        if (minute-previous_minute).total_seconds() != 60:
            state.update(count=0, ema=None, below=0, vwap_breaks=0, beyond=None)
        close = state['close']
        state['ema'] = close if state['ema'] is None else .2*close + .8*state['ema']
        state['count'] += 1
        # 2026-10-19: count consecutive COMPLETED 1m closes, not a single tick.
        state['below'] = state.get('below', 0) + 1 if close < state['ema'] else 0
        state['vwap_breaks'] = state.get('vwap_breaks', 0) + 1 if state.get('beyond') else 0
        state['minute'] = minute.isoformat()
    state.update(ts=now.isoformat(), close=ltp, beyond=beyond)
    with c:
        c.execute('INSERT OR REPLACE INTO celestial_exit_state VALUES (?,?)',
                  (pos.signal_id, json.dumps(state, allow_nan=False)))
    # 2026-10-19: spread width is an exit-pricing concern only (cheap expiry
    # strikes routinely quote >3% wide) - it no longer forces an exit.
    if state['count'] >= 9 and state.get('below', 0) >= CONFIRM_CANDLES and ltp < state['ema']:
        return 'premium_below_completed_1m_ema9'
    if live:
        if beyond and state.get('vwap_breaks', 0) >= CONFIRM_CANDLES:
            return 'underlying_lost_vwap'
        opposite = 'PE' if pos.direction == 'CE' else 'CE'
        if evidence.get('direction') == opposite and evidence.get('score', 0) >= 70:
            return 'opposite_gamma_confirmed'
        if evidence.get('shield_direction') == opposite and (evidence.get('shield_score') or 0) >= 70:
            return 'opposite_shield_active'
        if evidence.get('oi_direction') == opposite and evidence.get('score', 0) >= 70:
            return 'directional_oi_reversed'
    news = celestial.news_impact(pos.index, pos.direction, now)
    if news['news_blocked']:
        return 'fresh_catalyst_risk_block'
    return None
