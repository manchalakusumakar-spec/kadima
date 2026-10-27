"""Hourly path forecasts vs actuals for KADIMA Celestial. Display only.

One forecast per index per slot (09:15-10:15 ... 14:15-15:15, 15:15-15:30),
made at the slot start and then frozen. Actual prices are filled in every
5 minutes from the engine's own closed candles, so the comparison is honest
and every slot is kept in forecast.db for review and recalibration.

Runs as a daemon thread inside the engine process rather than a separate
service: inputs live in engine memory; a sibling forecast-only collector uses
idle capacity in the engine's existing shared quote budget,
and a second Python process costs far more RAM on this instance than a
thread does.

Never places, sizes or recommends an order. Manual trades from the
Celestial panel go through auto_executor.forecast_manual_buy(), which
applies every existing entry gate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from . import audit, storage
from . import hourly_forecast_model as M
from .config import DATA_DIR, IST, INDICES
from .utils import now_ist
from . import forecast_chain_data as FCD

FORECAST_INDICES = tuple(x for x in os.environ.get('KADIMA_FORECAST_INDICES', 'NIFTY').split(',') if x)
DB_PATH = os.path.join(DATA_DIR, 'forecast.db')
HISTORY_DAYS = 90
LOOP_SEC = 5
ON_TIME_SEC = 120            # generated within 2 min of slot start = on time
SPOT_FRESH_SEC = 30
# A tick can land while this loop is computing (the day forecast runs first), which makes the
# spot's timestamp slightly newer than the loop's clock. That is fresh, not stale (27 Oct fix).
SPOT_AHEAD_OK_SEC = 30
FINALIZE_GIVE_UP_SEC = 900   # 15 min after the slot: score it, or mark it incomplete
# 2026-10-20 scoring integrity: only a closed candle that starts exactly at the target bar
# may enter accuracy scoring. A provisional (unclosed) value is shown, never scored.
VERIFIED_SOURCES = ('closed_5m', 'closed_1m')
PROVISIONAL = 'provisional'
# A closed candle this many points or more from the path in force publishes a revised
# path (display only). The original rows stay as issued and remain the only thing scored.
REFORECAST_PTS = 5.0
CALIBRATION_DAYS = 120

FORECASTER = None            # set by Engine; read by control_api

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS hourly_forecasts (
    day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL,
    slot_end TEXT NOT NULL, steps INTEGER NOT NULL,
    generated_at TEXT NOT NULL, late INTEGER NOT NULL DEFAULT 0,
    anchor REAL NOT NULL, sigma REAL NOT NULL,
    direction TEXT NOT NULL, prob_up REAL NOT NULL,
    projected_pct REAL NOT NULL, projected_pts REAL NOT NULL,
    band_pts REAL NOT NULL, n_eff REAL, model_version TEXT, inputs TEXT,
    status TEXT NOT NULL DEFAULT 'live',
    dir_hit INTEGER, mae_pts REAL, coverage REAL, end_error_pts REAL,
    actual_pct REAL, actual_end REAL, finalized_at TEXT,
    PRIMARY KEY (day, index_name, slot)
);
CREATE TABLE IF NOT EXISTS hourly_forecast_points (
    day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL,
    k INTEGER NOT NULL, ts TEXT NOT NULL,
    forecast REAL NOT NULL, upper REAL NOT NULL, lower REAL NOT NULL,
    actual REAL,
    PRIMARY KEY (day, index_name, slot, k)
);
CREATE TABLE IF NOT EXISTS hourly_forecast_revisions (
    day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL, rev INTEGER NOT NULL,
    trigger_k INTEGER NOT NULL, trigger_ts TEXT NOT NULL, trigger_actual REAL NOT NULL,
    deviation_pts REAL NOT NULL, published_at TEXT NOT NULL, points TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY (day, index_name, slot, rev)
);
CREATE TABLE IF NOT EXISTS hourly_forecast_model (
    index_name TEXT PRIMARY KEY, params TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hourly_forecast_audit (
    day TEXT NOT NULL, index_name TEXT NOT NULL, slot TEXT NOT NULL,
    reasons TEXT NOT NULL, deviation_from TEXT, modification TEXT, decision TEXT,
    audited_at TEXT NOT NULL,
    PRIMARY KEY (day, index_name, slot)
);
CREATE TABLE IF NOT EXISTS hourly_forecast_model_history (
    ts TEXT NOT NULL, index_name TEXT NOT NULL, params TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS hourly_backtest_samples (
    index_name TEXT NOT NULL, day TEXT NOT NULL, slot TEXT NOT NULL, sample TEXT NOT NULL,
    PRIMARY KEY (index_name, day, slot)
);
CREATE TABLE IF NOT EXISTS history_candles (
    token TEXT NOT NULL, start_ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    PRIMARY KEY (token, start_ts)
);
"""
EVAL_SLOTS = 20             # most recent resolved slots held out to judge new weights
SCHEMA += FCD.SCHEMA

_local = threading.local()


def conn(path=None):
    path = path or DB_PATH
    cache = getattr(_local, 'conns', None)
    if cache is None:
        cache = _local.conns = {}
    if path not in cache:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        cols = {r['name'] for r in c.execute('PRAGMA table_info(hourly_forecast_points)')}
        if 'actual_source' not in cols:
            c.execute('ALTER TABLE hourly_forecast_points ADD COLUMN actual_source TEXT')
        if 'swing' not in cols:
            c.execute('ALTER TABLE hourly_forecast_points ADD COLUMN swing REAL')
        cache[path] = c
    return cache[path]


def _dt(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def _slot_dt(day, hm):
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=IST)


# ---------------------------------------------------------------- candles
def history_rows(token, until, days=HISTORY_DAYS, db=None):
    """Closed 5-minute candles strictly before `until`: the engine's own candles, plus the
    broker history backfilled into forecast.db for days the engine did not record."""
    since = (until - timedelta(days=days)).isoformat()
    merged = {}
    for r in conn(db).execute('SELECT start_ts, open, high, low, close FROM history_candles '
                              'WHERE token=? AND start_ts>=?', (token, since)):
        merged[r['start_ts']] = r
    for r in storage.conn().execute(
            'SELECT start_ts, open, high, low, close FROM candles WHERE token=? AND interval=5 '
            'AND closed=1 AND start_ts>=?', (token, since)):
        merged[r['start_ts']] = r
    out = []
    for ts in sorted(merged):
        r = merged[ts]
        start = _dt(ts)
        if start + timedelta(minutes=5) <= until:
            out.append((start, r['open'], r['high'], r['low'], r['close']))
    return out


# ---------------------------------------------------------------- option chain
def chain_at(index_name, ts):
    """Prefer a wide sample strictly before ts; retain the narrow-chain fallback."""
    try:
        wide = FCD.aggregate(FCD.wide_at(conn(), index_name, ts))
        if wide:
            return {k: wide[k] for k in ('pcr','ce_wall','pe_wall')}
        c = storage.conn()
        r = c.execute('SELECT ts FROM oi_snapshots WHERE index_name=? AND ts<? ORDER BY ts DESC LIMIT 1',
                      (index_name, ts.isoformat())).fetchone()
        if not r or (ts - _dt(r['ts'])).total_seconds() > 300:
            return None
        rows = c.execute('SELECT opt_type, strike, oi FROM oi_snapshots WHERE index_name=? AND ts=?',
                         (index_name, r['ts'])).fetchall()
    except sqlite3.Error:
        return None
    ce = [x for x in rows if x['opt_type'] == 'CE' and x['oi']]
    pe = [x for x in rows if x['opt_type'] == 'PE' and x['oi']]
    if not ce or not pe:
        return None
    sce, spe = sum(x['oi'] for x in ce), sum(x['oi'] for x in pe)
    return dict(pcr=spe / sce if sce else None, ce_wall=max(ce, key=lambda x: x['oi'])['strike'],
                pe_wall=max(pe, key=lambda x: x['oi'])['strike'])


def oi_features(index_name, t0, step):
    """PCR change over the last 30 min and OI wall moves over the last 60 min, known at t0."""
    now, p30, p60 = (chain_at(index_name, t0), chain_at(index_name, t0 - timedelta(minutes=30)),
                     chain_at(index_name, t0 - timedelta(minutes=60)))
    extras = FCD.extra_features(conn(), index_name, t0, step)
    if not now and not extras:
        return None
    now = now or {}
    wide = FCD.wide_at(conn(), index_name, t0)
    return dict(pcr_now=now.get('pcr'), pcr_prev=(p30 or {}).get('pcr'),
                ce_wall_now=now.get('ce_wall'), ce_wall_prev=(p60 or {}).get('ce_wall'),
                pe_wall_now=now.get('pe_wall'), pe_wall_prev=(p60 or {}).get('pe_wall'), step=step,
                **extras, **({'spot':wide[0]['spot']} if wide else {}))


def candle_close(token, start, interval, closed_only=True):
    sql = 'SELECT close FROM candles WHERE token=? AND interval=? AND start_ts=?'
    if closed_only:
        sql += ' AND closed=1'
    r = storage.conn().execute(sql, (token, interval, start.isoformat())).fetchone()
    return r['close'] if r and M.finite(r['close']) else None


def candle_bar(token, start):
    """The 5-minute bar starting at `start` for the panel's candles (display only)."""
    r = storage.conn().execute(
        'SELECT open, high, low, close, closed FROM candles WHERE token=? AND interval=5 AND start_ts=?',
        (token, start.isoformat())).fetchone()
    if not r or not all(M.finite(r[k]) for k in ('open', 'high', 'low', 'close')):
        return None
    return dict(open=r['open'], high=r['high'], low=r['low'], close=r['close'], closed=bool(r['closed']))


def candle_open(token, start):
    for interval in (1, 3, 5):
        r = storage.conn().execute(
            'SELECT open FROM candles WHERE token=? AND interval=? AND start_ts=?',
            (token, interval, start.isoformat())).fetchone()
        if r and M.finite(r['open']):
            return r['open']
    return None


def actual_at(token, ts, now):
    """(price, source) at a 5-minute mark: close of the bar ending exactly there.

    source is 'closed_5m' / 'closed_1m' for a closed candle starting exactly at the
    target bar (scorable), or PROVISIONAL for the last stored price of a bar that never
    closed (display only: a bar closes on the next tick, which may never come).
    """
    a = candle_close(token, ts - timedelta(minutes=5), 5)
    if a is not None:
        return a, 'closed_5m'
    a = candle_close(token, ts - timedelta(minutes=1), 1)
    if a is not None:
        return a, 'closed_1m'
    if now >= ts + timedelta(seconds=60):
        a = (candle_close(token, ts - timedelta(minutes=5), 5, closed_only=False)
             or candle_close(token, ts - timedelta(minutes=1), 1, closed_only=False))
        if a is not None:
            return a, PROVISIONAL
    return None, None


def verified(p):
    return p['actual'] is not None and p['actual_source'] in VERIFIED_SOURCES


def _swing(p):
    try:
        return p['swing']
    except (IndexError, KeyError):
        return None


def revisions(day, name, slot, db=None):
    return conn(db).execute('SELECT * FROM hourly_forecast_revisions WHERE day=? AND index_name=? AND slot=? '
                            'ORDER BY rev', (day, name, slot)).fetchall()


def receipt(day, name, slot, rev, trigger_k, path):
    """Short hash of exactly what was published, so a revision can be checked later."""
    blob = json.dumps([day, name, slot, rev, trigger_k, path], separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- params
def load_params(index_name, db=None):
    """Current coefficients; `<INDEX>:prior` holds backtest-tuned starting values."""
    r = conn(db).execute('SELECT params FROM hourly_forecast_model WHERE index_name=?',
                         (index_name,)).fetchone()
    params = dict(M.DEFAULT_PARAMS)
    if r:
        try:
            saved = json.loads(r['params'])
            if saved.get('version') == M.MODEL_VERSION:
                params.update({k: v for k, v in saved.items() if k in params})
        except ValueError:
            pass
    params['prob_c'] = max(params['prob_c'], M.PROB_C_MIN)
    return params


def sample_from(r, pts):
    """Turn a resolved forecast row and its points into a calibration sample."""
    try:
        inp = json.loads(r['inputs'])
    except (TypeError, ValueError):
        return None
    sigma, anchor = r['sigma'], r['anchor']
    zs = []
    for p in pts:
        k = p['k'] - 1
        if p['actual'] is None or k >= len(inp.get('sd', [])) or inp['sd'][k] <= 0:
            continue
        zs.append(abs(math.log(p['actual'] / anchor) / sigma - inp['mean'][k]) / inp['sd'][k])
    out = {k: inp.get(k) for k in M.COMPONENTS}
    out.update(day=r['day'], slot=r['slot'], p_raw=inp.get('p_raw'),
               sd_end=inp.get('sd_end') or (inp.get('sd') or [None])[-1],
               y=math.log(r['actual_end'] / anchor) / sigma, up=r['actual_end'] > anchor, zs=zs)
    return out


def samples_for(index_name, db=None, days=CALIBRATION_DAYS):
    """Resolved on-time live slots, oldest first."""
    c = conn(db)
    since = (now_ist().date() - timedelta(days=days)).isoformat()
    out = []
    for r in c.execute("SELECT * FROM hourly_forecasts WHERE index_name=? AND status='final' "
                       "AND late=0 AND actual_end IS NOT NULL AND day>=? ORDER BY day, slot",
                       (index_name, since)):
        pts = c.execute('SELECT k, actual FROM hourly_forecast_points WHERE day=? AND index_name=? '
                        'AND slot=? ORDER BY k', (r['day'], index_name, r['slot'])).fetchall()
        smp = sample_from(r, pts)
        if smp:
            out.append(smp)
    return out


def backtest_samples(index_name, db=None, days=CALIBRATION_DAYS):
    since = (now_ist().date() - timedelta(days=days)).isoformat()
    return [json.loads(r['sample']) for r in conn(db).execute(
        'SELECT sample FROM hourly_backtest_samples WHERE index_name=? AND day>=? ORDER BY day, slot',
        (index_name, since))]


def save_params(index_name, params, reason, db=None):
    c = conn(db)
    with c:
        c.execute('INSERT OR REPLACE INTO hourly_forecast_model VALUES (?,?,?)',
                  (index_name, json.dumps(params), now_ist().isoformat()))
        c.execute('INSERT INTO hourly_forecast_model_history VALUES (?,?,?,?)',
                  (now_ist().isoformat(), index_name, json.dumps(params), reason))


def describe_change(old, new):
    names = dict(beta_reg='pattern', beta_analog='similar-days', beta_tech='technicals',
                 beta_shield='Bull Shield', beta_trend='trend-day', beta_oi='option chain', beta_day='day path',
                 band_z='band width', prob_c='confidence')
    parts = [f"{label} {old.get(k, 0):.2f}\u2192{new.get(k, 0):.2f}" for k, label in names.items()
             if abs((new.get(k) or 0) - (old.get(k) or 0)) >= 0.005]
    if bool(old.get('band_only')) != bool(new.get('band_only')):
        parts.append('direction hidden (range only)' if new.get('band_only') else 'direction shown again')
    return ', '.join(parts)


def recalibrate_index(index_name, db=None, eval_slots=None):
    """Champion/challenger on all resolved history; returns (params, decision, change text)."""
    live = samples_for(index_name, db)
    seen = {(x['day'], x['slot']) for x in live}
    hist = [x for x in backtest_samples(index_name, db) if (x['day'], x['slot']) not in seen]
    allx = sorted(hist + live, key=lambda x: (x['day'], x['slot']))
    k = eval_slots or EVAL_SLOTS
    holdout, train = allx[-k:], allx[:-k]
    champion = load_params(index_name, db)
    prior = load_params(index_name + ':prior', db)
    params, decision = M.recalibrate(champion, train, holdout, prior=prior)
    change = describe_change(champion, params)
    decision['scorecard'] = M.component_scorecard(allx)
    if change:
        save_params(index_name, params, decision['reason'] + ' | ' + change, db)
    return params, decision, change


def refit(index_name, db=None):
    """Kept for compatibility: one champion/challenger pass."""
    return recalibrate_index(index_name, db)[0]


# ---------------------------------------------------------------- forecaster
class HourlyForecaster:
    def __init__(self, engine, db=None):
        self.engine = engine
        self.db = db
        self._stop = threading.Event()
        self._tod_cache = {}
        self._live_snap = {}
        self._chain_collector = FCD.Collector(engine, FORECAST_INDICES, db)
        from . import day_forecast                       # day path (23 Oct), driven from this loop
        self.day = day_forecast.DayForecaster(self)

    def start(self):
        self._chain_collector.start()
        threading.Thread(target=self.run, daemon=True, name='hourly-forecast').start()
        audit.log('hourly_forecast_started', indices=list(FORECAST_INDICES))

    def stop(self):
        """Engine shutdown (15:40): score the 15:15 slot with what has been recorded."""
        self._stop.set()
        self._chain_collector.stop()
        try:
            now = now_ist()
            for name in FORECAST_INDICES:
                self.update_actuals(name, now, give_up=True)
                try:
                    self.day.stop(name, now)
                except Exception as exc:                # noqa: BLE001 - display only
                    audit.log('day_forecast_error', error=str(exc)[:300])
        except Exception as exc:                        # noqa: BLE001
            audit.log('hourly_forecast_stop_error', error=str(exc)[:300])

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick(now_ist())
            except Exception as exc:                    # noqa: BLE001 - display-only thread
                audit.log('hourly_forecast_error', error_type=type(exc).__name__, error=str(exc)[:300])
            self._stop.wait(LOOP_SEC)

    # -------------------------------------------------------- per tick
    def tick(self, now):
        if now.weekday() >= 5:
            return
        slot = M.slot_for(now.time())
        for name in FORECAST_INDICES:
            st = self.engine.states.get(name) if self.engine else None
            if st is None:
                continue
            self._live_inputs(st, now)                  # one live read per loop, shared by day and hourly
            try:                                        # day path first: a milestone reforecast feeds the new hour
                self.day.tick(name, st, now)
            except Exception as exc:                    # noqa: BLE001 - display only, never block the hourly slots
                audit.log('day_forecast_error', error_type=type(exc).__name__, error=str(exc)[:300])
            if slot:
                self.ensure_forecast(name, st, slot, now)
            self.update_actuals(name, now)
            if slot and name == 'NIFTY':
                from . import nifty_breadth             # shadow record only; never alters a forecast
                nifty_breadth.observe(conn(self.db), name, M.slot_label(slot[0]), now)

    def _live_inputs(self, st, now):
        """Spot (if fresh), ATM IV and Bull Shield from engine memory. No broker calls.

        Read once per loop and reused (27 Oct fix): the day forecast and the hourly slot made in
        the same loop see exactly the same live inputs, however long the day forecast takes.
        """
        key = (id(st), now)
        hit = self._live_snap.get(key)
        if hit is not None:
            return hit
        out = self._read_live_inputs(st, now)
        self._live_snap = {key: out}
        return out

    def _read_live_inputs(self, st, now):
        spot = None
        if (st.is_live_tick and st.last_updated and M.finite(st.spot)
                and -SPOT_AHEAD_OK_SEC <= (now - st.last_updated).total_seconds() <= SPOT_FRESH_SEC):
            spot = st.spot
        iv = None
        chain = st.chain
        if spot and chain and chain.quotes and 0 <= time.monotonic() - getattr(chain, '_last_poll', 0) <= 60:
            try:
                from .greeks import implied_vol_and_greeks
                days = self.engine._dte(st) if hasattr(self.engine, '_dte') else 1.0
                step = chain.index.strike_step
                atm = round(spot / step) * step
                ivs = []
                for q in chain.quotes.values():
                    if q.strike == atm and q.ltp:
                        v, _ = implied_vol_and_greeks(spot, q.strike, q.ltp, days, q.opt_type)
                        if M.finite(v):
                            ivs.append(v)
                iv = sum(ivs) / len(ivs) if ivs else None
            except Exception:                           # noqa: BLE001
                iv = None
        shield_dir = st.direction if st.direction in ('CE', 'PE') else None
        shield_score = st.score if shield_dir else None
        return spot, iv, shield_dir, shield_score

    def ensure_forecast(self, name, st, slot, now):
        start, end = slot
        day = now.date()
        label = M.slot_label(start)
        c = conn(self.db)
        if c.execute('SELECT 1 FROM hourly_forecasts WHERE day=? AND index_name=? AND slot=?',
                     (day.isoformat(), name, label)).fetchone():
            return None
        t0 = _slot_dt(day, start)
        token = INDICES[name].token if name in INDICES else st.chain.index.token
        spot, iv, shield_dir, shield_score = self._live_inputs(st, now)
        on_time = (now - t0).total_seconds() <= ON_TIME_SEC
        if on_time and spot:
            anchor, late = spot, 0
        else:
            # No fresh live price at the slot start (engine started mid-slot or feed stale): rebuild
            # from the slot's opening price and data before it; calibration skips late slots.
            anchor, late = candle_open(token, t0), 1
            iv, shield_dir, shield_score = None, None, None
            if anchor is None:
                return None
        return self.generate(name, token, day, slot, anchor, late, now,
                             atm_iv=iv, shield_dir=shield_dir, shield_score=shield_score,
                             day_move=None if late else self._day_move(name, day, slot, anchor))

    def _day_move(self, name, day, slot, anchor):
        """Expected move of the day path in force over this slot (None if there is no day path)."""
        try:
            from . import day_forecast
            return day_forecast.path_move(name, day, M.bar_index(slot[0]), M.bar_index(slot[1]), anchor, self.db)
        except Exception:                               # noqa: BLE001 - optional input
            return None

    def generate(self, name, token, day, slot, anchor, late, now, *, atm_iv=None,
                 shield_dir=None, shield_score=None, rows=None, day_move=None):
        start, end = slot
        t0 = _slot_dt(day, start)
        rows = rows if rows is not None else history_rows(token, t0)
        days = M.build_days(rows, fill_before=day)
        if not days or days[-1].day != day:
            days.append(M.Day(day, *[[None] * M.BARS_PER_DAY for _ in range(4)]))
        di = len(days) - 1
        i, n = M.bar_index(start), M.slot_steps(start, end)
        if days[di].opens[0] is None and i == 0:
            days[di].opens[0] = anchor
        key = (name, day)
        if key not in self._tod_cache:
            self._tod_cache = {key: M.tod_profile(days[:di])}
        params = load_params(name, self.db)
        step = INDICES[name].strike_step if name in INDICES else 50
        oi = oi_features(name, t0, step)
        f = M.make_forecast(days, di, i, n, anchor, as_of=day, tod=self._tod_cache[key],
                            params=params, atm_iv=atm_iv, shield_dir=shield_dir,
                            shield_score=shield_score, oi=oi, day_move=day_move)
        if f is None:
            audit.log('hourly_forecast_skipped', index=name, slot=M.slot_label(start),
                      reason='insufficient_history_or_volatility')
            return None
        label = M.slot_label(start)
        c = conn(self.db)
        with c:
            c.execute('INSERT OR IGNORE INTO hourly_forecasts (day,index_name,slot,slot_end,steps,'
                      'generated_at,late,anchor,sigma,direction,prob_up,projected_pct,projected_pts,'
                      'band_pts,n_eff,model_version,inputs,status) VALUES '
                      '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (day.isoformat(), name, label, M.slot_label(end), n, now.isoformat(), late,
                       f.anchor, f.sigma, f.direction, f.prob_up, f.projected_pct, f.projected_pts,
                       f.band_pts, f.n_eff, M.MODEL_VERSION, json.dumps(f.inputs, default=str), 'live'))
            # Named columns: the table gains columns over time (actual_source, swing).
            swing = f.swing if len(f.swing) == len(f.points) else [None] * len(f.points)
            c.executemany('INSERT OR IGNORE INTO hourly_forecast_points (day,index_name,slot,k,ts,forecast,'
                          'upper,lower,swing) VALUES (?,?,?,?,?,?,?,?,?)',
                          [(day.isoformat(), name, label, k,
                            (t0 + timedelta(minutes=M.STEP_MIN * k)).isoformat(),
                            round(mean, 2), round(up, 2), round(lo, 2), sw)
                           for (k, mean, up, lo), sw in zip(f.points, swing)])
        audit.log('hourly_forecast_made', index=name, slot=label, late=late, anchor=f.anchor,
                  direction=f.direction, prob_up=f.prob_up, projected_pct=f.projected_pct,
                  band_pts=f.band_pts, n_eff=f.n_eff)
        return f

    def update_actuals(self, name, now, token=None, give_up=False):
        c = conn(self.db)
        token = token or (INDICES[name].token if name in INDICES else None)
        if token is None:
            return
        for r in c.execute("SELECT * FROM hourly_forecasts WHERE index_name=? AND status='live'",
                           (name,)).fetchall():
            pts = c.execute('SELECT * FROM hourly_forecast_points WHERE day=? AND index_name=? AND slot=? '
                            'ORDER BY k', (r['day'], name, r['slot'])).fetchall()
            changed = False
            for p in pts:
                ts = _dt(p['ts'])
                if not verified(p) and now >= ts + timedelta(seconds=5):
                    a, source = actual_at(token, ts, now)
                    if a is not None and (a, source) != (p['actual'], p['actual_source']):
                        with c:
                            c.execute('UPDATE hourly_forecast_points SET actual=?, actual_source=? '
                                      'WHERE day=? AND index_name=? AND slot=? AND k=?',
                                      (a, source, r['day'], name, r['slot'], p['k']))
                        changed = True
            end_ts = _dt(pts[-1]['ts']) if pts else None
            if end_ts is None:
                continue
            if changed:
                pts = c.execute('SELECT * FROM hourly_forecast_points WHERE day=? AND index_name=? AND slot=? '
                                'ORDER BY k', (r['day'], name, r['slot'])).fetchall()
            try:
                self.reforecast(r, pts, now)
            except Exception as exc:                    # noqa: BLE001 - display only, never block scoring
                audit.log('hourly_forecast_reforecast_error', error=str(exc)[:200])
            done = all(verified(p) for p in pts)
            age = (now - end_ts).total_seconds()
            if done and age >= 0:
                self.finalize(r, pts)
            elif age >= FINALIZE_GIVE_UP_SEC or (give_up and age >= 0):
                self.mark_incomplete(r, pts)

    def reforecast(self, r, pts, now):
        """Publish a revised path each time a closed candle sits REFORECAST_PTS or more from the
        path in force (the original, then the latest revision). The revision is the original path
        shifted to pass through that close. Provisional prices never trigger one. Display only:
        the original rows are untouched and revisions are never scored or used for tuning."""
        if not pts:
            return []
        day, name, slot = r['day'], r['index_name'], r['slot']
        active = {p['k']: p['forecast'] for p in pts}
        last_k, rev = 0, 0
        for v in revisions(day, name, slot, self.db):
            active.update({q['k']: q['forecast'] for q in json.loads(v['points'])})
            last_k, rev = v['trigger_k'], v['rev']
        end_k, made = pts[-1]['k'], []
        c = conn(self.db)
        for p in pts:
            if p['k'] <= last_k or p['k'] >= end_k or not verified(p):
                continue
            dev = round(p['actual'] - active[p['k']], 2)
            if abs(dev) < REFORECAST_PTS:
                continue
            shift = p['actual'] - p['forecast']
            path = [dict(k=q['k'], ts=q['ts'], forecast=round(q['forecast'] + shift, 2),
                         upper=round(q['upper'] + shift, 2), lower=round(q['lower'] + shift, 2),
                         **({'swing': round(q['swing'] + shift, 2)} if _swing(q) is not None else {}))
                    for q in pts if q['k'] > p['k']]
            rev += 1
            rc = receipt(day, name, slot, rev, p['k'], path)
            with c:
                c.execute('INSERT OR IGNORE INTO hourly_forecast_revisions (day,index_name,slot,rev,trigger_k,'
                          'trigger_ts,trigger_actual,deviation_pts,published_at,points,receipt) '
                          'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                          (day, name, slot, rev, p['k'], p['ts'], p['actual'], dev, now.isoformat(),
                           json.dumps(path), rc))
            audit.log('hourly_forecast_reforecast', index=name, day=day, slot=slot, rev=rev,
                      trigger_ts=p['ts'], deviation_pts=dev, receipt=rc)
            active.update({q['k']: q['forecast'] for q in path})
            last_k = p['k']
            made.append(rev)
        return made

    def mark_incomplete(self, r, pts):
        """Give-up with a required actual missing or unverified: keep the slot unscored and out
        of calibration. The original forecast rows are left exactly as issued."""
        c = conn(self.db)
        missing = [p['k'] for p in pts if not verified(p)]
        with c:
            c.execute("UPDATE hourly_forecasts SET status='incomplete', finalized_at=? WHERE day=? AND "
                      "index_name=? AND slot=? AND status='live'",
                      (now_ist().isoformat(), r['day'], r['index_name'], r['slot']))
        audit.log('hourly_forecast_incomplete', index=r['index_name'], day=r['day'], slot=r['slot'],
                  unverified_k=missing)

    def finalize(self, r, pts):
        if not pts or not all(verified(p) for p in pts):
            return self.mark_incomplete(r, pts)
        c = conn(self.db)
        points = [(p['k'], p['forecast'], p['upper'], p['lower']) for p in pts]
        actual = [p['actual'] for p in pts]
        try:
            sd_end = json.loads(r['inputs'])['sd'][-1]
        except (TypeError, ValueError, KeyError, IndexError):
            sd_end = math.sqrt(r['steps'])
        sc = M.score(points, r['anchor'], actual, r['direction'], r['sigma'], sd_end) or {}
        with c:
            c.execute("UPDATE hourly_forecasts SET status='final', dir_hit=?, mae_pts=?, coverage=?, "
                      "end_error_pts=?, actual_pct=?, actual_end=?, finalized_at=? WHERE day=? AND "
                      "index_name=? AND slot=?",
                      (None if 'dir_hit' not in sc else int(sc['dir_hit']), sc.get('mae_pts'),
                       sc.get('coverage'), sc.get('end_error_pts'), sc.get('actual_pct'),
                       actual[-1] if actual else None, now_ist().isoformat(),
                       r['day'], r['index_name'], r['slot']))
        audit.log('hourly_forecast_scored', index=r['index_name'], day=r['day'], slot=r['slot'], **sc)
        # Self-audit after every slot: why it deviated, then a guarded recalibration.
        # 27 Oct: weights are no longer changed after a single slot mid-session. The slot is stored
        # and joins the guarded retune that runs once a day after the close (tools/forecast_nightly.py).
        modification, decision = None, None
        if sc and not r['late']:
            modification = 'No change now: this hour joins the daily retune after the close'
        elif r['late']:
            modification = 'No change: late slot is not used for tuning'
        try:
            audit_slot(r, pts, sc, self.db, modification, decision)
        except Exception as exc:                        # noqa: BLE001
            audit.log('hourly_forecast_audit_error', error=str(exc)[:200])


# ---------------------------------------------------------------- self-audit
def _day_open_prev_close(token, day):
    c = storage.conn()
    o = c.execute("SELECT open FROM candles WHERE token=? AND interval=5 AND substr(start_ts,1,10)=? "
                  "ORDER BY start_ts LIMIT 1", (token, day)).fetchone()
    pc = c.execute("SELECT close FROM candles WHERE token=? AND interval=5 AND closed=1 AND "
                   "substr(start_ts,1,10)<? ORDER BY start_ts DESC LIMIT 1", (token, day)).fetchone()
    return (o['open'] if o else None), (pc['close'] if pc else None)


def deviation_start(pts):
    """First 5-minute mark where the actual left the band, else where it drifted past half the band."""
    for p in pts:
        if p['actual'] is not None and not p['lower'] <= p['actual'] <= p['upper']:
            return p['ts'], 'left the band'
    for p in pts:
        if p['actual'] is not None and abs(p['actual'] - p['forecast']) > (p['upper'] - p['lower']) / 4:
            return p['ts'], 'drifted from the forecast'
    return None, None


def audit_slot(r, pts, sc, db=None, modification=None, decision=None):
    """Store the reasons for this slot's deviation and the change made in response."""
    name, day = r['index_name'], r['day']
    token = INDICES[name].token if name in INDICES else None
    day_open, prev = _day_open_prev_close(token, day) if token else (None, None)
    anchor = r['anchor']
    day_pct = (anchor / day_open - 1) * 100 if day_open else None
    gap_pct = (day_open / prev - 1) * 100 if day_open and prev else None
    mid = pts[len(pts) // 2 - 1]['actual'] if len(pts) >= 2 else None
    first_half = (mid / anchor - 1) * 100 if mid else None
    t0 = _dt(pts[0]['ts']) - timedelta(minutes=M.STEP_MIN) if pts else None
    t1 = _dt(pts[-1]['ts']) if pts else None
    a, b = (chain_at(name, t0), chain_at(name, t1)) if t0 else (None, None)
    oi_move = None
    if a and b:
        oi_move, _ = M.oi_tilt(dict(pcr_now=b['pcr'], pcr_prev=a['pcr'], ce_wall_now=b['ce_wall'],
                                    ce_wall_prev=a['ce_wall'], pe_wall_now=b['pe_wall'],
                                    pe_wall_prev=a['pe_wall'], step=INDICES[name].strike_step if name in INDICES else 50))
    hit = sc.get('dir_hit') if sc else None
    tags = M.deviation_reasons(r['slot'], day_pct, sc.get('actual_pct') if sc else None,
                               sc.get('coverage') if sc else None, r['direction'], hit, bool(r['late']),
                               oi_move=oi_move, first_half_pct=first_half, gap_pct=gap_pct)
    details = {}
    if not hit:
        c = conn(db)
        today = []
        for row in c.execute('SELECT inputs FROM hourly_forecasts WHERE day=? AND index_name=? AND late=0',
                             (day, name)):
            try:
                today.append(json.loads(row['inputs']))
            except (TypeError, ValueError):
                pass
        biased = M.day_component_bias(today)
        if biased:
            tags.append('component_bias')
            details['biased_parts'] = biased
    if a and b:
        details['oi'] = dict(pcr=[a['pcr'], b['pcr']], ce_wall=[a['ce_wall'], b['ce_wall']],
                             pe_wall=[a['pe_wall'], b['pe_wall']])
    details.update(day_pct=day_pct, gap_pct=gap_pct)
    dev_ts, dev_kind = deviation_start(pts)
    if dev_ts:
        details['deviation_kind'] = dev_kind
    if not tags and not hit and sc:
        tags.append('unexplained')
    c = conn(db)
    with c:
        c.execute('INSERT OR REPLACE INTO hourly_forecast_audit VALUES (?,?,?,?,?,?,?,?)',
                  (day, name, r['slot'], json.dumps(dict(tags=tags, details=details), default=str),
                   dev_ts, modification, json.dumps(decision, default=str) if decision else None,
                   now_ist().isoformat()))
    audit.log('hourly_forecast_audited', index=name, day=day, slot=r['slot'], tags=tags,
              deviation_from=dev_ts, modification=modification)
    return tags


def reason_text(tag, details):
    if tag == 'component_bias':
        return M.REASONS[tag] + ': ' + ', '.join(details.get('biased_parts', []))
    if tag == 'oi_shift_missed' and details.get('oi'):
        o = details['oi']
        return (M.REASONS[tag] + f": PCR {o['pcr'][0]:.2f}\u2192{o['pcr'][1]:.2f}, CE wall "
                f"{o['ce_wall'][0]:.0f}\u2192{o['ce_wall'][1]:.0f}, PE wall {o['pe_wall'][0]:.0f}\u2192{o['pe_wall'][1]:.0f}")
    if tag == 'unexplained':
        return 'No rule matched: likely ordinary noise inside the hour'
    return M.REASONS.get(tag, tag)


# ---------------------------------------------------------------- read view
def _scorecard(rows):
    scored = [r for r in rows if r['dir_hit'] is not None]
    if not scored:
        return dict(n=0)
    n = len(scored)
    return dict(n=n, dir_hits=sum(r['dir_hit'] for r in scored),
                dir_hit_rate=round(sum(r['dir_hit'] for r in scored) / n, 3),
                coverage=round(sum(r['coverage'] or 0 for r in scored) / n, 3),
                mae_pts=round(sum(r['mae_pts'] or 0 for r in scored) / n, 2))


def view(name, day=None, engine=None, now=None, db=None):
    """Everything the Celestial panel draws, as plain JSON."""
    now = now or now_ist()
    day = day or now.date()
    c = conn(db)
    rows = {r['slot']: r for r in c.execute(
        'SELECT * FROM hourly_forecasts WHERE day=? AND index_name=?', (day.isoformat(), name))}
    st = engine.states.get(name) if engine else None
    live_price = None
    if (st is not None and day == now.date() and st.is_live_tick and st.last_updated
            and M.finite(st.spot) and 0 <= (now - st.last_updated).total_seconds() <= SPOT_FRESH_SEC):
        live_price = dict(ts=st.last_updated.isoformat(), price=st.spot)
    token = INDICES[name].token if name in INDICES else None
    slots = []
    for start, end in M.SLOTS:
        label, t0, t1 = M.slot_label(start), _slot_dt(day, start), _slot_dt(day, end)
        r = rows.get(label)
        base = dict(slot=label, slot_end=M.slot_label(end), start_ts=t0.isoformat(), end_ts=t1.isoformat())
        if r is None:
            base['status'] = 'pending' if now < t0 + timedelta(seconds=ON_TIME_SEC) and day == now.date() else 'missed'
            slots.append(base)
            continue
        pts = c.execute('SELECT k, ts, forecast, upper, lower, swing, actual, actual_source FROM hourly_forecast_points '
                        'WHERE day=? AND index_name=? AND slot=? ORDER BY k',
                        (day.isoformat(), name, label)).fetchall()
        p_up = r['prob_up']
        points = [dict(p) for p in pts]
        if token:
            for p in points:
                p['bar'] = candle_bar(token, _dt(p['ts']) - timedelta(minutes=M.STEP_MIN))
        base.update(
            status=r['status'], generated_at=r['generated_at'], late=bool(r['late']),
            anchor=r['anchor'], direction=r['direction'], prob_up=p_up,
            prob_direction=round(max(p_up, 1 - p_up), 4),
            projected_pct=r['projected_pct'], projected_pts=r['projected_pts'], band_pts=r['band_pts'],
            analog_matches=r['n_eff'], model_version=r['model_version'],
            points=points,
            revisions=[dict(rev=v['rev'], trigger_k=v['trigger_k'], trigger_ts=v['trigger_ts'],
                            trigger_actual=v['trigger_actual'], deviation_pts=v['deviation_pts'],
                            published_at=v['published_at'], receipt=v['receipt'],
                            points=json.loads(v['points']))
                       for v in revisions(day.isoformat(), name, label, db)],
            score=None if r['status'] != 'final' else dict(
                dir_hit=None if r['dir_hit'] is None else bool(r['dir_hit']), mae_pts=r['mae_pts'],
                coverage=r['coverage'], end_error_pts=r['end_error_pts'], actual_pct=r['actual_pct']),
            live=live_price if (r['status'] == 'live' and t0 <= now < t1 + timedelta(seconds=60)) else None)
        inp = json.loads(r['inputs'] or '{}')
        base['inputs'] = {'oi_features': inp.get('oi_features', {})}
        base['swing_from'] = inp.get('swing_from')
        a = c.execute('SELECT * FROM hourly_forecast_audit WHERE day=? AND index_name=? AND slot=?',
                      (day.isoformat(), name, label)).fetchone()
        if a:
            try:
                info = json.loads(a['reasons'])
            except (TypeError, ValueError):
                info = dict(tags=[], details={})
            base['audit'] = dict(reasons=[reason_text(t, info.get('details') or {}) for t in info.get('tags', [])],
                                 deviation_from=a['deviation_from'],
                                 deviation_kind=(info.get('details') or {}).get('deviation_kind'),
                                 modification=a['modification'])
        slots.append(base)
    since = (day - timedelta(days=30)).isoformat()
    recent = c.execute("SELECT dir_hit, coverage, mae_pts FROM hourly_forecasts WHERE index_name=? "
                       "AND status='final' AND late=0 AND day>=? AND day<=?",
                       (name, since, day.isoformat())).fetchall()
    params = load_params(name, db)
    try:
        from . import day_forecast
        day_view = day_forecast.view(name, day, now, live=live_price, db=db)
    except Exception as exc:                            # noqa: BLE001 - the hourly slots must still render
        day_view = dict(slot='Day', status='error', error=type(exc).__name__)
    return dict(index=name, day=day.isoformat(), now=now.isoformat(), live=live_price, slots=slots, day_path=day_view,
                scorecard=dict(today=_scorecard([r for r in rows.values() if not r['late']]),
                               last_30_days=_scorecard(recent)),
                model=dict(version=M.MODEL_VERSION, calibrated_on=params.get('n_samples', 0),
                           min_samples=M.MIN_SAMPLES, band_only=bool(params.get('band_only')),
                           params={k: params[k] for k in ('beta_reg', 'beta_analog', 'beta_tech', 'beta_shield',
                                                          'beta_trend', 'beta_oi', 'beta_day', 'band_z', 'prob_c')},
                           changes=[dict(ts=h['ts'], reason=h['reason']) for h in c.execute(
                               'SELECT ts, reason FROM hourly_forecast_model_history WHERE index_name=? '
                               'ORDER BY ts DESC LIMIT 5', (name,))],
                           scorecard=_component_card(name, db)),
                note='Model estimates only, calibrated against this model\'s own record. Not a trade signal.')


_card_cache = {}


def _component_card(name, db=None):
    """Per-part directional record (cached for a minute; panel polls every 5 s)."""
    hit = _card_cache.get((name, db))
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    live = samples_for(name, db)
    seen = {(x['day'], x['slot']) for x in live}
    allx = [x for x in backtest_samples(name, db) if (x['day'], x['slot']) not in seen] + live
    card = M.component_scorecard(allx)
    _card_cache[(name, db)] = (time.monotonic(), card)
    return card


def available_days(name, limit=60, db=None):
    return [r['day'] for r in conn(db).execute(
        'SELECT DISTINCT day FROM hourly_forecasts WHERE index_name=? ORDER BY day DESC LIMIT ?',
        (name, limit))]


def strike_options(name, engine, now=None):
    """CE: ATM, +1, +2 strikes. PE: ATM, -1, -2. Live premiums from the engine's last poll."""
    now = now or now_ist()
    st = engine.states.get(name) if engine else None
    if st is None or st.chain is None or not M.finite(st.spot):
        return dict(available=False, reason='index_not_tracked')
    chain = st.chain
    step = chain.index.strike_step
    atm = round(st.spot / step) * step
    fresh = 0 <= time.monotonic() - getattr(chain, '_last_poll', 0) <= 30
    by_key = {(q.strike, q.opt_type): q for q in chain.quotes.values()}

    def row(strike, kind):
        q = by_key.get((strike, kind))
        if q is None:
            return dict(strike=strike, option_type=kind, available=False)
        lot = None
        try:
            from .instrument_master import get_lot_size
            lot = get_lot_size(q.symbol, q.token)
        except Exception:                               # noqa: BLE001
            pass
        return dict(strike=strike, option_type=kind, available=True, symbol=q.symbol, token=q.token,
                    ltp=q.ltp if fresh else None, bid=q.bid, ask=q.ask, lot_size=lot)
    return dict(available=True, spot=st.spot, atm=atm, step=step, quotes_fresh=fresh,
                ce=[row(atm + k * step, 'CE') for k in (0, 1, 2)],
                pe=[row(atm - k * step, 'PE') for k in (0, 1, 2)])


def allowed_strike(name, engine, direction, strike):
    opts = strike_options(name, engine)
    if not opts.get('available'):
        return None
    side = opts['ce'] if direction == 'CE' else opts['pe'] if direction == 'PE' else []
    return next((r for r in side if r['strike'] == strike and r['available']), None)
