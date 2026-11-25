"""Offline tests for the 26-Nov NSE-VIX change. No broker, no network.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_nse_vix_20261126.py

1. NSE archive parsers (index close / participant OI / market activity) on the real file layout.
2. vix_for_forecast: live VIX if fresh, previous session's last value before the first poll,
   else the stored NSE close; too old or switched off -> None.
3. The 09:15 hourly slot uses VIX as its IV when no ATM IV exists, records iv_source and is not
   widened 1.4x; without VIX it is made exactly as before; later slots never ask for VIX.
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
sys.path.insert(0, 'tools')
from kadima import audit                                               # noqa: E402
audit.log = lambda *a, **k: None                                       # never write the real audit log
from kadima import hourly_forecast as HF                               # noqa: E402
from kadima import nse_daily as N                                      # noqa: E402
from kadima.config import IST                                         # noqa: E402
from test_hourly_forecast import synthetic                            # noqa: E402

FAILS = []
TMP = os.environ['KADIMA_DATA_DIR']


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


IDX = ('Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,'
       'Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n'
       'Nifty 50,25-11-2026,25658.35,25951.65,25619,25819.25,469.6,1.85,533250582,42910.7,21.03,3.17,1.32\n'
       'Nifty Next 50,25-11-2026,1,2,0.5,1.5,0,0,-,-,-,-,-\n'
       'India VIX,25-11-2026,17.6975,18.2375,16.4275,17.67,-0.03,-.16,-,-,-,-,-\n')
POI = ('""Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Nov 25, 2026"",,,,,,,,,,,,,,\n'
       'Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,'
       'Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,'
       'Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,'
       'Total Long Contracts,Total Short Contracts\n'
       'Client,100,300,1,1,500,700,400,600,1,1,1,1,1,1\n'
       'DII,50,50,1,1,0,0,0,0,1,1,1,1,1,1\n'
       'FII,300,100,1,1,600,200,300,300,1,1,1,1,1,1\n'
       'Pro,10,30,1,1,10,10,10,10,1,1,1,1,1,1\n'
       'TOTAL,460,480,4,4,1110,910,710,910,4,4,4,4,4,4\n')
MA = (',25-Nov-2026\nThe Nifty witnessed an intraday movement of about 332.65 points.\n\n'
      ', Traded Value (Rs. In Crores), 137205.83\nADVANCES,2104\nDECLINES,875\nUNCHANGED,90\n')


def snapshot(value, ts):
    p = os.path.join(TMP, f'macro_{ts.strftime("%H%M%S%f")}_{value}.json')
    with open(p, 'w') as f:
        json.dump({'india_vix': {'value': value, 'last_updated': ts.isoformat()}}, f)
    return p


def main():
    # ---- 1. parsers
    v = N.parse_index_close(IDX)
    check('index close: VIX close parsed', abs(v.get('vix_close', 0) - 17.67) < 1e-9)
    check('index close: VIX open/high/low parsed', v.get('vix_high') == 18.2375 and v.get('vix_low') == 16.4275)
    check('index close: NIFTY 50 official close (not Nifty Next 50)', v.get('nifty_close') == 25819.25)
    p = N.parse_participant_oi(POI)
    check('participant OI: FII futures net = (300-100)/400', abs(p['fii_fut_net'] - 0.5) < 1e-9)
    check('participant OI: client futures net = (100-300)/400', abs(p['client_fut_net'] + 0.5) < 1e-9)
    check('participant OI: FII options net = ((600-300)-(200-300))/1400', abs(p['fii_opt_net'] - 400 / 1400) < 1e-6)
    check('participant OI: TOTAL row ignored', not any(k.startswith('total') for k in p))
    check('participant OI: zero positions do not divide by zero', 'dii_opt_net' not in p and p['dii_fut_net'] == 0)
    a = N.parse_market_activity(MA)
    check('market activity: advances / declines', a == {'adv': 2104.0, 'dec': 875.0})
    check('parsers tolerate empty / html input', N.parse_index_close('') == {} and N.parse_participant_oi('<html>') == {})

    # ---- 2. VIX for the forecast
    db = os.path.join(TMP, 'f.db')
    c = N.conn(db)
    with c:
        c.execute("INSERT INTO nse_daily VALUES ('2026-11-23','vix_close',16.9,'ind_close_all','x')")
    now = datetime(2026, 11, 25, 9, 15, 4, tzinfo=IST)
    snap = snapshot(17.1, now - timedelta(seconds=30))
    check('fresh live VIX used', N.vix_for_forecast(now, db=db, snapshot=snap) == (17.1, 'india_vix_live'))
    snap = snapshot(17.7, datetime(2026, 11, 24, 15, 39, 10, tzinfo=IST))
    check('previous session last value used before the first poll',
          N.vix_for_forecast(now, db=db, snapshot=snap) == (17.7, 'india_vix_last'))
    snap = snapshot(18.0, now - timedelta(minutes=30))
    check('stale same-day live VIX -> stored NSE close',
          N.vix_for_forecast(now, db=db, snapshot=snap) == (16.9, 'india_vix_close_2026-11-23'))
    missing = os.path.join(TMP, 'none.json')
    check('no snapshot -> stored NSE close', N.vix_for_forecast(now, db=db, snapshot=missing)[0] == 16.9)
    late = datetime(2026, 12, 4, 9, 15, 4, tzinfo=IST)
    check('stored close older than 7 days not used', N.vix_for_forecast(late, db=db, snapshot=missing) == (None, None))
    snap = snapshot(250.0, now - timedelta(seconds=30))
    check('absurd live value rejected, falls back', N.vix_for_forecast(now, db=db, snapshot=snap)[0] == 16.9)
    with patch.object(N, 'VIX_FALLBACK', False):
        check('switch off -> None', N.vix_for_forecast(now, db=db, snapshot=missing) == (None, None))

    # nightly fetch with the network mocked
    def fake_get(path, timeout=20):
        return IDX if 'ind_close_all' in path else POI if 'participant' in path else MA
    with patch.object(N, '_get', fake_get), patch.object(N.time, 'sleep', lambda s: None):
        got = N.run_nightly(datetime(2026, 11, 26, 16, 5, tzinfo=IST), days_back=3, db=db)
    check('nightly stores the missing weekdays before today (16:05 < 18:00)',
          sorted(got) == ['2026-11-24', '2026-11-25'] and all(n > 10 for n in got.values()))
    row = sqlite3.connect(db).execute("SELECT value, source FROM nse_daily WHERE day='2026-11-25' AND key='vix_close'").fetchone()
    check('nightly row: VIX close and source', row == (17.67, 'ind_close_all'))
    with patch.object(N, '_get', lambda path, timeout=20: None), patch.object(N.time, 'sleep', lambda s: None):
        got = N.run_nightly(datetime(2026, 11, 30, 16, 5, tzinfo=IST), days_back=2, db=db)
    check('network failure stores nothing and does not raise', got and all(v == 0 for v in got.values()))

    # ---- 3. hourly 09:15 slot
    rows = synthetic(n_days=41)
    days = sorted({r[0].date() for r in rows})
    today = days[-1]
    hist = [(r[0].replace(tzinfo=IST),) + tuple(r[1:]) for r in rows if r[0].date() < today]
    anchor = rows[[r[0].date() for r in rows].index(today)][1]
    t915 = datetime(today.year, today.month, today.day, 9, 15, 4, tzinfo=IST)

    def make(name, vix, slot=((9, 15), (10, 15)), atm_iv=None, now=t915):
        hf = HF.HourlyForecaster(None, db=os.path.join(TMP, name + '.db'))
        calls = []

        def fake(n):
            calls.append(n)
            return vix
        with patch.object(N, 'vix_for_forecast', fake):
            f = hf.generate('NIFTY', '99926000', today, slot, anchor, 0, now, atm_iv=atm_iv, rows=list(hist))
        return f, calls, os.path.join(TMP, name + '.db')

    base, _, _ = make('off', (None, None))
    f, calls, fdb = make('vix', (16.0, 'india_vix_live'))
    check('09:15 slot asks for VIX when no ATM IV', calls == [t915])
    check('09:15 slot records iv_source', f is not None and f.inputs.get('iv_source') == 'india_vix_live')
    check('09:15 slot uses VIX as its IV', f is not None and f.inputs.get('sigma_iv') is not None)
    stored = json.loads(sqlite3.connect(fdb).execute('SELECT inputs FROM hourly_forecasts').fetchone()[0])
    check('iv_source saved with the slot', stored.get('iv_source') == 'india_vix_live')
    import math

    def model_band(fc):
        z, m, sd = fc.inputs['params']['band_z'], fc.inputs['mean'][-1], fc.inputs['sd'][-1]
        return fc.anchor * (math.exp((m + z * sd) * fc.sigma) - math.exp((m - z * sd) * fc.sigma)) / 2
    check('VIX-sized 09:15 band is the model band (not widened 1.4x)', abs(f.band_pts - model_band(f)) < 0.02)
    check('without VIX the 09:15 band keeps the 1.4x widening',
          abs(base.band_pts - HF.OPEN_BAND_MULT * model_band(base)) < 0.05)
    check('no VIX -> no iv_source and the old 1.4x band', base.inputs.get('iv_source') is None
          and base.inputs.get('sigma_iv') is None)
    with patch.object(HF, 'VIX_OPEN', False):
        off, calls_off, _ = make('switch', (16.0, 'india_vix_live'))
    check('KADIMA_VIX_OPEN off -> VIX not asked, same as before', calls_off == [] and abs(off.band_pts - base.band_pts) < 1e-6)
    t1015 = datetime(today.year, today.month, today.day, 10, 15, 4, tzinfo=IST)
    later, calls_l, _ = make('later', (16.0, 'india_vix_live'), slot=((10, 15), (11, 15)), now=t1015)
    check('10:15 slot never asks for VIX', calls_l == [] and (later is None or later.inputs.get('iv_source') is None))
    withiv, calls_iv, _ = make('atm', (16.0, 'india_vix_live'), atm_iv=14.0)
    check('ATM IV present at 09:15 -> ATM IV kept, VIX not asked',
          calls_iv == [] and withiv.inputs.get('iv_source') == 'atm_iv')
    err, _, _ = make('err', None)   # a broken return value must not break the slot
    check('broken VIX lookup -> slot still made as before', err is not None and err.inputs.get('iv_source') is None
          and abs(err.band_pts - base.band_pts) < 1e-6)

    print(f'\n{len(FAILS)} failure(s)')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
