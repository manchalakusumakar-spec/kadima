#!/usr/bin/env python3
"""Daily Missed Blasts report (trader 2026-10-27). Read-only; never touches trading.

For one trading day it finds every recorded option contract whose premium rose at least
MIN_MULTIPLE x from an intraday low to a later high, keeps the best contract per underlying,
and says what KADIMA did about it: traded (with P&L), triggered but not traded (with the
top rejection reasons), or never triggered.

Inputs (read-only): DATA_DIR/price_history.db, DATA_DIR/audit/<day>.jsonl, DATA_DIR/kadima.db
Outputs: <out-json-dir>/<day>.json and an HTML page (--html).
Limit: it only sees contracts KADIMA recorded that day (ladders it polled).

Usage: missed_blasts.py [--day YYYY-MM-DD] [--data /var/lib/kadima]
                        [--json-dir DIR] [--html FILE]
"""
import argparse, collections, html, json, os, re, sqlite3, sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MIN_MULTIPLE = 3.0
MIN_LOW = 0.5          # ignore sub-50-paise noise
MIN_GAIN_RS = 5.0      # and moves under Rs 5 per unit


def hhmm(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, IST).strftime('%H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return '?'


def best_run(rows):
    """rows: (minute, low, low_at, high, high_at) ascending. Best later-high / earlier-low."""
    best, lo = None, None
    for minute, low, low_at, high, high_at in rows:
        if low and low > 0 and (lo is None or low < lo[0]):
            lo = (low, low_at or minute)
        if lo and high and high > 0:
            mult = high / lo[0]
            if best is None or mult > best['multiple']:
                best = dict(low=lo[0], low_at=lo[1], high=high, high_at=high_at or minute,
                            multiple=mult)
    return best


def blasts(data_dir, day):
    path = os.path.join(data_dir, 'price_history.db')
    c = sqlite3.connect('file:' + path + '?mode=ro', uri=True, timeout=5)
    keys = c.execute("SELECT key, symbol, underlying, asset FROM price_days WHERE day=?",
                     (day,)).fetchall()
    spot, found = {}, []
    for key, symbol, underlying, asset in keys:
        rows = c.execute('SELECT minute, low, low_at, high, high_at FROM price_minutes '
                         'WHERE day=? AND key=? ORDER BY minute', (day, key)).fetchall()
        if not rows:
            continue
        if asset in ('STOCK', 'INDEX'):
            first = c.execute('SELECT first FROM price_minutes WHERE day=? AND key=? '
                              'ORDER BY minute LIMIT 1', (day, key)).fetchone()
            spot[underlying] = dict(open=first[0] if first else None,
                                    low=min(r[1] for r in rows if r[1]),
                                    high=max(r[3] for r in rows if r[3]))
            continue
        if asset != 'OPTION':
            continue
        b = best_run(rows)
        if (b and b['multiple'] >= MIN_MULTIPLE and b['low'] >= MIN_LOW
                and b['high'] - b['low'] >= MIN_GAIN_RS):
            found.append(dict(b, symbol=symbol, underlying=underlying))
    c.close()
    return found, spot


def audit_view(data_dir, day, names):
    """Per underlying: triggers, rejection reasons, dispatch results."""
    out = {n: dict(triggers=[], reasons=collections.Counter(), dispatch=[]) for n in names}
    path = os.path.join(data_dir, 'audit', day + '.jsonl')
    if not os.path.exists(path):
        return out
    pat = re.compile('|'.join(re.escape('"' + n + '"') for n in names)) if names else None
    with open(path, errors='ignore') as fh:
        for line in fh:
            if not pat or not pat.search(line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            name = e.get('underlying') or e.get('index') or e.get('name')
            if name not in out:
                continue
            ev, row = e.get('event', ''), out[name]
            if ev == 'MOVE_CAPTURE_TRIGGER':
                row['triggers'].append(dict(ts=e.get('ts', '')[11:19], score=e.get('score'),
                                            direction=e.get('direction')))
            elif ev.endswith('PREPARATION_REJECTED') or ev == 'STOCK_BLAST_STRIKE_REJECTS':
                row['reasons'][e.get('reason') or ev.lower()] += 1
            elif ev.endswith('DISPATCH_RESULT') or ev == 'STOCK_BLAST_LOTTERY_DISPATCH':
                row['dispatch'].append(dict(ts=e.get('ts', '')[11:19], ok=e.get('ok'),
                                            reason=e.get('reason')))
                if not e.get('ok') and e.get('reason'):
                    row['reasons']['dispatch:' + e['reason']] += 1
    return out


def positions(data_dir, day, names):
    out = collections.defaultdict(list)
    try:
        c = sqlite3.connect('file:' + os.path.join(data_dir, 'kadima.db') + '?mode=ro',
                            uri=True, timeout=5)
        for r in c.execute('SELECT index_name, symbol, entry_price, entry_ts, exit_price, '
                           'exit_reason, remaining_qty, lots, lot_size, status FROM positions '
                           'WHERE entry_ts LIKE ?', (day + '%',)):
            if r[0] in names:
                qty = (r[7] or 0) * (r[8] or 0) or r[6] or 0
                pnl = (r[4] - r[2]) * qty if r[4] and r[2] else None
                out[r[0]].append(dict(symbol=r[1], entry=r[2], entry_ts=(r[3] or '')[11:19],
                                      exit=r[4], exit_reason=r[5], status=r[9],
                                      pnl=round(pnl, 0) if pnl is not None else None))
        c.close()
    except sqlite3.Error:
        pass
    return out


def build(data_dir, day):
    found, spot = blasts(data_dir, day)
    per = {}
    for b in found:
        u = b['underlying']
        if u not in per or b['multiple'] > per[u]['best']['multiple']:
            per[u] = dict(best=b, contracts=per.get(u, {}).get('contracts', 0))
        per[u]['contracts'] = per[u].get('contracts', 0) + 1
    names = sorted(per)
    view, pos = audit_view(data_dir, day, names), positions(data_dir, day, names)
    rows = []
    for u in names:
        b, v = per[u]['best'], view[u]
        start = datetime.fromtimestamp(b['low_at'] / 1000, IST) if b['low_at'] else None
        near = [t for t in v['triggers']
                if start and t['ts'] >= (start - timedelta(minutes=15)).strftime('%H:%M:%S')]
        if pos.get(u):
            verdict = 'TRADED'
        elif v['triggers']:
            verdict = 'TRIGGERED_NOT_TRADED'
        else:
            verdict = 'NOT_TRIGGERED'
        s = spot.get(u, {})
        rows.append(dict(
            underlying=u, verdict=verdict, best_contract=b['symbol'],
            low=round(b['low'], 2), low_at=hhmm(b['low_at']), high=round(b['high'], 2),
            high_at=hhmm(b['high_at']), multiple=round(b['multiple'], 1),
            minutes=round((b['high_at'] - b['low_at']) / 60000) if b['low_at'] and b['high_at'] else None,
            contracts_3x=per[u]['contracts'],
            spot_open=s.get('open'), spot_low=s.get('low'), spot_high=s.get('high'),
            first_trigger_near_move=near[0] if near else None,
            triggers_today=len(v['triggers']),
            top_reasons=v['reasons'].most_common(4),
            dispatches=v['dispatch'][-5:], positions=pos.get(u, [])))
    rows.sort(key=lambda r: -r['multiple'])
    return dict(day=day, generated_at=datetime.now(IST).isoformat(timespec='seconds'),
                min_multiple=MIN_MULTIPLE, blasts=len(rows),
                missed=sum(r['verdict'] != 'TRADED' for r in rows), rows=rows,
                note='Only contracts KADIMA recorded that day are visible.')


def page(rep):
    e = html.escape
    colour = dict(TRADED='#2e9e5b', TRIGGERED_NOT_TRADED='#d08a1c', NOT_TRIGGERED='#c94545')
    body = []
    for r in rep['rows']:
        trig = r['first_trigger_near_move']
        trig_s = f"{trig['ts']} {trig['direction']} {trig['score']}" if trig else '-'
        reasons = ', '.join(f'{e(str(k))} x{n}' for k, n in r['top_reasons']) or '-'
        pnl = sum(p['pnl'] or 0 for p in r['positions']) if r['positions'] else None
        body.append(
            f"<tr><td><b>{e(r['underlying'])}</b><br><small>{e(r['best_contract'])}</small></td>"
            f"<td>{r['low']} @{r['low_at']} &rarr; {r['high']} @{r['high_at']}<br>"
            f"<b>{r['multiple']}x</b> in {r['minutes']} min ({r['contracts_3x']} contracts &ge;3x)</td>"
            f"<td><span style='color:{colour[r['verdict']]}'>{r['verdict'].replace('_', ' ')}</span>"
            f"{'<br>P&amp;L Rs ' + format(pnl, ',.0f') if pnl is not None else ''}</td>"
            f"<td>{e(trig_s)}<br><small>{r['triggers_today']} triggers</small></td>"
            f"<td><small>{reasons}</small></td></tr>")
    rows = ''.join(body) or "<tr><td colspan=5>No contract rose 3x today.</td></tr>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Missed Blasts</title>
<style>:root{{--bg:#0f1420;--fg:#e6e9ef;--mut:#8b93a7;--line:#263042}}
body{{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;margin:0;padding:16px}}
h1{{font-size:20px;margin:0 0 4px}}p{{color:var(--mut);margin:0 0 12px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:640px}}
td,th{{border-bottom:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}}
th{{color:var(--mut);font-weight:600}}small{{color:var(--mut)}}</style></head><body>
<h1>Missed Blasts &middot; {e(rep['day'])}</h1>
<p>{rep['blasts']} underlyings had a contract rise &ge;{rep['min_multiple']:.0f}x; {rep['missed']} not traded.
Generated {e(rep['generated_at'][11:16])} IST. {e(rep['note'])}</p>
<div class="wrap"><table><tr><th>Underlying</th><th>Move</th><th>KADIMA</th><th>First trigger</th>
<th>Why not</th></tr>{rows}</table></div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--day', default=datetime.now(IST).strftime('%Y-%m-%d'))
    ap.add_argument('--data', default=os.environ.get('KADIMA_DATA_DIR', '/var/lib/kadima'))
    ap.add_argument('--json-dir')
    ap.add_argument('--html')
    a = ap.parse_args()
    rep = build(a.data, a.day)
    if a.json_dir:
        os.makedirs(a.json_dir, exist_ok=True)
        with open(os.path.join(a.json_dir, a.day + '.json'), 'w') as f:
            json.dump(rep, f, indent=1, default=str)
    if a.html:
        tmp = a.html + '.tmp'
        with open(tmp, 'w') as f:
            f.write(page(rep))
        os.replace(tmp, a.html)
    print(f"{a.day}: {rep['blasts']} blasts, {rep['missed']} not traded")
    for r in rep['rows'][:15]:
        print(f"  {r['underlying']:12s} {r['multiple']:6.1f}x {r['best_contract']:28s} "
              f"{r['low_at']}->{r['high_at']} {r['verdict']} {r['top_reasons'][:2]}")


if __name__ == '__main__':
    sys.exit(main())
