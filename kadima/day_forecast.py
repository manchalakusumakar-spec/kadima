"""Day path forecast for KADIMA Celestial (09:15-15:30, 5-minute points). Display only.

Same model and the same 5-minute style as the hourly slots, over the whole session:

  * 09:15  the original day path: 75 points (09:20 ... 15:30), band and expected swings.
  * Every hourly milestone (10:15, 11:15, 12:15, 13:15, 14:15, 15:15) a reforecast is always
    published from that point to 15:30, rebuilt by the model with what is known at that moment.
  * Between milestones, a reforecast is published when a closed 5-minute candle sits the
    deviation threshold or more away from the day path in force. The threshold is one typical
    5-minute candle (average high-low range of the last 12 closed bars), held between 10 and 20
    points, so a normal candle does not trigger one and a real break does.

The original 09:15 rows are never changed and are the only thing scored at the close.
Reforecasts are kept with a receipt (hash of what was published) for review.

Driven from the hourly forecaster's loop (no thread or process of its own). Never places,
sizes or recommends an order. Stops keep following the hourly band, not this one.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from . import audit
from . import hourly_forecast as HF
from . import hourly_forecast_model as M
from .config import INDICES, IST

DAY_START = (9, 15)
DAY_END = (15, 30)
STEPS = M.BARS_PER_DAY                       # 75 five-minute points, 09:20 ... 15:30
MILESTONES = tuple(s for s, _ in M.SLOTS if s != DAY_START)   # 10:15 ... 15:15
DEV_MIN_PTS, DEV_MAX_PTS = 10.0, 20.0
ATR_BARS = 12
ON_TIME_SEC = 120
GIVE_UP_SEC = 900                            # score by 15:45 with what is recorded
MODEL_TAG = 'day-1.0'

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_forecasts (
    day TEXT NOT NULL, index_name TEXT NOT NULL,
    generated_at TEXT NOT NULL, late INTEGER NOT NULL DEFAULT 0,
    anchor REAL NOT NULL, sigma REAL NOT NULL, direction TEXT NOT NULL, prob_up REAL NOT NULL,
    projected_pct REAL NOT NULL, projected_pts REAL NOT NULL, band_pts REAL NOT NULL,
    model_version TEXT, inputs TEXT, status TEXT NOT NULL DEFAULT 'live',
    dir_hit INTEGER, mae_pts REAL, coverage REAL, end_error_pts REAL,
    actual_pct REAL, actual_end REAL, reasons TEXT, finalized_at TEXT,
    PRIMARY KEY (day, index_name)
);
CREATE TABLE IF NOT EXISTS day_forecast_points (
    day TEXT NOT NULL, index_name TEXT NOT NULL, k INTEGER NOT NULL, ts TEXT NOT NULL,
    forecast REAL NOT NULL, upper REAL NOT NULL, lower REAL NOT NULL, swing REAL,
    actual REAL, actual_source TEXT,
    PRIMARY KEY (day, index_name, k)
);
CREATE TABLE IF NOT EXISTS day_forecast_revisions (
    day TEXT NOT NULL, index_name TEXT NOT NULL, rev INTEGER NOT NULL,
    kind TEXT NOT NULL, trigger_k INTEGER NOT NULL, trigger_ts TEXT NOT NULL,
    trigger_actual REAL NOT NULL, deviation_pts REAL, threshold_pts REAL,
    direction TEXT, prob_up REAL, published_at TEXT NOT NULL, points TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY (day, index_name, rev)
);
"""


_ready = set()


def conn(db=None):
    c = HF.conn(db)
    if id(c) not in _ready:
        c.executescript(SCHEMA)
        _ready.add(id(c))
    return c


def t0_of(day):
    return datetime(day.year, day.month, day.day, DAY_START[0], DAY_START[1], tzinfo=IST)


def ts_of(day, k):
    return t0_of(day) + timedelta(minutes=M.STEP_MIN * k)


def k_of(hm):
    return M.bar_index(hm)


def threshold_pts(token, until, db=None):
    """One typical 5-minute candle (mean high-low of the last 12 closed bars), clamped to 10..20."""
    rows = HF.history_rows(token, until, days=5, db=db)[-ATR_BARS:]
    rng = [h - lo for _, _, h, lo, _ in rows if M.finite(h) and M.finite(lo) and h >= lo]
    atr = sum(rng) / len(rng) if rng else DEV_MIN_PTS
    return round(M.clip(atr, DEV_MIN_PTS, DEV_MAX_PTS), 1)


def revisions(day, name, db=None):
    return conn(db).execute('SELECT * FROM day_forecast_revisions WHERE day=? AND index_name=? ORDER BY rev',
                            (day, name)).fetchall()


def active_path(pts, revs):
    """{k: price} of the day path in force: the original, overridden by each revision from its trigger on."""
    out = {p['k']: p['forecast'] for p in pts}
    for v in revs:
        out.update({q['k']: q['forecast'] for q in json.loads(v['points'])})
    return out


def path_move(name, day, k_from, k_to, anchor, db=None):
    """Expected move (fraction) of the day path in force between two marks, or None.

    Used by the hourly forecaster as its 'day path' input. Reads only what is already stored.
    """
    c = conn(db)
    d = day.isoformat()
    pts = c.execute('SELECT k, forecast FROM day_forecast_points WHERE day=? AND index_name=?',
                    (d, name)).fetchall()
    if not pts:
        return None
    path = active_path(pts, revisions(d, name, db))
    a = path.get(k_from, anchor if k_from == 0 else None)
    for v in reversed(revisions(d, name, db)):    # a revision made at k_from starts from its anchor
        if v['trigger_k'] == k_from:
            a = v['trigger_actual']
            break
    b = path.get(k_to)
    if not (M.finite(a) and M.finite(b)) or a <= 0:
        return None
    return math.log(b / a)


class DayForecaster:
    """Owned by the HourlyForecaster; called once per loop for each forecast index."""

    def __init__(self, hourly):
        self.hourly = hourly
        self.db = hourly.db
        self._tod = {}

    # ------------------------------------------------------------ model
    def _model(self, name, token, day, k, anchor, *, atm_iv=None, shield_dir=None, shield_score=None, rows=None):
        t = ts_of(day, k)
        rows = rows if rows is not None else HF.history_rows(token, t)
        days = M.build_days(rows, fill_before=day)
        if not days or days[-1].day != day:
            days.append(M.Day(day, *[[None] * M.BARS_PER_DAY for _ in range(4)]))
        di = len(days) - 1
        if days[di].opens[0] is None and k == 0:
            days[di].opens[0] = anchor
        key = (name, day)
        if key not in self._tod:
            self._tod = {key: M.tod_profile(days[:di])}
        step = INDICES[name].strike_step if name in INDICES else 50
        try:
            oi = HF.oi_features(name, t, step)
        except Exception:                            # noqa: BLE001 - chain data is optional
            oi = None
        return M.make_forecast(days, di, k, STEPS - k, anchor, as_of=day, tod=self._tod[key],
                               params=HF.load_params(name, self.db), atm_iv=atm_iv,
                               shield_dir=shield_dir, shield_score=shield_score, oi=oi)

    # ------------------------------------------------------------ per loop
    def tick(self, name, st, now, token=None):
        token = token or (INDICES[name].token if name in INDICES else None)
        if token is None or now.weekday() >= 5:
            return
        day = now.date()
        t0 = t0_of(day)
        if now < t0:
            return
        c = conn(self.db)
        d = day.isoformat()
        row = c.execute('SELECT * FROM day_forecasts WHERE day=? AND index_name=?', (d, name)).fetchone()
        if row is None:
            if now >= ts_of(day, STEPS):
                return
            self.make_original(name, st, token, day, now)
            return
        if row['status'] != 'live':
            return
        self.update_actuals(name, token, d, now)
        pts = c.execute('SELECT * FROM day_forecast_points WHERE day=? AND index_name=? ORDER BY k',
                        (d, name)).fetchall()
        self.reforecast(name, st, token, day, pts, now)
        end_ts = ts_of(day, STEPS)
        if now >= end_ts:
            done = all(HF.verified(p) for p in pts)
            if done or (now - end_ts).total_seconds() >= GIVE_UP_SEC:
                self.finalize(row, pts, give_up=not done)

    def stop(self, name, now):
        """Engine shutdown: score with what is recorded."""
        c = conn(self.db)
        d = now.date().isoformat()
        row = c.execute("SELECT * FROM day_forecasts WHERE day=? AND index_name=? AND status='live'",
                        (d, name)).fetchone()
        if row and now >= ts_of(now.date(), STEPS):
            token = INDICES[name].token if name in INDICES else None
            if token:
                self.update_actuals(name, token, d, now)
            pts = c.execute('SELECT * FROM day_forecast_points WHERE day=? AND index_name=? ORDER BY k',
                            (d, name)).fetchall()
            self.finalize(row, pts, give_up=not all(HF.verified(p) for p in pts))

    def make_original(self, name, st, token, day, now, rows=None):
        t0 = t0_of(day)
        spot = iv = sd_ = ss = None
        if st is not None:
            spot, iv, sd_, ss = self.hourly._live_inputs(st, now)
        if spot and (now - t0).total_seconds() <= ON_TIME_SEC:
            anchor, late = spot, 0
        else:
            anchor, late = HF.candle_open(token, t0), 1
            iv = sd_ = ss = None
            if anchor is None:
                return None
        f = self._model(name, token, day, 0, anchor, atm_iv=iv, shield_dir=sd_, shield_score=ss, rows=rows)
        if f is None:
            audit.log('day_forecast_skipped', index=name, reason='insufficient_history_or_volatility')
            return None
        c = conn(self.db)
        d = day.isoformat()
        swing = f.swing if len(f.swing) == len(f.points) else [None] * len(f.points)
        with c:
            c.execute('INSERT OR IGNORE INTO day_forecasts (day,index_name,generated_at,late,anchor,sigma,direction,'
                      'prob_up,projected_pct,projected_pts,band_pts,model_version,inputs,status) '
                      'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (d, name, now.isoformat(), late, f.anchor, f.sigma, f.direction, f.prob_up,
                       f.projected_pct, f.projected_pts, f.band_pts, f'{MODEL_TAG}/{M.MODEL_VERSION}',
                       json.dumps(dict(sd=f.inputs.get('sd'), mean=f.inputs.get('mean'),
                                       swing_from=f.inputs.get('swing_from'), n_eff=f.n_eff), default=str),
                       'live'))
            c.executemany('INSERT OR IGNORE INTO day_forecast_points (day,index_name,k,ts,forecast,upper,lower,swing) '
                          'VALUES (?,?,?,?,?,?,?,?)',
                          [(d, name, k, ts_of(day, k).isoformat(), round(m, 2), round(u, 2), round(lo, 2), sw)
                           for (k, m, u, lo), sw in zip(f.points, swing)])
        audit.log('day_forecast_made', index=name, late=late, anchor=f.anchor, direction=f.direction,
                  projected_pts=f.projected_pts, band_pts=f.band_pts)
        return f

    def update_actuals(self, name, token, d, now):
        c = conn(self.db)
        for p in c.execute('SELECT * FROM day_forecast_points WHERE day=? AND index_name=? ORDER BY k',
                           (d, name)).fetchall():
            ts = HF._dt(p['ts'])
            if now < ts + timedelta(seconds=5):
                break
            if HF.verified(p):
                continue
            a, source = HF.actual_at(token, ts, now)
            if a is not None and (a, source) != (p['actual'], p['actual_source']):
                with c:
                    c.execute('UPDATE day_forecast_points SET actual=?, actual_source=? WHERE day=? AND '
                              'index_name=? AND k=?', (a, source, d, name, p['k']))

    def reforecast(self, name, st, token, day, pts, now, rows=None):
        """Publish at most one revision per loop (a restart mid-day catches up gradually).

        In order of time: an hourly milestone always reforecasts; otherwise two closed candles in a
        row, both the threshold or more from the path in force on the same side, do (27 Oct: one
        spike candle no longer redraws the day). Provisional prices never trigger one.
        """
        if not pts:
            return None
        d = day.isoformat()
        revs = revisions(d, name, self.db)
        done_k = {v['trigger_k'] for v in revs}
        last_k = max(done_k) if done_k else 0
        active = active_path(pts, revs)
        by_k = {p['k']: p for p in pts}
        mks = {k_of(m) for m in MILESTONES}
        prev_side = 0                                # +1 / -1 if the previous candle was beyond the limit
        for k in range(last_k + 1, STEPS):
            mark = ts_of(day, k)
            if now < mark:
                break
            p = by_k.get(k)
            if k in mks:
                anchor = None
                if st is not None and (now - mark).total_seconds() <= ON_TIME_SEC:
                    spot = self.hourly._live_inputs(st, now)[0]
                    anchor = spot
                if anchor is None and p is not None and HF.verified(p):
                    anchor = p['actual']
                if anchor is None:
                    return None              # wait for the milestone's price
                dev = round(anchor - active[k], 2) if k in active else None
                return self._publish(name, st, token, day, k, 'milestone', anchor, dev, None, now, revs, rows)
            if p is None or not HF.verified(p):
                return None                  # candles are judged in order; wait for this one
            thr = threshold_pts(token, mark, self.db)
            dev = round(p['actual'] - active[k], 2)
            side = (1 if dev > 0 else -1) if abs(dev) >= thr else 0
            if side and side == prev_side:
                return self._publish(name, st, token, day, k, 'deviation', p['actual'], dev, thr, now, revs, rows)
            prev_side = side
        return None

    def _publish(self, name, st, token, day, k, kind, anchor, dev, thr, now, revs, rows=None):
        mark = ts_of(day, k)
        iv = sd_ = ss = None
        if st is not None and (now - mark).total_seconds() <= ON_TIME_SEC:
            _, iv, sd_, ss = self.hourly._live_inputs(st, now)
        f = self._model(name, token, day, k, anchor, atm_iv=iv, shield_dir=sd_, shield_score=ss, rows=rows)
        if f is None:
            # Model could not run (thin data): carry the path in force through the new price instead.
            c = conn(self.db)
            pts = c.execute('SELECT * FROM day_forecast_points WHERE day=? AND index_name=? AND k>? ORDER BY k',
                            (day.isoformat(), name, k)).fetchall()
            act = active_path(c.execute('SELECT k, forecast FROM day_forecast_points WHERE day=? AND index_name=?',
                                        (day.isoformat(), name)).fetchall(), revs)
            shift = anchor - act.get(k, anchor)
            path = [dict(k=q['k'], ts=q['ts'], forecast=round(act[q['k']] + shift, 2),
                         upper=round(q['upper'] - q['forecast'] + act[q['k']] + shift, 2),
                         lower=round(q['lower'] - q['forecast'] + act[q['k']] + shift, 2)) for q in pts]
            direction, prob = None, None
        else:
            path = [dict(k=k + j, ts=ts_of(day, k + j).isoformat(), forecast=round(m, 2), upper=round(u, 2),
                         lower=round(lo, 2), swing=sw)
                    for (j, m, u, lo), sw in zip(f.points, f.swing if len(f.swing) == len(f.points)
                                                 else [None] * len(f.points))]
            direction, prob = f.direction, f.prob_up
        rev = (revs[-1]['rev'] if revs else 0) + 1
        rc = HF.receipt(day.isoformat(), name, 'day', rev, k, path)
        c = conn(self.db)
        with c:
            c.execute('INSERT OR IGNORE INTO day_forecast_revisions (day,index_name,rev,kind,trigger_k,trigger_ts,'
                      'trigger_actual,deviation_pts,threshold_pts,direction,prob_up,published_at,points,receipt) '
                      'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (day.isoformat(), name, rev, kind, k, mark.isoformat(), round(anchor, 2), dev, thr,
                       direction, prob, now.isoformat(), json.dumps(path), rc))
        audit.log('day_forecast_reforecast', index=name, rev=rev, kind=kind, trigger_ts=mark.isoformat(),
                  deviation_pts=dev, threshold_pts=thr, receipt=rc)
        return rev

    def finalize(self, row, pts, give_up=False):
        c = conn(self.db)
        if give_up and not (pts and HF.verified(pts[-1])):
            with c:
                c.execute("UPDATE day_forecasts SET status='incomplete', finalized_at=? WHERE day=? AND "
                          "index_name=? AND status='live'", (datetime.now(IST).isoformat(), row['day'], row['index_name']))
            audit.log('day_forecast_incomplete', index=row['index_name'], day=row['day'])
            return None
        use = [p for p in pts if HF.verified(p)]
        points = [(p['k'], p['forecast'], p['upper'], p['lower']) for p in use]
        actual = [p['actual'] for p in use]
        sc = M.score(points, row['anchor'], actual, row['direction'], row['sigma'], 1.0) or {}
        tags = M.deviation_reasons('09:15', None, sc.get('actual_pct'), sc.get('coverage'), row['direction'],
                                   sc.get('dir_hit'), bool(row['late']))
        n_rev = len(revisions(row['day'], row['index_name'], self.db))
        with c:
            c.execute("UPDATE day_forecasts SET status='final', dir_hit=?, mae_pts=?, coverage=?, end_error_pts=?, "
                      "actual_pct=?, actual_end=?, reasons=?, finalized_at=? WHERE day=? AND index_name=?",
                      (None if 'dir_hit' not in sc else int(sc['dir_hit']), sc.get('mae_pts'), sc.get('coverage'),
                       sc.get('end_error_pts'), sc.get('actual_pct'), actual[-1] if actual else None,
                       json.dumps(dict(tags=tags, revisions=n_rev, scored_points=len(use))),
                       datetime.now(IST).isoformat(), row['day'], row['index_name']))
        audit.log('day_forecast_scored', index=row['index_name'], day=row['day'], revisions=n_rev, **sc)
        return sc


# ---------------------------------------------------------------- read view
def view(name, day, now, live=None, db=None):
    """The day chart in the same shape as an hourly slot, so the panel draws it the same way."""
    c = conn(db)
    d = day.isoformat()
    t0, t1 = t0_of(day), ts_of(day, STEPS)
    base = dict(slot='Day', slot_end=M.slot_label(DAY_END), start_ts=t0.isoformat(), end_ts=t1.isoformat(),
                milestones=[ts_of(day, k_of(m)).isoformat() for m in MILESTONES],
                threshold=dict(min=DEV_MIN_PTS, max=DEV_MAX_PTS))
    r = c.execute('SELECT * FROM day_forecasts WHERE day=? AND index_name=?', (d, name)).fetchone()
    if r is None:
        base['status'] = 'pending' if now < t0 + timedelta(seconds=ON_TIME_SEC) and day == now.date() else 'missed'
        return base
    token = INDICES[name].token if name in INDICES else None
    points = [dict(p) for p in c.execute(
        'SELECT k, ts, forecast, upper, lower, swing, actual, actual_source FROM day_forecast_points '
        'WHERE day=? AND index_name=? ORDER BY k', (d, name))]
    if token:
        for p in points:
            p['bar'] = HF.candle_bar(token, HF._dt(p['ts']) - timedelta(minutes=M.STEP_MIN))
    revs = [dict(rev=v['rev'], kind=v['kind'], trigger_k=v['trigger_k'], trigger_ts=v['trigger_ts'],
                 trigger_actual=v['trigger_actual'], deviation_pts=v['deviation_pts'],
                 threshold_pts=v['threshold_pts'], direction=v['direction'], prob_up=v['prob_up'],
                 published_at=v['published_at'], receipt=v['receipt'], points=json.loads(v['points']))
            for v in revisions(d, name, db)]
    cur_thr = None
    if token and r['status'] == 'live' and day == now.date():
        try:
            cur_thr = threshold_pts(token, now, db)
        except Exception:                            # noqa: BLE001 - display only
            cur_thr = None
    try:
        inp = json.loads(r['inputs'] or '{}')
    except ValueError:
        inp = {}
    reasons = {}
    if r['reasons']:
        try:
            reasons = json.loads(r['reasons'])
        except ValueError:
            reasons = {}
    base.update(status=r['status'], generated_at=r['generated_at'], late=bool(r['late']), anchor=r['anchor'],
                direction=r['direction'], prob_up=r['prob_up'], projected_pct=r['projected_pct'],
                projected_pts=r['projected_pts'], band_pts=r['band_pts'], model_version=r['model_version'],
                points=points, revisions=revs, swing_from=inp.get('swing_from'),
                trigger_pts=cur_thr if cur_thr is not None else (revs[-1]['threshold_pts'] if revs and revs[-1]['threshold_pts'] else DEV_MIN_PTS),
                live=live if (r['status'] == 'live' and t0 <= now < t1 + timedelta(seconds=60)) else None,
                score=None if r['status'] != 'final' else dict(
                    dir_hit=None if r['dir_hit'] is None else bool(r['dir_hit']), mae_pts=r['mae_pts'],
                    coverage=r['coverage'], end_error_pts=r['end_error_pts'], actual_pct=r['actual_pct'],
                    reasons=[M.REASONS.get(t, t) for t in reasons.get('tags', [])]))
    return base
