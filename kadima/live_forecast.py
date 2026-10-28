"""Live forecast line + sharp-move meter (shadow). Display only. 28-Oct-2026.

Runs as a thread beside the hourly forecaster. Never places, sizes or changes an order, never
changes a forecast row, never feeds the band that stop-losses follow.

1. Live line (every second): the forecast path in force for the current hour and for the day
   (original, overridden by each reforecast), re-anchored on the current live price. The band
   narrows with the time left: half-width x sqrt(time left / original horizon).

2. Sharp-move meter (every 5 s, from the engine's latest complete option-chain sample, ATM +/-5):
   - dealer gamma (sum of gamma x OI, calls +, puts -; negative = moves tend to accelerate,
     positive = price tends to pin), and the strike with the most gamma (pin level);
   - wall stress: OI unwinding at the nearest CE wall above / PE wall below over 10 minutes;
   - delta flow: change in delta-weighted OI over 5 minutes (read as writers' positioning);
   - ATM IV change over 5 minutes; ATM CE vs PE premium lead over 1 minute;
   - squeeze: recent 1-minute ranges small against the day's typical range.
   Score 0-100 and an UP/DOWN lean. Rules are a starting point and UNCALIBRATED: every minute the
   reading is stored in forecast.db (live_meter) so it can be measured against what price did.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from . import audit
from . import hourly_forecast as HF
from . import hourly_forecast_model as M
from .config import IST

TICK_SEC = 1.0
METER_SEC = 5.0
LOG_SEC = 60.0
PATH_REFRESH_SEC = 15.0
SAMPLE_FRESH_SEC = 60.0
LIVE = {}                      # index -> latest snapshot (read by control_api)

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_meter (
    ts TEXT NOT NULL, index_name TEXT NOT NULL, spot REAL, score REAL, lean TEXT,
    lean_strength REAL, parts TEXT NOT NULL,
    PRIMARY KEY (ts, index_name)
);
"""


def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# ---------------------------------------------------------------- live line
def path_in_force(points, revisions):
    """{ts: (forecast, half_width, horizon_start)} for the path in force: the original, overridden from
    each revision's trigger onward. horizon_start is when that path's band started (for narrowing)."""
    out = {}
    for p in points:
        out[p['ts']] = (p['forecast'], (p['upper'] - p['lower']) / 2.0, None)
    for v in revisions:
        pts = v['points'] if isinstance(v['points'], list) else json.loads(v['points'])
        if v.get('trigger_actual') is not None:          # the revision starts at the price that triggered it
            out[v['trigger_ts']] = (v['trigger_actual'], 0.0, v['trigger_ts'])
        for q in pts:
            out[q['ts']] = (q['forecast'], (q['upper'] - q['lower']) / 2.0, v['trigger_ts'])
    return out


def live_line(path, start_ts, start_value, now, spot):
    """Re-anchor the path in force on the live price. Returns [{ts, v, up, lo}] from now to the end."""
    if not path or spot is None:
        return []
    keys = sorted(path)
    times = [HF._dt(k) for k in keys]
    t0 = HF._dt(start_ts)
    # value of the path at `now` (linear between marks; the slot start is the anchor)
    prev_t, prev_v = t0, start_value
    base = None
    for t, k in zip(times, keys):
        if t >= now:
            span = (t - prev_t).total_seconds() or 1.0
            w = _clip((now - prev_t).total_seconds() / span, 0.0, 1.0)
            base = prev_v + (path[k][0] - prev_v) * w
            break
        prev_t, prev_v = t, path[k][0]
    if base is None:
        return []
    shift = spot - base
    out = [dict(ts=now.isoformat(), v=round(spot, 2), up=round(spot, 2), lo=round(spot, 2))]
    for t, k in zip(times, keys):
        if t <= now:
            continue
        fc, hw, hstart = path[k]
        h0 = HF._dt(hstart) if hstart else t0
        full = max((t - h0).total_seconds(), 1.0)
        left = max((t - now).total_seconds(), 0.0)
        hw_live = hw * math.sqrt(_clip(left / full, 0.0, 1.0))
        v = fc + shift
        out.append(dict(ts=k, v=round(v, 2), up=round(v + hw_live, 2), lo=round(v - hw_live, 2)))
    return out


# ---------------------------------------------------------------- meter
def _greeks(spot, q, days):
    from .greeks import implied_vol_and_greeks
    try:
        iv, g = implied_vol_and_greeks(spot, q['strike'], q['ltp'], days, q['opt_type'])
    except Exception:                                   # noqa: BLE001
        return None, None, None
    if not g:
        return iv, None, None
    return iv, g.get('delta'), g.get('gamma')


def chain_state(spot, quotes, days):
    """Per-sample aggregates from one complete chain sample (list of quote dicts)."""
    rows = []
    for q in quotes:
        if not q.get('ltp') or q.get('oi') is None:
            continue
        iv, delta, gamma = _greeks(spot, q, days)
        if delta is None or gamma is None:
            continue
        rows.append(dict(strike=q['strike'], t=q['opt_type'], ltp=q['ltp'], oi=q['oi'], iv=iv,
                         delta=delta, gamma=gamma))
    if len(rows) < 6:
        return None
    gex = sum((1 if r['t'] == 'CE' else -1) * r['gamma'] * r['oi'] for r in rows)
    gabs = sum(r['gamma'] * r['oi'] for r in rows) or 1.0
    by_strike = {}
    for r in rows:
        by_strike[r['strike']] = by_strike.get(r['strike'], 0.0) + r['gamma'] * r['oi']
    pin = max(by_strike, key=by_strike.get)
    net_delta = sum(r['delta'] * r['oi'] for r in rows)
    dabs = sum(abs(r['delta']) * r['oi'] for r in rows) or 1.0
    ce = [r for r in rows if r['t'] == 'CE' and r['strike'] >= spot]
    pe = [r for r in rows if r['t'] == 'PE' and r['strike'] <= spot]
    ce_wall = max(ce, key=lambda r: r['oi']) if ce else None
    pe_wall = max(pe, key=lambda r: r['oi']) if pe else None
    atm = min(rows, key=lambda r: abs(r['strike'] - spot))['strike']
    atm_ce = next((r for r in rows if r['strike'] == atm and r['t'] == 'CE'), None)
    atm_pe = next((r for r in rows if r['strike'] == atm and r['t'] == 'PE'), None)
    ivs = [r['iv'] for r in (atm_ce, atm_pe) if r and r['iv']]
    return dict(gex_norm=gex / gabs, pin=pin, net_delta=net_delta, dabs=dabs,
                ce_wall=(ce_wall['strike'], ce_wall['oi']) if ce_wall else None,
                pe_wall=(pe_wall['strike'], pe_wall['oi']) if pe_wall else None,
                oi_by=dict(((r['strike'], r['t']), r['oi']) for r in rows),
                atm_iv=sum(ivs) / len(ivs) if ivs else None,
                atm_ce=atm_ce['ltp'] if atm_ce else None, atm_pe=atm_pe['ltp'] if atm_pe else None)


def _ago(hist, now, sec):
    """Latest history entry at least `sec` seconds old."""
    best = None
    for t, s in hist:
        if (now - t).total_seconds() >= sec:
            best = s
        else:
            break
    return best


def meter(now, spot, cur, hist, spots):
    """Score 0-100 and lean from the current chain state, its history and recent spot samples."""
    parts = {}
    score = 0.0
    gneg = max(0.0, -cur['gex_norm'])
    parts['dealer_gamma'] = round(cur['gex_norm'], 3)
    parts['pin'] = cur['pin']
    score += 30 * _clip(gneg / 0.5, 0, 1)
    lean = 0.0
    old10 = _ago(hist, now, 600)
    stress_ce = stress_pe = 0.0
    if old10:
        for side, key in (('ce', 'ce_wall'), ('pe', 'pe_wall')):
            w = cur[key]
            if w:
                prev = old10['oi_by'].get((w[0], 'CE' if side == 'ce' else 'PE'))
                if prev:
                    s = max(0.0, 1 - w[1] / prev)
                    if side == 'ce':
                        stress_ce = s
                    else:
                        stress_pe = s
    parts['wall_stress_ce'] = round(stress_ce, 3)
    parts['wall_stress_pe'] = round(stress_pe, 3)
    score += 25 * _clip(max(stress_ce, stress_pe) / 0.15, 0, 1)
    lean += 1.0 * _clip((stress_ce - stress_pe) / 0.15, -1, 1)   # CE wall giving way -> up
    old5 = _ago(hist, now, 300)
    if old5:
        flow = (cur['net_delta'] - old5['net_delta']) / max(cur['dabs'], 1.0)
        parts['delta_flow'] = round(flow, 4)
        lean += -1.0 * _clip(flow / 0.05, -1, 1)               # writers' convention (to be measured)
        if cur['atm_iv'] and old5.get('atm_iv'):
            d_iv = cur['atm_iv'] - old5['atm_iv']
            parts['iv_change_pp'] = round(d_iv, 2)
            score += 15 * _clip(d_iv / 1.0, 0, 1)
    old1 = _ago(hist, now, 60)
    if old1 and cur['atm_ce'] and cur['atm_pe'] and old1.get('atm_ce') and old1.get('atm_pe'):
        lead = (cur['atm_ce'] / old1['atm_ce'] - 1) - (cur['atm_pe'] / old1['atm_pe'] - 1)
        parts['premium_lead'] = round(lead, 4)
        score += 15 * _clip(abs(lead) / 0.06, 0, 1)
        lean += 1.0 * _clip(lead / 0.06, -1, 1)
    # squeeze: last 10 one-minute ranges vs the day's median one-minute range
    if len(spots) > 120:
        mins = {}
        for t, p in spots:
            k = t.replace(second=0, microsecond=0)
            lo, hi = mins.get(k, (p, p))
            mins[k] = (min(lo, p), max(hi, p))
        rng = [hi - lo for lo, hi in mins.values()]
        if len(rng) >= 15:
            med = sorted(rng)[len(rng) // 2] or 1e-9
            recent = sum(rng[-10:]) / len(rng[-10:])
            ratio = recent / med
            parts['squeeze_ratio'] = round(ratio, 2)
            score += 15 * _clip((0.8 - ratio) / 0.5, 0, 1)
    lean_strength = round(_clip(abs(lean) / 2.0, 0, 1), 2)
    direction = 'UP' if lean > 0.3 else 'DOWN' if lean < -0.3 else 'NONE'
    return round(_clip(score, 0, 100), 1), direction, lean_strength, parts


# ---------------------------------------------------------------- thread
class LiveForecaster:
    def __init__(self, hourly):
        self.hourly = hourly
        self.db = hourly.db
        self._stop = threading.Event()
        self._paths = {}                  # (name, kind) -> (loaded_mono, data)
        self._hist = {}                   # name -> deque[(t, chain_state)]
        self._spots = {}                  # name -> deque[(t, spot)] for today
        self._last_meter = {}
        self._last_log = {}
        self._last_sample = {}
        with HF.conn(self.db) as c:
            c.executescript(SCHEMA)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name='live-forecast').start()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            t = time.monotonic()
            try:
                self.tick(datetime.now(IST))
            except Exception as exc:                    # noqa: BLE001 - display only
                audit.log('live_forecast_error', error_type=type(exc).__name__, error=str(exc)[:200])
                self._stop.wait(10)
            self._stop.wait(max(0.05, TICK_SEC - (time.monotonic() - t)))

    def _spot(self, st, now):
        if (st.is_live_tick and st.last_updated and M.finite(st.spot)
                and -30 <= (now - st.last_updated).total_seconds() <= HF.SPOT_FRESH_SEC):
            return st.spot
        return None

    def _load(self, name, kind, day, slot=None):
        key = (name, kind, day, slot)
        hit = self._paths.get(key)
        if hit and time.monotonic() - hit[0] < PATH_REFRESH_SEC:
            return hit[1]
        from . import day_forecast as DF                 # DF.conn also creates the day tables if missing
        c = DF.conn(self.db)
        d = day.isoformat()
        if kind == 'hour':
            r = c.execute('SELECT anchor, slot FROM hourly_forecasts WHERE day=? AND index_name=? AND slot=?',
                          (d, name, slot)).fetchone()
            pts = c.execute('SELECT ts, forecast, upper, lower FROM hourly_forecast_points WHERE day=? AND '
                            'index_name=? AND slot=? ORDER BY k', (d, name, slot)).fetchall() if r else []
            revs = c.execute('SELECT trigger_ts, trigger_actual, points FROM hourly_forecast_revisions WHERE day=? AND index_name=? '
                             'AND slot=? ORDER BY rev', (d, name, slot)).fetchall() if r else []
            start = HF._slot_dt(day, tuple(int(x) for x in slot.split(':'))).isoformat() if r else None
        else:
            r = c.execute('SELECT anchor FROM day_forecasts WHERE day=? AND index_name=?', (d, name)).fetchone()
            pts = c.execute('SELECT ts, forecast, upper, lower FROM day_forecast_points WHERE day=? AND index_name=? '
                            'ORDER BY k', (d, name)).fetchall() if r else []
            revs = c.execute('SELECT trigger_ts, trigger_actual, points FROM day_forecast_revisions WHERE day=? AND index_name=? '
                             'ORDER BY rev', (d, name)).fetchall() if r else []
            start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST).isoformat() if r else None
        data = None
        if r and pts:
            data = dict(start=start, anchor=r['anchor'],
                        path=path_in_force([dict(p) for p in pts], [dict(v) for v in revs]))
        self._paths[key] = (time.monotonic(), data)
        return data

    def tick(self, now):
        if now.weekday() >= 5 or not (9 * 60 + 15 <= now.hour * 60 + now.minute < 15 * 60 + 31):
            return
        eng = self.hourly.engine
        for name in HF.FORECAST_INDICES:
            st = eng.states.get(name) if eng else None
            if st is None:
                continue
            spot = self._spot(st, now)
            snap = dict(index=name, ts=now.isoformat(), spot=spot, hour=None, day=None,
                        meter=(LIVE.get(name) or {}).get('meter'))
            if spot is not None:
                sp = self._spots.setdefault(name, deque(maxlen=6 * 3600))
                if sp and sp[-1][0].date() != now.date():
                    sp.clear()
                sp.append((now, spot))
            slot = M.slot_for(now.time())
            if slot and spot is not None:
                h = self._load(name, 'hour', now.date(), M.slot_label(slot[0]))
                if h:
                    snap['hour'] = dict(slot=M.slot_label(slot[0]),
                                        line=live_line(h['path'], h['start'], h['anchor'], now, spot))
                dd = self._load(name, 'day', now.date())
                if dd:
                    snap['day'] = dict(line=live_line(dd['path'], dd['start'], dd['anchor'], now, spot))
            if spot is not None and time.monotonic() - self._last_meter.get(name, 0) >= METER_SEC:
                self._last_meter[name] = time.monotonic()
                snap['meter'] = self._meter(name, st, now, spot) or snap['meter']
            LIVE[name] = snap

    def _meter(self, name, st, now, spot):
        chain = getattr(st, 'chain', None)
        sample = getattr(chain, '_forecast_sample', None) if chain else None
        if not sample or time.monotonic() - sample.get('mono', 0) > SAMPLE_FRESH_SEC:
            return dict(ts=now.isoformat(), available=False, reason='no_fresh_chain_sample')
        try:
            days = self.hourly.engine._dte(st) if hasattr(self.hourly.engine, '_dte') else 1.0
        except Exception:                               # noqa: BLE001
            days = 1.0
        hist = self._hist.setdefault(name, deque(maxlen=400))
        if sample.get('mono') != self._last_sample.get(name):
            cur = chain_state(spot, sample['quotes'], days)
            self._last_sample[name] = sample.get('mono')
            if cur is None:
                return dict(ts=now.isoformat(), available=False, reason='greeks_unavailable')
            hist.append((now, cur))
        if not hist:
            return dict(ts=now.isoformat(), available=False, reason='warming_up')
        cur = hist[-1][1]
        score, lean, strength, parts = meter(now, spot, cur, list(hist), list(self._spots.get(name, [])))
        level = 'HIGH' if score >= 60 else 'MEDIUM' if score >= 35 else 'LOW'
        out = dict(ts=now.isoformat(), available=True, score=score, level=level, lean=lean,
                   lean_strength=strength, parts=parts, calibrated=False)
        if time.monotonic() - self._last_log.get(name, 0) >= LOG_SEC:
            self._last_log[name] = time.monotonic()
            try:
                c = HF.conn(self.db)
                with c:
                    c.execute('INSERT OR REPLACE INTO live_meter VALUES (?,?,?,?,?,?,?)',
                              (now.replace(microsecond=0).isoformat(), name, spot, score, lean, strength,
                               json.dumps(parts)))
            except Exception as exc:                    # noqa: BLE001
                audit.log('live_meter_log_error', error=str(exc)[:200])
        return out


def snapshot(name):
    return LIVE.get(name) or dict(index=name, available=False, reason='not_running_or_outside_market_hours')
