"""Read-only replay of the opening-range breakout lean (kadima/forecast_orb.py).

    venv/bin/python -I tools/forecast_orb_replay.py [--days 30] [--forecast-db PATH] [--kadima-db PATH]

For every finished NIFTY session in the candle history it computes the lean each rule slot
would have shown at its start (only bars before the slot are used) and scores it against the
slot's close. It also compares, on the days the live hourly forecast exists, the live direction
calls ("before") with the live calls plus the lean where the live model said FLAT ("after").
Opens both databases read-only; writes nothing.
"""
import argparse
import collections
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kadima import forecast_orb as ORB  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY = '99926000'
SLOT_END = {'12:15': '13:15', '13:15': '14:15', '15:15': '15:30'}


def ro(path):
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_days(fdb, kdb):
    merged = {}
    try:
        for r in fdb.execute('SELECT start_ts, open, high, low, close FROM history_candles WHERE token=?', (NIFTY,)):
            merged[r['start_ts']] = tuple(r)
    except sqlite3.OperationalError:
        pass
    for r in kdb.execute('SELECT start_ts, open, high, low, close FROM candles WHERE token=? AND interval=5 '
                         'AND closed=1', (NIFTY,)):
        merged[r['start_ts']] = tuple(r)
    days = collections.OrderedDict()
    for ts in sorted(merged):
        _, o, h, lo, c = merged[ts]
        t = datetime.fromisoformat(ts)
        j = ((t.hour * 60 + t.minute) - (9 * 60 + 15)) // 5
        if not 0 <= j < 75:
            continue
        d = days.setdefault(t.date().isoformat(), [[None] * 75 for _ in range(4)])
        d[0][j], d[1][j], d[2][j], d[3][j] = o, h, lo, c
    return {k: v for k, v in days.items() if all(x is not None for x in v[3])}


def bar(hm):
    hh, mm = (int(x) for x in hm.split(':'))
    return ((hh * 60 + mm) - (9 * 60 + 15)) // 5


def replay(days):
    """[(day, slot, lean, rule, ref, end, move_pts, hit)] for every rule slot with a lean."""
    out = []
    for day, (o, h, lo, c) in days.items():
        for slot, end in SLOT_END.items():
            ln = ORB.lean_for(slot, o, h, lo, c)
            if ln is None or ln.lean == 'NONE':
                continue
            e = c[bar(end) - 1]
            mv = e - ln.ref
            out.append((day, slot, ln.lean, ln.rule, ln.ref, e, mv,
                        None if mv == 0 else (mv > 0) == (ln.lean == 'UP')))
    return out


def stats(rows):
    s = [r for r in rows if r[7] is not None]
    if not s:
        return '-'
    hit = sum(r[7] for r in s) / len(s)
    pts = sum(r[6] if r[2] == 'UP' else -r[6] for r in s) / len(s)
    return f'n={len(s):4d} hit={hit:5.1%} avg={pts:+5.1f} pts'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--forecast-db', default='/var/lib/kadima/forecast.db')
    ap.add_argument('--kadima-db', default='/var/lib/kadima/kadima.db')
    a = ap.parse_args()
    fdb, kdb = ro(a.forecast_db), ro(a.kadima_db)
    days = load_days(fdb, kdb)
    rows = replay(days)
    names = list(days)
    print(f'sessions {len(days)} ({names[0]} .. {names[-1]}), leans {len(rows)}')
    half = names[len(names) // 2]
    recent = names[-a.days] if len(names) >= a.days else names[0]
    for label, sel in (('ALL', lambda r: True), ('ORB', lambda r: r[3] == 'orb'), ('FADE', lambda r: r[3] == 'fade')):
        rr = [r for r in rows if sel(r)]
        print(f'{label:5s} all {stats(rr)} | 1st half {stats([r for r in rr if r[0] < half])} | '
              f'2nd half {stats([r for r in rr if r[0] >= half])} | last {a.days}d {stats([r for r in rr if r[0] >= recent])}')
    for slot in SLOT_END:
        rr = [r for r in rows if r[1] == slot]
        print(f'  {slot} {stats(rr)} | 1st {stats([r for r in rr if r[0] < half])} | 2nd {stats([r for r in rr if r[0] >= half])}')
    # before / after on the days the live hourly forecast ran
    live = collections.defaultdict(dict)
    for r in fdb.execute("SELECT day, slot, direction, anchor, actual_end FROM hourly_forecasts "
                         "WHERE index_name='NIFTY' AND status='final' AND late=0"):
        live[r['day']][r['slot']] = r
    lean_by = {(r[0], r[1]): r for r in rows}
    b_calls = b_hits = a_calls = a_hits = 0
    print('\nday         live calls (before)    with lean (after)')
    for day in sorted(live)[-a.days:]:
        before, after = [], []
        for slot in ('09:15', '10:15', '11:15', '12:15', '13:15', '14:15', '15:15'):
            r = live[day].get(slot)
            if r is None or r['actual_end'] is None:
                before.append('.'), after.append('.')
                continue
            up = r['actual_end'] > r['anchor']
            d = r['direction']
            if d != 'FLAT':
                b_calls += 1
                b_hits += (d == 'UP') == up
            ln = lean_by.get((day, slot))
            d2 = d if d != 'FLAT' or ln is None else ln[2]
            if d2 != 'FLAT':
                a_calls += 1
                a_hits += (d2 == 'UP') == up
            mark = lambda x: x[0] if x == 'FLAT' else (x[0] if (x == 'UP') == up else x[0].lower())
            before.append(mark(d)), after.append(mark(d2))
        print(f'{day}  {"".join(before):20s}  {"".join(after)}')
    print(f'before: {b_calls} direction calls, {b_hits} right | after: {a_calls} calls, {a_hits} right '
          '(capital = right, lower case = wrong, F = FLAT)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
