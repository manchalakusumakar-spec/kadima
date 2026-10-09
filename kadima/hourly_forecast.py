"""Hourly path forecasts vs actuals for KADIMA Celestial. Display only.

One forecast per index per slot (09:15-10:15 ... 14:15-15:15, 15:15-15:30),
made at the slot start and then frozen. Actual prices are filled in every
5 minutes from the engine's own closed candles, so the comparison is honest
and every slot is kept in forecast.db for review and recalibration.

Runs as a daemon thread inside the engine process rather than a separate
service: the inputs (live spot, option chain, Bull Shield) already live in
engine memory, it makes no broker calls and no network calls of its own,
and a second Python process costs far more RAM on this instance than a
thread does.

Never places, sizes or recommends an order. Manual trades from the
Celestial panel go through auto_executor.forecast_manual_buy(), which
applies every existing entry gate.
"""
from __future__ import annotations

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

FORECAST_INDICES = tuple(x for x in os.environ.get('KADIMA_FORECAST_INDICES', 'NIFTY').split(',') if x)
DB_PATH = os.path.join(DATA_DIR, 'forecast.db')
HISTORY_DAYS = 90
LOOP_SEC = 5
ON_TIME_SEC = 120            # generated within 2 min of slot start = on time
SPOT_FRESH_SEC = 30
FINALIZE_GIVE_UP_SEC = 900   # finalize with whatever actuals exist after 15 min
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
CREATE TABLE IF NOT EXISTS hourly_forecast_model (
    index_name TEXT PRIMARY KEY, params TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

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
        cache[path] = c
    return cache[path]


def _dt(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def _slot_dt(day, hm):
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=IST)


# ---------------------------------------------------------------- candles
def history_rows(token, until, days=HISTORY_DAYS):
    """Closed 5-minute candles from kadima.db strictly before `until`."""
    since = (until - timedelta(days=days)).isoformat()
    rows = storage.conn().execute(
        'SELECT start_ts, open, high, low, close FROM candles WHERE token=? AND interval=5 '
        'AND closed=1 AND start_ts>=? ORDER BY start_ts', (token, since)).fetchall()
    out = []
    for r in rows:
        start = _dt(r['start_ts'])
        if start + timedelta(minutes=5) <= until:
            out.append((start, r['open'], r['high'], r['low'], r['close']))
    return out


def candle_close(token, start, interval, closed_only=True):
    sql = 'SELECT close FROM candles WHERE token=? AND interval=? AND start_ts=?'
    if closed_only:
        sql += ' AND closed=1'
    r = storage.conn().execute(sql, (token, interval, start.isoformat())).fetchone()
    return r['close'] if r and M.finite(r['close']) else None


def candle_open(token, start):
    for interval in (1, 3, 5):
        r = storage.conn().execute(
            'SELECT open FROM candles WHERE token=? AND interval=? AND start_ts=?',
            (token, interval, start.isoformat())).fetchone()
        if r and M.finite(r['open']):
            return r['open']
    return None


def actual_at(token, ts, now):
    """Index price at a 5-minute mark: close of the 5-min bar ending there.

    A bar is only marked closed when the next tick arrives, which never
    happens for 15:25-15:30 if the index stops ticking at 15:30. A minute
    after the mark, the last stored price of that bar is final anyway.
    """
    a = (candle_close(token, ts - timedelta(minutes=5), 5)
         or candle_close(token, ts - timedelta(minutes=1), 1))
    if a is None and now >= ts + timedelta(seconds=60):
        a = (candle_close(token, ts - timedelta(minutes=5), 5, closed_only=False)
             or candle_close(token, ts - timedelta(minutes=1), 1, closed_only=False))
    return a


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
    return params


def samples_for(index_name, db=None, days=CALIBRATION_DAYS):
    """Resolved on-time slots as calibration samples for fit_params()."""
    c = conn(db)
    since = (now_ist().date() - timedelta(days=days)).isoformat()
    out = []
    for r in c.execute("SELECT * FROM hourly_forecasts WHERE index_name=? AND status='final' "
                       "AND late=0 AND actual_end IS NOT NULL AND day>=?", (index_name, since)):
        try:
            inp = json.loads(r['inputs'])
        except (TypeError, ValueError):
            continue
        pts = c.execute('SELECT k, actual FROM hourly_forecast_points WHERE day=? AND index_name=? '
                        'AND slot=? ORDER BY k', (r['day'], index_name, r['slot'])).fetchall()
        sigma, anchor = r['sigma'], r['anchor']
        zs = []
        for p in pts:
            k = p['k'] - 1
            if p['actual'] is None or k >= len(inp.get('sd', [])) or inp['sd'][k] <= 0:
                continue
            zs.append(abs(math.log(p['actual'] / anchor) / sigma - inp['mean'][k]) / inp['sd'][k])
        out.append(dict(x_reg=inp.get('x_reg'), x_analog=inp.get('x_analog'),
                        x_tech=inp.get('x_tech'), p_raw=inp.get('p_raw'),
                        y=math.log(r['actual_end'] / anchor) / sigma,
                        up=r['actual_end'] > anchor, zs=zs))
    return out


def refit(index_name, db=None):
    """Live slots refit around the backtest prior (if saved), else the defaults."""
    prior = load_params(index_name + ':prior', db)
    params = M.fit_params(samples_for(index_name, db), prior=prior)
    c = conn(db)
    with c:
        c.execute('INSERT OR REPLACE INTO hourly_forecast_model VALUES (?,?,?)',
                  (index_name, json.dumps(params), now_ist().isoformat()))
    return params


# ---------------------------------------------------------------- forecaster
class HourlyForecaster:
    def __init__(self, engine, db=None):
        self.engine = engine
        self.db = db
        self._stop = threading.Event()
        self._tod_cache = {}

    def start(self):
        threading.Thread(target=self.run, daemon=True, name='hourly-forecast').start()
        audit.log('hourly_forecast_started', indices=list(FORECAST_INDICES))

    def stop(self):
        """Engine shutdown (15:40): score the 15:15 slot with what has been recorded."""
        self._stop.set()
        try:
            now = now_ist()
            for name in FORECAST_INDICES:
                self.update_actuals(name, now, give_up=True)
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
            if slot:
                self.ensure_forecast(name, st, slot, now)
            self.update_actuals(name, now)

    def _live_inputs(self, st, now):
        """Spot (if fresh), ATM IV and Bull Shield from engine memory. No broker calls."""
        spot = None
        if (st.is_live_tick and st.last_updated and M.finite(st.spot)
                and 0 <= (now - st.last_updated).total_seconds() <= SPOT_FRESH_SEC):
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
            # Engine started mid-slot: rebuild from the slot's opening price and
            # data before it, and mark the slot late so calibration skips it.
            anchor, late = candle_open(token, t0), 1
            iv, shield_dir, shield_score = None, None, None
            if anchor is None:
                return None
        return self.generate(name, token, day, slot, anchor, late, now,
                             atm_iv=iv, shield_dir=shield_dir, shield_score=shield_score)

    def generate(self, name, token, day, slot, anchor, late, now, *, atm_iv=None,
                 shield_dir=None, shield_score=None, rows=None):
        start, end = slot
        t0 = _slot_dt(day, start)
        rows = rows if rows is not None else history_rows(token, t0)
        days = M.build_days(rows)
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
        f = M.make_forecast(days, di, i, n, anchor, as_of=day, tod=self._tod_cache[key],
                            params=params, atm_iv=atm_iv, shield_dir=shield_dir,
                            shield_score=shield_score)
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
            c.executemany('INSERT OR IGNORE INTO hourly_forecast_points VALUES (?,?,?,?,?,?,?,?,?)',
                          [(day.isoformat(), name, label, k,
                            (t0 + timedelta(minutes=M.STEP_MIN * k)).isoformat(),
                            round(mean, 2), round(up, 2), round(lo, 2), None)
                           for k, mean, up, lo in f.points])
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
                if p['actual'] is None and now >= ts + timedelta(seconds=5):
                    a = actual_at(token, ts, now)
                    if a is not None:
                        with c:
                            c.execute('UPDATE hourly_forecast_points SET actual=? WHERE day=? AND '
                                      'index_name=? AND slot=? AND k=?',
                                      (a, r['day'], name, r['slot'], p['k']))
                        changed = True
            end_ts = _dt(pts[-1]['ts']) if pts else None
            if end_ts is None:
                continue
            if changed:
                pts = c.execute('SELECT * FROM hourly_forecast_points WHERE day=? AND index_name=? AND slot=? '
                                'ORDER BY k', (r['day'], name, r['slot'])).fetchall()
            done = all(p['actual'] is not None for p in pts)
            age = (now - end_ts).total_seconds()
            if (done and age >= 0) or age >= FINALIZE_GIVE_UP_SEC or (give_up and age >= 0):
                self.finalize(r, pts)

    def finalize(self, r, pts):
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
        if sc and not r['late']:
            params = refit(r['index_name'], self.db)
            audit.log('hourly_forecast_recalibrated', index=r['index_name'],
                      n_samples=params['n_samples'],
                      **{k: params[k] for k in ('beta_reg', 'beta_analog', 'beta_tech', 'band_z', 'prob_c')})


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
    slots = []
    for start, end in M.SLOTS:
        label, t0, t1 = M.slot_label(start), _slot_dt(day, start), _slot_dt(day, end)
        r = rows.get(label)
        base = dict(slot=label, slot_end=M.slot_label(end), start_ts=t0.isoformat(), end_ts=t1.isoformat())
        if r is None:
            base['status'] = 'pending' if now < t0 + timedelta(seconds=ON_TIME_SEC) and day == now.date() else 'missed'
            slots.append(base)
            continue
        pts = c.execute('SELECT k, ts, forecast, upper, lower, actual FROM hourly_forecast_points '
                        'WHERE day=? AND index_name=? AND slot=? ORDER BY k',
                        (day.isoformat(), name, label)).fetchall()
        p_up = r['prob_up']
        base.update(
            status=r['status'], generated_at=r['generated_at'], late=bool(r['late']),
            anchor=r['anchor'], direction=r['direction'], prob_up=p_up,
            prob_direction=round(max(p_up, 1 - p_up), 4),
            projected_pct=r['projected_pct'], projected_pts=r['projected_pts'], band_pts=r['band_pts'],
            analog_matches=r['n_eff'], model_version=r['model_version'],
            points=[dict(p) for p in pts],
            score=None if r['status'] != 'final' else dict(
                dir_hit=None if r['dir_hit'] is None else bool(r['dir_hit']), mae_pts=r['mae_pts'],
                coverage=r['coverage'], end_error_pts=r['end_error_pts'], actual_pct=r['actual_pct']),
            live=live_price if (r['status'] == 'live' and t0 <= now < t1 + timedelta(seconds=60)) else None)
        slots.append(base)
    since = (day - timedelta(days=30)).isoformat()
    recent = c.execute("SELECT dir_hit, coverage, mae_pts FROM hourly_forecasts WHERE index_name=? "
                       "AND status='final' AND late=0 AND day>=? AND day<=?",
                       (name, since, day.isoformat())).fetchall()
    params = load_params(name, db)
    return dict(index=name, day=day.isoformat(), now=now.isoformat(), live=live_price, slots=slots,
                scorecard=dict(today=_scorecard([r for r in rows.values() if not r['late']]),
                               last_30_days=_scorecard(recent)),
                model=dict(version=M.MODEL_VERSION, calibrated_on=params.get('n_samples', 0),
                           min_samples=M.MIN_SAMPLES,
                           params={k: params[k] for k in ('beta_reg', 'beta_analog', 'beta_tech',
                                                          'band_z', 'prob_c')}),
                note='Model estimates only, calibrated against this model\'s own record. Not a trade signal.')


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
