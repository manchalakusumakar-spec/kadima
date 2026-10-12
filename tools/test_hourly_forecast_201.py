"""Offline checks for hourly forecast hf-2.0.1 (12 Oct 2026).

Opening-hour band, 95% band, heavyweight input, trigger/failure levels, one re-forecast
after a band break, and that learned weights survive. No broker, no network.
Run:  python tools/test_hourly_forecast_201.py
"""
import json
import math
import os
import random
import socket
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima-hf201-')
os.environ['HOME'] = os.environ['KADIMA_DATA_DIR']
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))

from kadima import hourly_forecast as HF, hourly_forecast_model as M, storage  # noqa: E402
from kadima.config import IST  # noqa: E402

FAILED = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILED.append(name)


def synthetic(n_days=30, seed=3, open_vol=3.0, last=None):
    """Calm afternoons, volatile opens (open_vol x), Monday after a weekend included."""
    rng = random.Random(seed)
    rows, price, d = [], 25000.0, date(2026, 8, 31)
    while n_days:
        if d.weekday() < 5:
            price *= math.exp(rng.gauss(0, 0.003))
            for j in range(M.BARS_PER_DAY):
                vol = 0.0008 * (open_vol if j < 12 else 1.0)
                o = price
                c = o * math.exp(rng.gauss(0, vol))
                rows.append((datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * j),
                             o, max(o, c) * 1.0002, min(o, c) * 0.9998, c))
                price = c
            n_days -= 1
        d += timedelta(days=1)
    return rows


def test_model_versions():
    print('versions: learned weights are kept')
    check('params version unchanged (hf-2.0)', M.MODEL_VERSION == 'hf-2.0')
    check('revision recorded', M.REVISION == 'hf-2.0.1')
    check('new params have cautious defaults', M.DEFAULT_PARAMS['beta_heavy'] == 0.05
          and M.DEFAULT_PARAMS['band_z95'] == 1.96)
    c = HF.conn()
    saved = dict(M.DEFAULT_PARAMS, beta_reg=0.77, band_z=1.3)
    HF.save_params('NIFTY', saved, 'test')
    p = HF.load_params('NIFTY')
    check('a saved hf-2.0 calibration still loads', p['beta_reg'] == 0.77 and p['band_z'] == 1.3)
    check('missing new keys fall back to defaults', p['beta_heavy'] == 0.05)
    with c:
        c.execute('DELETE FROM hourly_forecast_model')


def test_opening_band():
    print('opening-hour band (09:15 slot)')
    rows = synthetic()
    days = M.build_days(rows)
    di = len(days) - 1
    tod = M.tod_profile(days[:di])
    s_open, factor = M.opening_sigma(days, di, tod)
    check('typical opening sigma measured', s_open is not None and s_open > 0)
    check('gap-day factor within 0.8-1.6', 0.8 <= factor <= 1.6)
    anchor = days[di].opens[0]
    f0 = M.make_forecast(days, di, 0, 12, anchor, as_of=days[di].day, tod=tod)
    real_only = M.sigma_at(days, di, 0, tod)
    check('09:15 sigma never below the typical opening sigma', f0.sigma >= s_open * 0.999)
    check('09:15 sigma at least the previous-afternoon estimate', f0.sigma >= real_only * 0.999)
    f12 = M.make_forecast(days, di, 12, 12, days[di].opens[12], as_of=days[di].day, tod=tod)
    check('10:15 slot does not use the opening floor', f12.inputs.get('sigma_open') is None)
    check('09:15 records the opening inputs', f0.inputs.get('sigma_open') is not None)


def test_outer_band_and_fit():
    print('95% band')
    rows = synthetic()
    days = M.build_days(rows)
    di = len(days) - 1
    f = M.make_forecast(days, di, 24, 12, days[di].opens[24], as_of=days[di].day)
    outer = f.inputs['outer']
    check('outer band per point', len(outer) == 12)
    check('outer band wider than inner at every point',
          all(o[1] >= p[2] and o[2] <= p[3] for o, p in zip(outer, f.points)))
    zs = [abs(random.Random(i).gauss(0, 1)) for i in range(400)]
    fitted = M.fit_params([dict(y=0.1, p_raw=0.5, up=True, zs=zs)] * 40)
    check('band_z95 fitted from errors (about 1.96 for normal errors)', 1.6 <= fitted['band_z95'] <= 2.3)
    stepped = M.step_toward(dict(M.DEFAULT_PARAMS), dict(M.DEFAULT_PARAMS, band_z95=3.4))
    check('band_z95 moves at most 0.2 per recalibration', abs(stepped['band_z95'] - 2.16) < 1e-9)


def test_heavy():
    print('heavyweights')
    rows = synthetic()
    days = M.build_days(rows)
    di = len(days) - 1
    a = days[di].opens[30]
    base = M.make_forecast(days, di, 30, 12, a, as_of=days[di].day)
    bull = M.make_forecast(days, di, 30, 12, a, as_of=days[di].day, heavy=1.0)
    bear = M.make_forecast(days, di, 30, 12, a, as_of=days[di].day, heavy=-1.0)
    check('heavyweights bullish raise the path, bearish lower it',
          bull.projected_pts > base.projected_pts > bear.projected_pts)
    check('x_heavy recorded for calibration', abs(bull.inputs['x_heavy'] - math.sqrt(12)) < 1e-9)
    old = [dict(x_reg=0.5, x_analog=0.2, x_tech=0.3, x_shield=0.1, x_trend=0.0, x_oi=0.0,
                y=0.4 * (1 if i % 3 else -1), p_raw=0.55, up=bool(i % 3), zs=[0.5, 1.0])
           for i in range(40)]
    fitted = M.fit_params(old)
    check('old samples without heavyweights keep beta_heavy at its prior', abs(fitted['beta_heavy'] - 0.05) < 1e-6)
    check('component scorecard lists heavy', 'heavy' in M.component_scorecard(old))


def test_levels():
    print('trigger / failure levels')
    bars = [(25050, 25000), (25060, 25010), (25040, 25005), (25030, 24990)]
    lv = M.key_levels(25020, bars, ce_wall=25100, pe_wall=24900, vwap=25012)
    check('trigger = nearest level above (opening range high 25,060)', lv['trigger'] == 25060)
    check('failure = nearest level below', lv['failure'] == 25012 and lv['failure_source'] == 'VWAP')
    lv0 = M.key_levels(25020, [], ce_wall=25100, pe_wall=24900)
    check('09:15 uses OI walls', lv0['trigger'] == 25100 and lv0['failure'] == 24900)
    check('no-edge text', M.bias_text('FLAT', lv0) == 'No edge: bullish above 25,100, bearish below 24,900')
    check('bullish text', M.bias_text('UP', lv0).startswith('Bullish while 24,900 holds'))
    check('bearish text', M.bias_text('DOWN', lv0).startswith('Bearish while 25,100 caps'))
    check('no levels -> plain text', M.bias_text('FLAT', None) == 'No edge')


def snap(price_vs_vwap, ema_bull, rsi):
    return NS(vwap=100.0, ema_bull=ema_bull, rsi=rsi), 100.0 + price_vs_vwap


def test_heavy_vote():
    print('heavyweight vote from engine snapshots')
    now = datetime(2026, 10, 13, 11, 0, tzinfo=IST)

    def stock(diff, bull, rsi):
        s, spot = snap(diff, bull, rsi)
        return NS(snap=s, spot=spot, technical_at=now - timedelta(minutes=2), last_updated=now)
    eng = NS(bank_states=dict(HDFCBANK=stock(1, True, 60), ICICIBANK=stock(1, True, 60), KOTAKBANK=stock(-1, False, 40)),
             heavy_states=dict(RELIANCE=stock(1, True, 60), INFY=stock(-1, False, 40), TCS=stock(-1, False, 40)),
             states={})
    f = HF.HourlyForecaster(eng, db=os.path.join(os.environ['KADIMA_DATA_DIR'], 'hv.db'))
    tilt, used = f._heavy_tilt(now)
    check('Kotak ignored (not a forecast heavyweight)', 'KOTAKBANK' not in used)
    check('weighted vote (13+9+9-5-3)/39', abs(tilt - 23 / 39) < 1e-9)
    eng.heavy_states['RELIANCE'].last_updated = now - timedelta(minutes=5)
    eng.heavy_states['INFY'].technical_at = now - timedelta(minutes=20)
    eng.heavy_states['TCS'].technical_at = None
    check('fewer than 3 fresh stocks -> no vote', f._heavy_tilt(now)[0] is None)


def put_candle(token, start, o, h, lo, c):
    storage.upsert_candle(token, 5, start.isoformat(), o, h, lo, c, 0, c, True)


def test_runner_update_and_scoring():
    print('runner: one update after a band break, 95% coverage, view')
    token = HF.INDICES['NIFTY'].token
    rows = synthetic(n_days=25, open_vol=1.5)
    day = rows[-1][0].date()
    hist = [r for r in rows if r[0].date() < day]
    for r in hist:
        put_candle(token, *r)
    db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'run.db')
    f = HF.HourlyForecaster(None, db=db)
    t0 = datetime(day.year, day.month, day.day, 10, 15, tzinfo=IST)
    today = [r for r in rows if r[0].date() == day and r[0] < t0]
    for r in today:
        put_candle(token, *r)
    anchor = today[-1][4]
    fc = f.generate('NIFTY', token, day, ((10, 15), (11, 15)), anchor, 0, t0)
    check('forecast made with levels and bias', fc is not None and 'bias' in fc.inputs and 'levels' in fc.inputs)
    c = HF.conn(db)
    pts = c.execute("SELECT * FROM hourly_forecast_points WHERE slot='10:15' ORDER BY k").fetchall()
    # Price drops well below the band from the first point, then keeps falling.
    price = anchor
    for k, p in enumerate(pts, start=1):
        price = p['lower'] - 40 - 5 * k
        start = t0 + timedelta(minutes=5 * (k - 1))
        put_candle(token, start, price + 3, price + 4, price - 2, price)
        f.update_actuals('NIFTY', start + timedelta(minutes=5, seconds=10), token=token)
    u = c.execute('SELECT * FROM hourly_forecast_updates').fetchall()
    check('exactly one update recorded', len(u) == 1)
    check('update starts at the first break (k=1)', u and u[0]['from_k'] == 1)
    check('update covers the rest of the slot', u and len(json.loads(u[0]['points'])) == 11)
    row = c.execute("SELECT * FROM hourly_forecasts WHERE slot='10:15'").fetchone()
    check('slot finalised', row['status'] == 'final')
    check('95% coverage scored', row['coverage95'] is not None and 0 <= row['coverage95'] <= 1)
    check('68% coverage zero for a move far outside', row['coverage'] == 0)
    check('update scored', u and c.execute('SELECT dir_hit FROM hourly_forecast_updates').fetchone()[0] is not None)
    v = HF.view('NIFTY', day, engine=None, now=t0 + timedelta(hours=2), db=db)
    s = next(x for x in v['slots'] if x['slot'] == '10:15')
    check('view: wide band per point', all('upper95' in p for p in s['points']))
    check('view: update with points and score', s.get('update') and len(s['update']['points']) == 11
          and s['update']['dir_hit'] is not None)
    check('view: bias, levels, coverage95', s.get('bias') and 'trigger' in (s.get('levels') or {})
          and 'coverage95' in s['score'])
    check('view: model lists beta_heavy and band_z95', 'beta_heavy' in v['model']['params']
          and 'band_z95' in v['model']['params'] and v['model']['revision'] == 'hf-2.0.1')
    samples = HF.samples_for('NIFTY', db, days=3650)
    check('the update is not a calibration sample (one sample for one slot)', len(samples) == 1)


def test_migration_old_db():
    print('existing forecast.db without coverage95')
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(), 'old.db')
    k = sqlite3.connect(path)
    k.executescript(HF.SCHEMA.split('CREATE TABLE IF NOT EXISTS hourly_forecast_points')[0])
    k.commit()
    k.close()
    c = HF.conn(path)
    cols = {r['name'] for r in c.execute('PRAGMA table_info(hourly_forecasts)')}
    check('coverage95 column added', 'coverage95' in cols)
    check('updates table created', c.execute("SELECT name FROM sqlite_master WHERE name='hourly_forecast_updates'").fetchone() is not None)


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print('-' * 40)
    print('ALL PASS' if not FAILED else f'{len(FAILED)} FAILED: {FAILED}')
    sys.exit(1 if FAILED else 0)
