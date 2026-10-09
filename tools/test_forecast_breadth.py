"""Constituent breadth in the hourly forecast: model, gating, live path, audit, view.

Offline: throwaway KADIMA_DATA_DIR, synthetic candles, a real ConstituentRadar fed
synthetic ticks with SYNTHETIC test weights, and a fake engine. No broker, no network.

    python3 tools/test_forecast_breadth.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix='kadima_breadth_')
os.environ['KADIMA_DATA_DIR'] = TMP
sys.path.insert(0, '.')
sys.path.insert(0, 'tools')

from kadima import storage, hourly_forecast as HF, hourly_forecast_model as M  # noqa: E402
from kadima.config import IST  # noqa: E402
from kadima.constituent_radar import ConstituentRadar  # noqa: E402
from test_hourly_forecast import synthetic  # noqa: E402

FAILS = []
TOKEN = '99926000'


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def ready(vwap_above=0.8, ret5=20.0, pos=0.7, neg=0.3, cov=1.0):
    return dict(status='ready', vwap_above=vwap_above, vwap_cov=cov, ret5_bps=ret5, pos5=pos, neg5=neg)


def model_tests():
    sigma = 0.0012
    t, parts = M.breadth_tilt(None, sigma)
    check('no radar reading -> tilt 0', t == 0.0 and parts == {})
    t, _ = M.breadth_tilt(dict(ready(), status='degraded'), sigma)
    check('degraded radar (not all 50 fresh) -> tilt 0', t == 0.0)
    t, parts = M.breadth_tilt(ready(), sigma)
    check('heavyweights above VWAP and rising -> positive tilt', t > 0.3 and set(parts) == {'vwap', 'momentum', 'participation'})
    t, _ = M.breadth_tilt(ready(vwap_above=0.2, ret5=-20, pos=0.3, neg=0.7), sigma)
    check('heavyweights below VWAP and falling -> negative tilt', t < -0.3)
    _, parts = M.breadth_tilt(ready(cov=0.5, vwap_above=0.5), sigma)
    check('thin VWAP coverage drops the VWAP part only', 'vwap' not in parts and 'momentum' in parts)

    rows = synthetic(n_days=31, seed=5)
    days = M.build_days(rows)
    di, i, n = len(days) - 1, M.bar_index((11, 15)), 12
    anchor = days[di].opens[i]
    base = M.make_forecast(days, di, i, n, anchor, as_of=days[di].day)
    off = M.make_forecast(days, di, i, n, anchor, as_of=days[di].day, breadth=dict(ready(), status='unavailable'))
    check('radar off gives the identical forecast (no change to today\'s model)',
          [p[1] for p in base.points] == [p[1] for p in off.points] and base.prob_up == off.prob_up)
    check('radar off is recorded as unavailable', off.inputs['breadth_status'] == 'unavailable'
          and off.inputs['x_breadth'] == 0)
    up = M.make_forecast(days, di, i, n, anchor, as_of=days[di].day, breadth=ready())
    check('bullish breadth nudges the path up by a small amount (beta 0.05)',
          up.points[-1][1] > base.points[-1][1] and up.inputs['x_breadth'] > 0
          and (up.points[-1][1] - base.points[-1][1]) / anchor < 0.002)
    check('default starting weight is 0.05', M.DEFAULT_PARAMS['beta_breadth'] == 0.05)

    # Gating: breadth keeps its prior weight until it has 30 resolved slots of its own.
    def sample(k, xb=None):
        y = ((k * 7919) % 13 - 6) / 6.0
        s = dict(x_reg=0.1 * ((k * 31) % 7 - 3), x_analog=0.0, x_tech=0.0, x_shield=0.0, x_trend=0.0, x_oi=0.0,
                 y=y, p_raw=0.5, up=y > 0, zs=[0.8], sd_end=1.0)
        if xb is not None:
            s['x_breadth'] = xb(y)
        return s
    hist = [sample(k) for k in range(300)]
    few = hist + [sample(k, lambda y: y) for k in range(10)]
    many = hist + [sample(k, lambda y: y) for k in range(40)]
    check('10 live breadth slots: weight stays at 0.05', M.fit_params(few)['beta_breadth'] == 0.05)
    check('40 live breadth slots that predicted well: weight rises', M.fit_params(many)['beta_breadth'] > 0.2)
    against = hist + [sample(k, lambda y: -y) for k in range(40)]
    check('40 live breadth slots that pointed wrong: weight turns negative/learns sign',
          M.fit_params(against)['beta_breadth'] < 0)
    p_few, p_old = M.fit_params(few), M.fit_params(hist + [sample(k) for k in range(10)])
    check('gated breadth does not change the other weights',
          all(p_few[b] == p_old[b] for b, _, _, _ in M.BETAS if b != 'beta_breadth'))
    stepped = M.step_toward(M.DEFAULT_PARAMS, dict(M.DEFAULT_PARAMS, beta_breadth=0.5))
    check('one recalibration moves breadth at most 0.10', abs(stepped['beta_breadth'] - 0.15) < 1e-9)

    tags = M.deviation_reasons('11:15', 0.2, 0.4, 0.9, 'DOWN', False, False, breadth_move=0.6)
    check('missed slot with heavyweights moving the other way is tagged', 'breadth_against' in tags)
    tags = M.deviation_reasons('11:15', 0.2, 0.4, 0.9, 'DOWN', False, False, breadth_move=-0.6)
    check('breadth that agreed with the forecast is not blamed', 'breadth_against' not in tags)
    tags = M.deviation_reasons('11:15', 0.2, 0.4, 0.9, 'UP', True, False, breadth_move=0.6)
    check('hit slots carry no breadth tag', 'breadth_against' not in tags)


def put(rows, upto=None):
    for start, o, h, lo, c in rows:
        start = start.replace(tzinfo=IST)
        if upto and start + timedelta(minutes=5) > upto:
            continue
        storage.upsert_candle(TOKEN, 5, start.isoformat(), o, h, lo, c, 0, c, True)
        storage.upsert_candle(TOKEN, 1, (start + timedelta(minutes=4)).isoformat(), c, c, c, c, 0, c, True)
        storage.upsert_candle(TOKEN, 1, start.isoformat(), o, o, o, o, 0, o, True)


def make_radar(day):
    members = [dict(symbol='S%d' % i, weight=0.02, sector='BANK' if i < 25 else 'OTHER') for i in range(50)]
    rows = [dict(symbol=m['symbol'] + '-EQ', exch_seg='NSE', token=str(1000 + i)) for i, m in enumerate(members)]
    manifest = dict(valid_from=day.isoformat(), valid_until=(day + timedelta(days=1)).isoformat(),
                    source='synthetic test only', members=members)
    return ConstituentRadar(manifest, rows, day)


def feed(radar, t, rise):
    """Three ticks per stock ending at t: baseline, volume at 100, then the move."""
    for i in range(50):
        for sec, px, vol in ((-300, 100.0, 1000), (-200, 100.0, 1100), (0, 100.0 * (1 + rise), 1101)):
            at = t + timedelta(seconds=sec)
            radar.on_tick(dict(token=str(1000 + i), ts=at, exchange_ts=at, exchange_type=1, ltp=px, volume=vol))


def live_tests():
    rows = synthetic(n_days=31, seed=3, day_drift=0.0002)
    today = sorted({r[0].date() for r in rows})[-1]
    put([r for r in rows if r[0].date() < today])
    todays = [r for r in rows if r[0].date() == today]
    at = lambda hh, mm, ss=0: datetime(today.year, today.month, today.day, hh, mm, ss, tzinfo=IST)

    radar = make_radar(today)
    chain = SimpleNamespace(quotes={}, _last_poll=0.0, index=SimpleNamespace(strike_step=50, token=TOKEN))
    st = SimpleNamespace(spot=None, last_updated=None, is_live_tick=False, chain=chain, direction=None, score=None)
    engine = SimpleNamespace(states={'NIFTY': st}, _dte=lambda s: 3.5, constituent_radar=radar)
    fc = HF.HourlyForecaster(engine)
    HF.FORECASTER = fc

    t = at(9, 15, 3)
    feed(radar, t, 0.01)
    st.spot, st.last_updated, st.is_live_tick = todays[0][1], t, True
    fc.tick(t)
    c = HF.conn()
    r = c.execute("SELECT inputs FROM hourly_forecasts WHERE slot='09:15'").fetchone()
    inp = json.loads(r['inputs'])
    check('09:15 forecast used the ready radar reading', inp['breadth_status'] == 'ready' and inp['x_breadth'] > 0)
    check('one breadth reading stored for the bar', c.execute('SELECT COUNT(*) FROM hourly_breadth').fetchone()[0] == 1)
    fc.tick(at(9, 15, 8))
    check('no second reading inside the same 5-minute bar',
          c.execute('SELECT COUNT(*) FROM hourly_breadth').fetchone()[0] == 1)

    v = HF.view('NIFTY', today, engine=engine, now=at(9, 15, 8))
    b = v['slots'][0].get('breadth') or {}
    check('panel slot shows breadth used at the start', b.get('status') == 'ready' and b.get('tilt', 0) > 0)
    check('panel shows the live breadth reading', (v.get('breadth_now') or {}).get('status') == 'ready')
    check('panel lists the breadth weight', v['model']['params'].get('beta_breadth') == 0.05)
    check('panel scorecard has a breadth row', 'breadth' in v['model']['scorecard'])

    # Radar goes stale mid-morning: the next slot is forecast without breadth.
    put(todays, upto=at(10, 15))
    t = at(10, 15, 2)
    st.spot, st.last_updated = todays[12][1], t
    fc.tick(t)
    inp = json.loads(c.execute("SELECT inputs FROM hourly_forecasts WHERE slot='10:15'").fetchone()['inputs'])
    check('stale radar -> slot forecast without breadth', inp['breadth_status'] == 'degraded' and inp['x_breadth'] == 0)
    v = HF.view('NIFTY', today, engine=engine, now=at(10, 15, 8))
    check('panel says breadth not used for that slot', v['slots'][1]['breadth']['status'] == 'degraded')

    # Self-audit: a missed slot where heavyweights rallied is explained by breadth.
    day = today.isoformat()
    for ts, above in ((at(11, 15), 0.40), (at(12, 15), 0.85)):
        reading = dict(ready(vwap_above=above), up=[dict(symbol='S1', bps=3.2)], down=[])
        with c:
            c.execute('INSERT INTO hourly_breadth VALUES (?,?,?,?,?)',
                      (day, 'NIFTY', ts.isoformat(), 'ready', json.dumps(reading)))
    fake = dict(index_name='NIFTY', day=day, slot='11:15', anchor=25000.0, direction='DOWN', late=0)
    pts = [dict(k=k, ts=(at(11, 15) + timedelta(minutes=5 * k)).isoformat(), actual=25000.0 + 10 * k,
                forecast=25000.0 - 2 * k, upper=25000.0 + 40, lower=25000.0 - 60)
           for k in range(1, 13)]
    sc = dict(dir_hit=False, actual_pct=0.48, coverage=0.9)
    tags = HF.audit_slot(fake, pts, sc, modification='No change: test')
    check('audit tags heavyweights moving against the forecast', 'breadth_against' in tags)
    a = c.execute("SELECT reasons FROM hourly_forecast_audit WHERE slot='11:15'").fetchone()
    info = json.loads(a['reasons'])
    text = HF.reason_text('breadth_against', info['details'])
    check('audit reason shows the VWAP shift and movers', '40%→85%' in text and 'S1 +3.2' in text)

    # Saved weights from before this change keep working: breadth gets its default.
    with c:
        old = dict(M.DEFAULT_PARAMS, beta_tech=0.22)
        old.pop('beta_breadth')
        c.execute('INSERT OR REPLACE INTO hourly_forecast_model VALUES (?,?,?)', ('NIFTY', json.dumps(old), 'x'))
    p = HF.load_params('NIFTY')
    check('existing learned weights are kept', p['beta_tech'] == 0.22 and p['beta_breadth'] == 0.05)
    check('model version unchanged so saved weights are not reset', M.MODEL_VERSION == 'hf-2.0')

    # No radar on the engine (feature switched off): nothing stored, forecasts unaffected.
    engine2 = SimpleNamespace(states={'NIFTY': st}, _dte=lambda s: 3.5)
    fc2 = HF.HourlyForecaster(engine2)
    check('radar absent -> no breadth reading', fc2.live_breadth(at(10, 20)) is None)
    off_radar = ConstituentRadar()
    check('radar disabled -> no breadth reading', HF.compact_breadth(off_radar.snapshot(at(10, 20))) is None)


def main():
    model_tests()
    live_tests()
    print(f'\n{len(FAILS)} failure(s)')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
