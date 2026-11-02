"""PULSE forecast (2-Nov-2026, trader approved 1-Nov). Display only.

Never places, sizes or changes an order, never changes a stored forecast row, never feeds the band
that stop-losses follow. Called every second from live_forecast.LiveForecaster.tick.

1. Reforecast on deviation: the pulse line is frozen at each revision (not re-anchored every second),
   so the miss between live price and the line is real. When the miss reaches the trigger
   (5 pts on NIFTY; scaled by price for bigger indices: 5 x spot/25000, min 5) a new revision is
   made at once (min 3 s apart). Each revision re-anchors the path in force (hour, else day) on the
   live price and tilts its first 15 minutes by the last 3 minutes' momentum (fading, tau 6 min).
2. Milestones: turning points of the pulse line (swing >= trigger), with a best-entry plan:
   side, entry (the next pullback milestone or now), stop just beyond it, target at the next turn.
3. Circuit mode: when a 50-100 pt class surge looks likely (sharp-move meter HIGH with a lean, a
   5-minute surge >= 0.15% of spot, or an early Marubozu) the line becomes a step ("electric
   circuit") path toward a projected move sized from the day: 4x today's median closed 5-minute range,
   scaled up by signal strength (meter HIGH score, surge size, Marubozu range), kept within 0.15%-0.6%
   of spot, over ~16 minutes. Both directions.
5. Dynamic maximum stop-loss (2-Nov night, trader approved): for the plan's side, the worst price the
   pulse line still allows before its target (line +/- one typical move x sqrt(minutes ahead)) plus 1.5
   typical 1-minute moves, never closer than 3 typical moves to the price. Recomputed every second;
   it only tightens (trails), never loosens. A side change must hold 10 s before the stop resets.
4. Early Marubozu: the forming 5-minute candle, after 2 minutes, with body >= 80% of range, close
   within 15% of the extreme and range >= 1.2x the day's median 5-minute range; the last closed
   5-minute candle with body >= 90% of range is reported too.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

TRIGGER_BASE = 5.0
MIN_REV_GAP_SEC = 3.0
MAX_REV_AGE_SEC = 120.0
TILT_MIN = 15
TILT_TAU = 6.0
SURGE_PCT = 0.0015
MOVE_PCT = 0.003            # legacy fixed size (kept for reference)
MOVE_MULT = 4.0             # x median closed 5-min range
MOVE_MIN_PCT = 0.0015
MOVE_MAX_PCT = 0.006
CIRCUIT_MIN = 16
CIRCUIT_HOLD_SEC = 180
SL_ROOM_SIG = 1.5           # max SL: room beyond the forecast worst case, in typical 1-min moves
SL_MIN_SIG = 3.0            # ... and never closer than this many typical moves to the price
SL_SIDE_HOLD_SEC = 10       # a new plan side must hold this long before the stop resets
SL_WAIT_KEEP_SEC = 120      # keep showing the stop this long while the plan says WAIT

_S: dict = {}


def trigger_for(spot):
    return round(max(TRIGGER_BASE, TRIGGER_BASE * (spot or 0) / 25000.0), 1)


def _dt(ts):
    return ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)


def _interp(line, t):
    """line: [(datetime, v)] sorted. Value at t (clamped)."""
    if not line:
        return None
    if t <= line[0][0]:
        return line[0][1]
    for (t0, v0), (t1, v1) in zip(line, line[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds() or 1.0
            return v0 + (v1 - v0) * (t - t0).total_seconds() / span
    return line[-1][1]


def _ago(spots, now, sec):
    """Price about `sec` seconds ago from a deque of (t, p)."""
    target = now - timedelta(seconds=sec)
    best = None
    for t, p in reversed(spots):
        if t <= target:
            best = p
            break
    return best


def candles(spots, now, minutes=5):
    """Completed + forming candles from (t, p) ticks of today: [(start, o, h, l, c)]."""
    out = []
    cur = None
    for t, p in spots:
        if t.date() != now.date():
            continue
        m = t.hour * 60 + t.minute
        start_m = m - (m - (9 * 60 + 15)) % minutes
        key = t.replace(hour=start_m // 60, minute=start_m % 60, second=0, microsecond=0)
        if cur is None or cur[0] != key:
            if cur is not None:
                out.append(tuple(cur))
            cur = [key, p, p, p, p]
        else:
            cur[2] = max(cur[2], p)
            cur[3] = min(cur[3], p)
            cur[4] = p
    if cur is not None:
        out.append(tuple(cur))
    return out


def marubozu(spots, now, spot):
    cs = candles(spots, now, 5)
    if not cs:
        return dict(state='NONE')
    done = cs[:-1]
    ranges = sorted(c[2] - c[3] for c in done) if done else []
    med = ranges[len(ranges) // 2] if len(ranges) >= 3 else (spot or 0) * 0.0008
    med = med or 1e-9
    out = dict(state='NONE', median_5m_range=round(med, 2))
    st, o, h, l, c = cs[-1]
    elapsed = (now - st).total_seconds()
    rng = h - l
    if elapsed >= 120 and rng > 0:
        body = abs(c - o) / rng
        up = c > o
        near = (h - c) / rng <= 0.15 if up else (c - l) / rng <= 0.15
        if body >= 0.8 and near and rng >= 1.2 * med:
            out.update(state='FORMING', dir='UP' if up else 'DOWN', tf='5m', body_ratio=round(body, 2),
                       range=round(rng, 2), range_x=round(rng / med, 2), since=st.isoformat())
            return out
    if done:
        st, o, h, l, c = done[-1]
        rng = h - l
        if rng > 0 and abs(c - o) / rng >= 0.9 and rng >= 1.2 * med:
            out.update(state='CLOSED', dir='UP' if c > o else 'DOWN', tf='5m',
                       body_ratio=round(abs(c - o) / rng, 2), range=round(rng, 2),
                       range_x=round(rng / med, 2), since=st.isoformat())
    return out


def turns(line, min_swing):
    """Turning points of a [(t, v)] line (zigzag with min_swing); the last extreme is included."""
    out = []
    if len(line) < 2:
        return out
    direction = 0
    ext_t, ext_v = line[0]
    lo_t, lo_v = hi_t, hi_v = line[0]
    for t, v in line[1:]:
        if direction == 0:
            if v > hi_v:
                hi_t, hi_v = t, v
            if v < lo_v:
                lo_t, lo_v = t, v
            if hi_v - line[0][1] >= min_swing:
                direction, ext_t, ext_v = 1, hi_t, hi_v
            elif line[0][1] - lo_v >= min_swing:
                direction, ext_t, ext_v = -1, lo_t, lo_v
            continue
        if direction == 1:
            if v > ext_v:
                ext_t, ext_v = t, v
            elif ext_v - v >= min_swing:
                out.append(dict(ts=ext_t.isoformat(), v=round(ext_v, 2), kind='high'))
                direction, ext_t, ext_v = -1, t, v
        else:
            if v < ext_v:
                ext_t, ext_v = t, v
            elif v - ext_v >= min_swing:
                out.append(dict(ts=ext_t.isoformat(), v=round(ext_v, 2), kind='low'))
                direction, ext_t, ext_v = 1, t, v
    if direction:
        out.append(dict(ts=ext_t.isoformat(), v=round(ext_v, 2), kind='high' if direction == 1 else 'low'))
    return out


def plan_for(now, spot, line, ms, trig):
    """Best-entry plan from the pulse line: direction of the next big leg, entry, stop, target."""
    if not line:
        return None
    hi = max(line, key=lambda x: x[1])
    lo = min(line, key=lambda x: x[1])
    up_leg = hi[1] - spot
    dn_leg = spot - lo[1]
    if max(up_leg, dn_leg) < 2 * trig:
        return dict(side='WAIT', note='no leg of at least 2x trigger ahead')
    side = 'CE' if up_leg >= dn_leg else 'PE'
    tgt = hi if side == 'CE' else lo
    pull = None
    for m in ms:
        mt = _dt(m['ts'])
        if mt >= tgt[0]:
            break
        if side == 'CE' and m['kind'] == 'low':
            pull = m
            break
        if side == 'PE' and m['kind'] == 'high':
            pull = m
            break
    entry = pull['v'] if pull else spot
    entry_ts = pull['ts'] if pull else now.isoformat()
    buf = max(trig, 0.25 * abs(tgt[1] - entry))
    stop = entry - buf if side == 'CE' else entry + buf
    return dict(side=side, entry=round(entry, 2), entry_ts=entry_ts, entry_is='pullback' if pull else 'now',
                stop=round(stop, 2), target=round(tgt[1], 2), target_ts=tgt[0].isoformat(),
                reward_risk=round(abs(tgt[1] - entry) / buf, 2))


def circuit_path(now, spot, direction, move):
    """Step path: hold, jump, hold, jump ... 4 steps over CIRCUIT_MIN minutes."""
    sgn = 1 if direction == 'UP' else -1
    steps, pts = 4, [(now, spot)]
    seg = CIRCUIT_MIN / steps
    v = spot
    t = now
    levels = []
    for k in range(steps):
        t_hold = t + timedelta(minutes=seg * 0.6)
        pts.append((t_hold, v))                       # horizontal: pause / consolidation
        v = v + sgn * move / steps
        t = t + timedelta(minutes=seg)
        pts.append((t, v))                            # near-vertical: impulse leg
        levels.append(dict(ts=t.isoformat(), v=round(v, 2), kind='step', k=k + 1))
    return pts, levels


def move_size(spot, mz, meter, surge):
    """Projected circuit move (2-Nov after-hours, trader approved): MOVE_MULT x today's median closed
    5-minute range, scaled up by signal strength, kept within MOVE_MIN_PCT..MOVE_MAX_PCT of spot."""
    med = (mz or {}).get('median_5m_range') or spot * 0.0008
    k = 1.0
    if (meter or {}).get('available') and meter.get('level') == 'HIGH':
        k += 0.5 * min(1.0, (meter.get('score') or 0) / 100.0)
    if surge:
        k += 0.5 * min(1.0, abs(surge) / (2 * SURGE_PCT * spot))
    if (mz or {}).get('state') == 'FORMING' and (mz.get('range_x') or 0) >= 2:
        k += 0.25
    move = MOVE_MULT * med * k
    return round(min(max(move, MOVE_MIN_PCT * spot), MOVE_MAX_PCT * spot), 1)


def sigma_1m(spots, now, spot):
    """Typical 1-minute move today: 1.25 x median |change| between minute closes (robust), clamped."""
    closes = {}
    cut = now.replace(second=0, microsecond=0)
    for t, p in spots:
        if t.date() == now.date() and t < cut:
            closes[t.replace(second=0, microsecond=0)] = p
    cl = [v for _, v in sorted(closes.items())][-90:]
    lo, hi = 0.00015 * spot, 0.003 * spot
    if len(cl) < 6:
        return 0.0004 * spot
    d = sorted(abs(b - a) for a, b in zip(cl, cl[1:]))
    return min(max(1.25 * d[len(d) // 2], lo), hi)


def _sl_candidate(now, spot, fwd, side, target_ts, sig):
    down = side == 'PE'
    tgt_t = _dt(target_ts) if target_ts else None
    worst = spot
    for t, v in fwd:
        if tgt_t and t > tgt_t:
            break
        hw = sig * math.sqrt(max((t - now).total_seconds() / 60.0, 0.0))
        worst = max(worst, v + hw) if down else min(worst, v - hw)
    cand = worst + (SL_ROOM_SIG * sig if down else -SL_ROOM_SIG * sig)
    return max(cand, spot + SL_MIN_SIG * sig) if down else min(cand, spot - SL_MIN_SIG * sig)


def trail_sl(st, now, spot, fwd, plan, sig):
    """Dynamic maximum stop-loss for the plan's side (display only). Returns a dict or None."""
    side = (plan or {}).get('side')
    s = st.get('sl')
    if s and s['hit'] and (now - _dt(s['hit'])).total_seconds() > 300:
        st['sl'] = s = None                             # 5 minutes after a stop-out, start a fresh stop
    if side in ('CE', 'PE') and (s is None or s['side'] != side):
        pend = st.get('sl_pending')
        if s is None or (pend and pend[0] == side and (now - pend[1]).total_seconds() >= SL_SIDE_HOLD_SEC):
            lvl = round(_sl_candidate(now, spot, fwd, side, plan.get('target_ts'), sig), 1)
            s = dict(side=side, level=lvl, init=lvl, since=now.isoformat(), hist=[[now.isoformat(), lvl]],
                     hit=None, plan_stop=plan.get('stop'))
            st['sl'], st['sl_pending'] = s, None
        elif not pend or pend[0] != side:
            st['sl_pending'] = (side, now)
    elif side == s_side(s):
        st['sl_pending'] = None
    if s is None:
        return None
    if side not in ('CE', 'PE'):
        if (now - _dt(s['hist'][-1][0])).total_seconds() > SL_WAIT_KEEP_SEC and not s['hit']:
            st['sl'] = None
            return None
    elif side == s['side'] and not s['hit']:
        down = side == 'PE'
        cand = _sl_candidate(now, spot, fwd, side, plan.get('target_ts'), sig)
        if (down and cand < s['level'] - 0.5) or (not down and cand > s['level'] + 0.5):
            s['level'] = round(cand, 1)
            s['hist'] = (s['hist'] + [[now.isoformat(), s['level']]])[-60:]
        s['plan_stop'] = plan.get('stop')
    if not s['hit'] and ((s['side'] == 'PE' and spot >= s['level']) or (s['side'] == 'CE' and spot <= s['level'])):
        s['hit'] = now.isoformat()
    return dict(side=s['side'], level=s['level'], init=s['init'], trailed=round(abs(s['init'] - s['level']), 1),
                room=round(abs(s['level'] - spot), 1), since=s['since'], hit=s['hit'], hist=s['hist'][-30:],
                plan_stop=s['plan_stop'], pending=(st.get('sl_pending') or (None,))[0])


def s_side(s):
    return s['side'] if s else None


def update(name, now, spot, snap, spots):
    """Returns the pulse dict for this index (or None when no spot)."""
    if spot is None:
        return None
    spots = list(spots or [])
    st = _S.setdefault(name, dict(rev=0, line=[], rev_ts=None, reason=None, day=None, circuit=None,
                                  revs=[]))
    if st['day'] != now.date():
        st.update(rev=0, line=[], rev_ts=None, reason=None, day=now.date(), circuit=None, revs=[])
    trig = trigger_for(spot)
    mz = marubozu(spots, now, spot)
    meter = snap.get('meter') or {}
    # ---- big-move detection (circuit mode)
    p5 = _ago(spots, now, 300)
    surge = (spot - p5) if p5 else 0.0
    why = []
    direction = None
    if meter.get('available') and meter.get('level') == 'HIGH' and meter.get('lean') in ('UP', 'DOWN'):
        why.append('meter HIGH %s' % meter.get('lean'))
        direction = meter['lean']
    if p5 and abs(surge) >= SURGE_PCT * spot:
        why.append('5-min surge %+.0f' % surge)
        direction = direction or ('UP' if surge > 0 else 'DOWN')
    if mz.get('state') == 'FORMING':
        why.append('Marubozu forming %s' % mz['dir'])
        direction = direction or mz['dir']
    circ = st['circuit']
    if why and direction:
        if not circ or circ['dir'] != direction:
            circ = dict(dir=direction, since=now.isoformat(), why=why,
                        move=move_size(spot, mz, meter, surge))
            st['circuit'] = circ
            st['line'] = []                           # force a new revision in circuit shape
        circ['last'] = now.isoformat()
        circ['why'] = why
    elif circ and (now - _dt(circ['last'])).total_seconds() > CIRCUIT_HOLD_SEC:
        st['circuit'] = circ = None
        st['line'] = []
    # ---- deviation / revision
    pred = _interp(st['line'], now) if st['line'] else None
    dev = (spot - pred) if pred is not None else None
    age = (now - st['rev_ts']).total_seconds() if st['rev_ts'] else 1e9
    need = None
    if not st['line']:
        need = 'start' if st['rev'] == 0 else 'mode change'
    elif abs(dev) >= trig and age >= MIN_REV_GAP_SEC:
        need = 'miss %+.1f >= %.1f' % (dev, trig)
    elif age >= MAX_REV_AGE_SEC:
        need = 'refresh'
    if need:
        if circ:
            line, levels = circuit_path(now, spot, circ['dir'], circ['move'])
        else:
            src = (snap.get('hour') or {}).get('line') or (snap.get('day') or {}).get('line') or []
            base = [(_dt(p['ts']), float(p['v'])) for p in src]
            if not base:
                base = [(now, spot), (now + timedelta(minutes=30), spot)]
            p3 = _ago(spots, now, 180)
            slope = ((spot - p3) / 3.0) if p3 else 0.0       # pts per minute
            line = []
            for t, v in base:
                mins = max(0.0, (t - now).total_seconds() / 60.0)
                tilt = slope * TILT_TAU * (1 - math.exp(-min(mins, TILT_MIN) / TILT_TAU))
                line.append((t, v - base[0][1] + spot + tilt))
            line[0] = (now, spot)
            levels = None
        st['rev'] += 1
        st['line'] = line
        st['rev_ts'] = now
        st['reason'] = need
        st['levels'] = levels
        st['revs'] = (st['revs'] + [dict(rev=st['rev'], ts=now.isoformat(), spot=round(spot, 2),
                                         reason=need)])[-30:]
        pred, dev = spot, 0.0
    line = st['line']
    ms = st.get('levels') or turns(line, trig)
    fwd = [(t, v) for t, v in line if t >= now] or line[-1:]
    if circ:
        tgt = line[-1]
        sgn = 1 if circ['dir'] == 'UP' else -1
        plan = dict(side='CE' if sgn > 0 else 'PE', entry=round(spot, 2), entry_is='now (momentum)',
                    stop=round(spot - sgn * max(trig * 2, 0.3 * circ['move']), 2), target=round(tgt[1], 2),
                    target_ts=tgt[0].isoformat())
        plan['reward_risk'] = round(abs(plan['target'] - plan['entry']) / abs(plan['entry'] - plan['stop']), 2)
    else:
        plan = plan_for(now, spot, fwd, [m for m in ms if _dt(m['ts']) >= now], trig)
    sig = sigma_1m(spots, now, spot)
    try:
        msl = trail_sl(st, now, spot, fwd, plan, sig)
    except Exception:                                   # noqa: BLE001 - display only
        msl = None
    return dict(mode='circuit' if circ else 'smooth', rev=st['rev'], max_sl=msl, sigma_1m=round(sig, 2), rev_ts=st['rev_ts'].isoformat(),
                rev_reason=st['reason'], trigger=trig, dev=None if dev is None else round(dev, 2),
                line=[dict(ts=t.isoformat(), v=round(v, 2)) for t, v in line],
                milestones=ms[:8], plan=plan, circuit=circ, marubozu=mz, revs=st['revs'][-10:],
                revs_today=st['rev'])
