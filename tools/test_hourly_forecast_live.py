"""Integration test: forecaster thread logic + storage + API view, no broker.

Uses a throwaway KADIMA_DATA_DIR, synthetic candles and a fake engine.

    python3 tools/test_hourly_forecast_live.py
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, date
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix='kadima_hf_')
os.environ['KADIMA_DATA_DIR'] = TMP
os.environ['KADIMA_API_TOKEN'] = 'test-token'
sys.path.insert(0, '.')
sys.path.insert(0, 'tools')

from kadima import storage, hourly_forecast as HF, hourly_forecast_model as M  # noqa: E402
from kadima.config import IST  # noqa: E402
from test_hourly_forecast import synthetic  # noqa: E402

FAILS = []
TOKEN = '99926000'


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def put(rows, upto=None):
    for start, o, h, lo, c in rows:
        start = start.replace(tzinfo=IST)
        if upto and start + timedelta(minutes=5) > upto:
            continue
        storage.upsert_candle(TOKEN, 5, start.isoformat(), o, h, lo, c, 0, c, True)
        storage.upsert_candle(TOKEN, 1, (start + timedelta(minutes=4)).isoformat(), c, c, c, c, 0, c, True)
        storage.upsert_candle(TOKEN, 1, start.isoformat(), o, o, o, o, 0, o, True)


def main():
    rows = synthetic(n_days=31, seed=3, day_drift=0.0002)
    days = sorted({r[0].date() for r in rows})
    today = days[-1]
    hist = [r for r in rows if r[0].date() < today]
    todays = [r for r in rows if r[0].date() == today]
    put(hist)

    chain = SimpleNamespace(quotes={}, _last_poll=0.0, index=SimpleNamespace(strike_step=50, token=TOKEN))
    st = SimpleNamespace(spot=None, last_updated=None, is_live_tick=False, chain=chain,
                         direction='CE', score=72.0)
    engine = SimpleNamespace(states={'NIFTY': st}, _dte=lambda s: 3.5)
    fc = HF.HourlyForecaster(engine)

    def at(hh, mm, ss=0):
        return datetime(today.year, today.month, today.day, hh, mm, ss, tzinfo=IST)

    # 09:15:03 - first tick of the day arrives, 09:15 slot is forecast on time.
    t = at(9, 15, 3)
    st.spot, st.last_updated, st.is_live_tick = todays[0][1], t, True
    fc.tick(t)
    v = HF.view('NIFTY', today, engine=engine, now=t)
    s0 = v['slots'][0]
    check('09:15 forecast generated on time', s0['status'] == 'live' and s0['late'] is False)
    check('09:15 forecast has 12 points', len(s0['points']) == 12)
    check('forecast % present', isinstance(s0['projected_pct'], float) and 0.5 <= s0['prob_direction'] <= 0.95)
    check('later slots pending', all(s['status'] == 'pending' for s in v['slots'][1:]))
    check('live price shown on running slot', s0['live'] and s0['live']['price'] == st.spot)

    # Forecast is frozen: a second tick in the slot never regenerates it.
    before = s0['points'][0]['forecast']
    st.spot = st.spot * 1.01
    fc.tick(at(9, 16))
    check('forecast frozen after generation',
          HF.view('NIFTY', today, engine=engine, now=at(9, 16))['slots'][0]['points'][0]['forecast'] == before)

    # Candles through 10:15 arrive; actuals fill and the slot is scored.
    put(todays, upto=at(10, 15))
    t = at(10, 15, 2)
    st.spot, st.last_updated = todays[12][1], t
    fc.tick(t)
    t = at(10, 15, 8)       # the 10:15 mark is read 5 s after the bar closes
    fc.tick(t)
    v = HF.view('NIFTY', today, engine=engine, now=t)
    s0, s1 = v['slots'][0], v['slots'][1]
    check('09:15 slot actuals all filled', all(p['actual'] is not None for p in s0['points']))
    check('09:15 slot finalized with a score', s0['status'] == 'final' and s0['score']['mae_pts'] is not None)
    check('10:15 slot generated at its start', s1['status'] == 'live' and not s1['late'])
    check('anchor equals live spot at slot start', s1['anchor'] == todays[12][1])
    check('scorecard counts the finished slot', v['scorecard']['today']['n'] == 1)
    au = s0.get('audit') or {}
    check('finished slot has an audit with a modification note', bool(au.get('modification')))
    check('weights kept until enough recent slots exist', au.get('modification', '').startswith('No change'))
    print('   audit:', au)

    # Engine restarted at 11:40: the 11:15 slot is rebuilt late from its open price.
    put(todays, upto=at(11, 40))
    st.is_live_tick = False
    t = at(11, 40)
    fc.tick(t)
    v = HF.view('NIFTY', today, engine=engine, now=t)
    s2 = v['slots'][2]
    check('mid-slot restart produces a late forecast', s2['status'] == 'live' and s2['late'] is True)
    check('late forecast anchored on slot open', abs(s2['anchor'] - todays[24][1]) < 1e-6)
    check('late slot excluded from calibration (2 on-time slots scored)', len(HF.samples_for('NIFTY')) == 2)

    # The whole day runs; last slot is 15:15-15:30 with 3 points.
    put(todays)
    st.is_live_tick = True
    for hh, mm in ((12, 15), (13, 15), (14, 15), (15, 15)):
        t = at(hh, mm, 1)
        st.spot, st.last_updated = todays[M.bar_index((hh, mm))][1], t
        fc.tick(t)
    fc.tick(at(15, 31))
    fc.stop()
    v = HF.view('NIFTY', today, engine=engine, now=at(15, 45))
    last = v['slots'][-1]
    check('7 slots in the day', len(v['slots']) == 7)
    check('15:15 slot has 3 points', len(last['points']) == 3)
    check('every slot final by close', all(s['status'] == 'final' for s in v['slots']))
    check('scorecard has 6 on-time slots', v['scorecard']['today']['n'] == 6)
    check('day listed for history', HF.available_days('NIFTY') == [today.isoformat()])
    for s in v['slots']:
        p = s['points']
        check(f"{s['slot']} band contains its forecast", all(x['lower'] < x['forecast'] < x['upper'] for x in p))

    # Missed slot: nothing generated and the slot is past.
    other = today + timedelta(days=1)
    v = HF.view('NIFTY', other, engine=None, now=datetime(other.year, other.month, other.day, 12, 0, tzinfo=IST))
    check('past slots without a forecast show missed', v['slots'][0]['status'] == 'missed')
    check('future slots show pending', v['slots'][-1]['status'] == 'pending')

    # Strike options: CE ATM/+1/+2, PE ATM/-1/-2.
    q = lambda strike, kind, tok: SimpleNamespace(strike=float(strike), opt_type=kind, token=tok,
                                                 symbol=f'NIFTY{strike}{kind}', ltp=100.0, bid=99.5, ask=100.5)
    chain.quotes = {str(i): q(k, t2, str(i)) for i, (k, t2) in enumerate(
        [(s, 'CE') for s in range(24950, 25301, 50)] + [(s, 'PE') for s in range(24950, 25301, 50)])}
    chain._last_poll = time.monotonic()
    st.spot = 25112.0
    opts = HF.strike_options('NIFTY', engine)
    check('CE strikes 25100/25150/25200', [r['strike'] for r in opts['ce']] == [25100, 25150, 25200])
    check('PE strikes 25100/25050/25000', [r['strike'] for r in opts['pe']] == [25100, 25050, 25000])
    check('premiums shown when quotes fresh', opts['ce'][0]['ltp'] == 100.0)
    check('allowed strike accepted', HF.allowed_strike('NIFTY', engine, 'PE', 25050.0) is not None)
    check('strike outside the offer refused', HF.allowed_strike('NIFTY', engine, 'PE', 25150.0) is None)

    # API: auth, view, and buy validation (no order reaches a broker here).
    from fastapi.testclient import TestClient
    from kadima import control_api
    HF.FORECASTER = fc
    calls = []
    control_api.EXECUTOR = SimpleNamespace(forecast_manual_buy=lambda *a: calls.append(a) or
                                           {'ok': False, 'reason': 'outside_market_hours'})
    client = TestClient(control_api.app)
    h = {'Authorization': 'Bearer test-token'}
    check('API refuses without token', client.get('/api/forecast/hourly').status_code == 401)
    r = client.get('/api/forecast/hourly', params={'index': 'NIFTY', 'day': today.isoformat()}, headers=h)
    check('API returns the day', r.status_code == 200 and len(r.json()['slots']) == 7)
    check('API includes trade strikes', r.json()['trade']['strikes']['atm'] == 25100)
    check('API refuses SENSEX until enabled',
          client.get('/api/forecast/hourly', params={'index': 'SENSEX'}, headers=h).status_code == 422)
    r = client.post('/api/forecast/buy', json={'index': 'NIFTY', 'direction': 'CE', 'strike': 25300, 'lots': 1}, headers=h)
    check('buy of a strike not offered is refused before the executor', r.status_code == 409 and not calls)
    r = client.post('/api/forecast/buy', json={'index': 'NIFTY', 'direction': 'CE', 'strike': 25150, 'lots': 1}, headers=h)
    check('valid buy reaches executor gates', len(calls) == 1 and r.status_code == 409
          and 'outside_market_hours' in r.text)
    r = client.post('/api/forecast/buy', json={'index': 'NIFTY', 'direction': 'CE', 'strike': 25150}, headers=h)
    check('buy without lots refused', r.status_code == 422)

    print('\n%d failure(s)' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
