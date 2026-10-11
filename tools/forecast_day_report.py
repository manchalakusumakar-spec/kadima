"""Read-only day report: hourly forecast vs actual, and why each slot varied.

    cd ~/kadima-py && python3 tools/forecast_day_report.py            # today
    cd ~/kadima-py && python3 tools/forecast_day_report.py 2026-10-09

For each slot: what the model forecast and why (the parts that drove it),
what actually happened, and the market context the model did or did not see
(day trend so far, option-chain PCR and OI shifts during the slot). Opens
the databases read-only; changes nothing.
"""
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

DATA = os.environ.get('KADIMA_DATA_DIR', '/var/lib/kadima')
NIFTY = '99926000'


def ro(name):
    path = os.path.join(DATA, name)
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    return c


def pcr_at(k, ts):
    """PCR and ATM-zone OI from the last NIFTY option-chain sample at or before ts."""
    r = k.execute("SELECT ts FROM oi_snapshots WHERE index_name='NIFTY' AND ts<=? "
                  "ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    if not r:
        return None
    rows = k.execute("SELECT opt_type, strike, oi FROM oi_snapshots WHERE index_name='NIFTY' AND ts=?",
                     (r['ts'],)).fetchall()
    ce = sum(x['oi'] or 0 for x in rows if x['opt_type'] == 'CE')
    pe = sum(x['oi'] or 0 for x in rows if x['opt_type'] == 'PE')
    top_ce = max((x for x in rows if x['opt_type'] == 'CE'), key=lambda x: x['oi'] or 0, default=None)
    top_pe = max((x for x in rows if x['opt_type'] == 'PE'), key=lambda x: x['oi'] or 0, default=None)
    return dict(pcr=pe / ce if ce else None, ce=ce, pe=pe,
                ce_wall=top_ce['strike'] if top_ce else None, pe_wall=top_pe['strike'] if top_pe else None)


def fmt(x, d=0, sign=False):
    if x is None:
        return '-'
    s = f'{x:+.{d}f}' if sign else f'{x:.{d}f}'
    return s


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    f, k = ro('forecast.db'), ro('kadima.db')
    rows = f.execute('SELECT * FROM hourly_forecasts WHERE day=? AND index_name=? ORDER BY slot',
                     (day, 'NIFTY')).fetchall()
    bars = k.execute("SELECT start_ts, open, close FROM candles WHERE token=? AND interval=5 AND closed=1 "
                     "AND substr(start_ts,1,10)=? ORDER BY start_ts", (NIFTY, day)).fetchall()
    if not bars:
        print('No NIFTY candles for', day)
        return 1
    day_open = bars[0]['open']
    prev = k.execute("SELECT close FROM candles WHERE token=? AND interval=5 AND closed=1 AND start_ts<? "
                     "ORDER BY start_ts DESC LIMIT 1", (NIFTY, bars[0]['start_ts'])).fetchone()
    print(f'NIFTY {day}: prev close {fmt(prev and prev["close"], 1)}, open {day_open:.1f}, '
          f'last {bars[-1]["close"]:.1f} ({(bars[-1]["close"] / day_open - 1) * 100:+.2f}% from open)')
    print('Hourly closes:', ' '.join(f'{b["start_ts"][11:16]}={b["close"]:.0f}'
                                     for b in bars if b['start_ts'][14:16] in ('10',)))
    print()
    print('slot  late dir  p_up  fc_pts act_pts err  band  in%  | why: reg analog tech(ema rsi twap shield) '
          '| ctx: day%  PCR start->end  CE/PE wall  hit')
    for r in rows:
        inp = json.loads(r['inputs'] or '{}')
        parts = inp.get('tech_parts') or {}
        p = inp.get('params') or {}
        start = datetime.fromisoformat(r['day'] + 'T' + r['slot'] + ':00+05:30')
        end = datetime.fromisoformat(r['day'] + 'T' + r['slot_end'] + ':00+05:30')
        a = pcr_at(k, start.isoformat())
        b = pcr_at(k, end.isoformat())
        act_pts = (r['actual_end'] - r['anchor']) if r['actual_end'] else None
        reg = p.get('beta_reg', 0) * (inp.get('x_reg') or 0)
        ana = p.get('beta_analog', 0) * (inp.get('x_analog') or 0)
        tech = p.get('beta_tech', 0) * (inp.get('x_tech') or 0)
        day_pct = (r['anchor'] / day_open - 1) * 100
        print(f"{r['slot']} {'L' if r['late'] else ' '}  {r['direction']:<4} {r['prob_up']:.2f} "
              f"{fmt(r['projected_pts'], 0, True):>6} {fmt(act_pts, 0, True):>7} {fmt(r['end_error_pts'], 0, True):>4} "
              f"{fmt(r['band_pts'], 0):>4} {fmt((r['coverage'] or 0) * 100, 0):>4} "
              f"| {reg:+.2f} {ana:+.2f} {tech:+.2f}("
              f"{fmt(parts.get('ema'), 2, True)} {fmt(parts.get('rsi'), 2, True)} "
              f"{fmt(parts.get('twap'), 2, True)} {fmt(parts.get('shield'), 2, True)}) "
              f"| {day_pct:+.2f} {fmt(a and a['pcr'], 2)}->{fmt(b and b['pcr'], 2)} "
              f"{fmt(a and a['ce_wall'], 0)}/{fmt(a and a['pe_wall'], 0)} "
              f"{'-' if r['dir_hit'] is None else ('HIT' if r['dir_hit'] else 'miss')}")
    print()
    print('why = contribution to the forecast end-move in sigma units (+ up, - down).')
    print('reg = pattern regression, analog = similar past moments, tech = EMA/RSI/TWAP/Shield tilt.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
