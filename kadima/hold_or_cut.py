"""Hold winners, cut losers (trader ask, 21-Oct-2026). SHADOW ONLY: no orders.

21-Oct: four NIFTY PEs that agreed with the falling market were sold at +3.5% to
+7.2% and then ran to +15..+70%, while a MIDCPNIFTY CE bought against the trend
fell 68%. Premium-tick rules (EMA9, VWAP, "no expansion in N min") cannot tell a
pause in a winner from a real reversal. This rule judges the trade by the
underlying index, which is what moves the premium:

LOSER  - premium below entry + costs and the index has moved against the trade
         (0.5 ATR if the trade is against the day trend, 1 ATR otherwise) for
         CONFIRM completed 1-min closes -> exit now. No waiting for a % stop.
WINNER - premium above entry + costs, the trade not against the day trend, and
         either the day trend agrees with it or the index has moved PROVE ATR
         in its favour: the trade is a winner (sticky). Soft premium signals are then ignored. It exits
         only when: premium falls to entry + costs (breakeven lock), the index
         gives back max(1 ATR, half of its best favourable move) for CONFIRM
         closes, or the day trend turns against it.
Otherwise the existing logic decides. The 35% premium hard cap always applies.

Every threshold is in 5-minute ATR of the index; the only fixed numbers are the
35% cap and the 1% cost reserve, both already in trade_policy / trend_rules.
"""
from __future__ import annotations

import json
import math

CUT_COUNTER = 0.5   # ATR against entry that ends a counter-trend loser
CUT_ANY = 1.0       # ATR against entry that ends any other loser
PROVE = 0.5         # ATR in favour that makes a trade a winner
GIVEBACK = 1.0      # minimum ATR give-back that ends a winner
RETRACE = 0.5        # share of the best index move a winner may give back
RETRACE_TREND = 0.618  # 'fib' / 'oi' variants while the day trend agrees
RETRACE_WALL = 0.382   # 'oi' variant while the index sits at the favourable OI wall
CONFIRM = 2         # consecutive completed 1-min index closes for any index-based exit
HARD_CAP = 0.35
COST = 0.01
SOFT_HOLD = 'winner_hold'   # reason recorded when a winner ignores a soft exit


def _ok(*values):
    try:
        return all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


def against(trend, direction):
    return (trend == 'DOWN' and direction == 'CE') or (trend == 'UP' and direction == 'PE')


def new_state(entry_spot):
    return dict(entry_spot=entry_spot, best=0.0, phase='open', adverse=0, giveback=0)


def step(state, direction, entry, ltp, spot, atr, trend, closed=True, variant='base', wall=None):
    """One decision. `spot` is a completed 1-min index close when closed=True
    (counters only move on completed closes). Returns (action, reason) where
    action is 'exit', 'hold' (winner: ignore soft exits) or 'none' (existing
    logic decides). Mutates `state`.

    variant 'base': a winner may give back half its best index move (a 50% retracement).
    variant 'fib':  61.8% while the day trend agrees, 50% otherwise.
    variant 'oi':   'fib', but tightened to 38.2% while the index sits at the option-
                    chain wall in the trade's favour (`wall`: the biggest put-OI strike
                    below spot for a PE, call-OI strike above spot for a CE), and back to
                    'fib' once the index has closed CONFIRM times 0.5 ATR through it."""
    if not _ok(entry, ltp):
        return 'none', 'no_premium'
    if ltp <= entry * (1 - HARD_CAP):
        return 'exit', 'hard_cap'
    if not _ok(spot, atr, state.get('entry_spot')):
        return ('hold', SOFT_HOLD) if state['phase'] == 'winner' else ('none', 'no_index_data')
    side = 1 if direction == 'CE' else -1
    # Points, not ATR units: ATR changes during the day, a best move must not.
    fav = (spot - state['entry_spot']) * side
    state['best'] = max(state['best'], fav)
    state['fav'] = round(fav, 2)
    counter = against(trend, direction)
    floor = entry * (1 + COST)

    agrees = (trend == 'UP' and direction == 'CE') or (trend == 'DOWN' and direction == 'PE')
    if (state['phase'] != 'winner' and ltp > floor and not counter
            and (agrees or state['best'] >= PROVE * atr)):
        state['phase'] = 'winner'
        state['adverse'] = 0
    if state['phase'] == 'winner':
        if counter:
            return 'exit', 'winner_trend_turned'
        if ltp <= floor:
            return 'exit', 'winner_breakeven_lock'
        retrace = RETRACE
        if variant in ('fib', 'oi') and agrees:
            retrace = RETRACE_TREND
        if variant == 'oi' and _ok(wall):
            beyond = (spot - wall) * side          # > 0: index has gone through the wall
            if closed:
                state['through'] = state.get('through', 0) + 1 if beyond >= 0.5 * atr else 0
            if state.get('through', 0) < CONFIRM and abs(spot - wall) <= 0.5 * atr:
                retrace = RETRACE_WALL
        give = max(GIVEBACK * atr, state['best'] * retrace)
        if closed:
            state['giveback'] = state['giveback'] + 1 if fav <= state['best'] - give else 0
        if state['giveback'] >= CONFIRM:
            return 'exit', 'winner_index_gave_back'
        return 'hold', SOFT_HOLD

    if ltp < floor:
        limit = CUT_COUNTER if counter else CUT_ANY
        if closed:
            state['adverse'] = state['adverse'] + 1 if fav <= -limit * atr else 0
        if state['adverse'] >= CONFIRM:
            return 'exit', 'cut_counter_trend' if counter else 'cut_index_against'
    elif closed:
        state['adverse'] = 0
    return 'none', 'undecided'


def dumps(state):
    return json.dumps(state, allow_nan=False)


# ------------------------------------------------------------------ live shadow
# Called once per check_exits cycle per open position, after KADIMA has chosen
# its own exit reason (or none). Logs what this rule would do; changes nothing.
HARD_REASONS = frozenset({'daily_loss_limit_breached', 'eod_force_close', 'mcx_session_exit'})
_logged: dict = {}


def _conn():
    from . import storage
    c = storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS hold_or_cut_state '
              '(signal_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    return c


VARIANTS = ('base', 'fib', 'oi')


def wall_from(rows, spot, direction):
    """rows: (strike, opt_type, oi) of one chain snapshot. The favourable wall:
    biggest put OI at/below spot for a PE, biggest call OI at/above spot for a CE."""
    want = [(oi or 0, k) for k, t, oi in rows if k and t == direction and
            ((direction == 'PE' and k <= spot) or (direction == 'CE' and k >= spot))]
    return max(want)[1] if want else None


def _live_wall(pos, spot):
    from . import storage
    c = storage.conn()
    last = c.execute('SELECT MAX(ts) FROM oi_snapshots WHERE index_name=?', (pos.index,)).fetchone()[0]
    if not last:
        return None
    rows = c.execute('SELECT strike, opt_type, oi FROM oi_snapshots WHERE index_name=? AND ts=?',
                     (pos.index, last)).fetchall()
    return wall_from([tuple(r) for r in rows], spot, pos.direction)


def _index_inputs(pos, now):
    """(spot, atr, trend) from what the engine already publishes, or Nones."""
    from . import celestial, config, trend_rules
    idx = config.GAMMA_INDICES.get(pos.index)
    h = celestial.health('structure:' + pos.index)
    if idx is None or not h or not celestial.fresh(h.get('ts'), now) \
            or not celestial.fresh(h.get('spot_at'), now) or not _ok(h.get('spot')):
        return None, None, None
    spot = float(h['spot'])
    atr = trend_rules.inputs(idx.token, now).get('atr')
    trend = trend_rules.day_trend(pos.index, idx.token, spot, h.get('vwap'), now)
    return spot, atr, trend


def shadow(pos, ltp, kadima_reason, now):
    # CAS and fast-momentum positions are included: their own timers
    # (no expansion in 2/5 min, option VWAP lost) are prime early-exit suspects.
    if getattr(pos, 'status', 'open') != 'open':
        return None
    from . import audit
    spot, atr, trend = _index_inputs(pos, now)
    c = _conn()
    row = c.execute('SELECT payload FROM hold_or_cut_state WHERE signal_id=?', (pos.signal_id,)).fetchone()
    saved = json.loads(row[0]) if row else None
    if saved is None or saved.get('day') != now.date().isoformat():
        if not _ok(spot):
            return None
        saved = dict(day=now.date().isoformat(), minute=None, done={},
                     states={v: new_state(spot) for v in VARIANTS})
    minute = now.replace(second=0, microsecond=0).isoformat()
    closed = saved.get('minute') is not None and minute > saved['minute']
    if saved.get('minute') is None or closed:
        saved['minute'] = minute
    try:
        wall = _live_wall(pos, spot) if _ok(spot) else None
    except Exception:  # noqa: BLE001 - the wall is optional evidence
        wall = None
    out = {}
    for v in VARIANTS:
        if v in saved['done']:          # this variant already exited in shadow
            continue
        state = saved['states'][v]
        action, reason = step(state, pos.direction, pos.entry_price, ltp, spot, atr, trend,
                              closed=closed, variant=v, wall=wall)
        out[v] = (action, reason)
        if kadima_reason:
            verdict = ('agree' if action in ('exit', 'none') or kadima_reason in HARD_REASONS
                       else 'would_hold')
        else:
            verdict = 'would_exit' if action == 'exit' else None
        if action == 'exit':
            saved['done'][v] = dict(ts=now.isoformat(), ltp=ltp, rule=reason)
        key = (v, verdict, kadima_reason or reason)
        if verdict and key not in _logged.setdefault(pos.signal_id, set()):
            _logged[pos.signal_id].add(key)
            audit.log('hold_or_cut_shadow', signal_id=pos.signal_id, symbol=getattr(pos, 'symbol', None),
                      variant=v, verdict=verdict, kadima_reason=kadima_reason, rule=reason,
                      phase=state['phase'], ltp=ltp, entry=pos.entry_price, spot=spot,
                      entry_spot=state['entry_spot'], fav_pts=state.get('fav'),
                      best_pts=round(state['best'], 2), wall=wall,
                      atr=None if atr is None else round(atr, 2), trend=trend)
    with c:
        c.execute('INSERT OR REPLACE INTO hold_or_cut_state VALUES (?,?)', (pos.signal_id, json.dumps(saved)))
    return out
