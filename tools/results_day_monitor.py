#!/usr/bin/env python3
"""Read-only intraday monitor (results day 2026-10-27). Prints one compact status block.

Engine health, stock-lane activity (triggers, filter blocks, dispatch results), today's positions
with P&L, the results-day names, and the biggest stock movers KADIMA has not traded.
Usage: results_day_monitor.py [--day YYYY-MM-DD] [--data /var/lib/kadima]
"""
import argparse, collections, hashlib, json, os, re, sqlite3, subprocess
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
RESULTS = ['LT', 'MARUTI', 'BAJAJ-AUTO', 'TATASTEEL', 'SBILIFE', 'CIPLA', 'INDUSTOWER', 'MAZDOCK',
           'MFSL', 'PGEL', 'RADICO', 'SAIL', 'WAAREEENER']


def key(exchange, token, symbol):
    return hashlib.sha256((exchange + '|' + str(token) + '|' + symbol).encode()).hexdigest()[:32]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--day', default=datetime.now(IST).strftime('%Y-%m-%d'))
    ap.add_argument('--data', default='/var/lib/kadima')
    a = ap.parse_args()
    out = []
    try:
        act = subprocess.run(['systemctl', 'is-active', 'kadima'], capture_output=True, text=True).stdout.strip()
        rs = subprocess.run(['systemctl', 'show', 'kadima', '-p', 'NRestarts'], capture_output=True,
                            text=True).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        act, rs = 'unknown', str(exc)
    out.append(f'engine={act} {rs} at {datetime.now(IST).strftime("%H:%M")}')

    ev, blocks, disp_fail, trig = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    disp_ok, errors, last_trig = [], collections.Counter(), {}
    path = os.path.join(a.data, 'audit', a.day + '.jsonl')
    if os.path.exists(path):
        for line in open(path, errors='ignore'):
            m = re.search(r'"event":"([A-Za-z_]+)"', line)
            e = m.group(1) if m else ''
            if e == 'MOVE_CAPTURE_TRIGGER':
                n = re.search(r'"underlying":"([^"]+)"', line)
                if n:
                    trig[n.group(1)] += 1
                    last_trig[n.group(1)] = line[18:26]
            elif e == 'STOCK_BLAST_FILTER_BLOCK':
                blocks[re.search(r'"reason":"([a-z_0-9]+)"', line).group(1)] += 1
            elif e in ('MOVE_CAPTURE_DISPATCH_RESULT', 'STOCK_BLAST_LOTTERY_DISPATCH'):
                d = json.loads(line)
                if d.get('ok'):
                    disp_ok.append(f"{d['ts'][11:16]} {d.get('index')} {d.get('direction')} s{d.get('score')}")
                else:
                    disp_fail[d.get('reason')] += 1
            elif e == 'MOVE_CAPTURE_PREPARATION_REJECTED':
                r = re.search(r'"reason":"([a-z_0-9]+)"', line)
                errors[r.group(1) if r else '?'] += 1
            if 'Traceback' in line or '"error"' in line[:200]:
                errors['error_lines'] += 1
    out.append(f'stock triggers={sum(trig.values())} ({len(trig)} stocks); stock orders sent={len(disp_ok)}')
    if disp_ok:
        out.append('  sent: ' + '; '.join(disp_ok[-8:]))
    if blocks:
        out.append('  filter blocks: ' + ', '.join(f'{k} {v}' for k, v in blocks.most_common()))
    if disp_fail:
        out.append('  executor rejects: ' + ', '.join(f'{k} {v}' for k, v in disp_fail.most_common(5)))
    if errors:
        out.append('  prep rejects: ' + ', '.join(f'{k} {v}' for k, v in errors.most_common(5)))

    # positions today
    pnl_total, rows, traded = 0.0, [], set()
    ph = None
    try:
        ph = sqlite3.connect('file:' + os.path.join(a.data, 'price_history.db') + '?mode=ro', uri=True, timeout=2)
    except sqlite3.Error:
        pass
    try:
        c = sqlite3.connect('file:' + os.path.join(a.data, 'kadima.db') + '?mode=ro', uri=True, timeout=2)
        for r in c.execute('SELECT kind, index_name, symbol, token, exchange, entry_price, entry_ts, lots, '
                           'lot_size, remaining_qty, status, peak_price, exit_price, exit_reason '
                           'FROM positions WHERE entry_ts LIKE ? ORDER BY entry_ts', (a.day + '%',)):
            kind, und, sym, tok, exch, entry, ets, lots, lot, rem, status, peak, exitp, why = r
            qty = (lots or 0) * (lot or 0) or rem or 0
            px = exitp
            if px is None and ph is not None:
                q = ph.execute('SELECT last FROM price_minutes WHERE day=? AND key=? ORDER BY minute DESC LIMIT 1',
                               (a.day, key(exch or 'NFO', tok, sym))).fetchone()
                px = q[0] if q else None
            pnl = (px - entry) * qty if px and entry else 0.0
            pnl_total += pnl
            traded.add(und)
            rows.append(f"{(ets or '')[11:16]} {kind} {sym} {status} in {entry} pk {peak} "
                        f"{'out ' + str(exitp) if exitp else 'now ' + str(px)} {why or ''} P&L {pnl:,.0f}")
        c.close()
    except sqlite3.Error as exc:
        rows.append('positions unreadable: ' + str(exc)[:80])
    out.append(f'positions today={len(rows)} est P&L Rs {pnl_total:,.0f}')
    out += ['  ' + x for x in rows[-12:]]

    # results names + top movers
    moves = {}
    if ph is not None:
        for k, und in ph.execute("SELECT key, underlying FROM price_days WHERE day=? AND asset='STOCK'", (a.day,)):
            f = ph.execute('SELECT first FROM price_minutes WHERE day=? AND key=? ORDER BY minute LIMIT 1',
                           (a.day, k)).fetchone()
            l = ph.execute('SELECT last FROM price_minutes WHERE day=? AND key=? ORDER BY minute DESC LIMIT 1',
                           (a.day, k)).fetchone()
            if f and l and f[0] and l[0]:
                moves[und] = (l[0] / f[0] - 1) * 100
    res = []
    for n in RESULTS:
        mv = moves.get(n)
        res.append(f"{n} {mv:+.1f}%" if mv is not None else f"{n} n/a")
        if trig.get(n):
            res[-1] += f" trig{trig[n]}@{last_trig[n]}"
        if n in traded:
            res[-1] += ' TRADED'
    out.append('results names: ' + ', '.join(res))
    top = sorted(moves.items(), key=lambda kv: -abs(kv[1]))[:10]
    out.append('top movers: ' + ', '.join(f"{n} {m:+.1f}%{' T' if n in traded else ''}" for n, m in top))
    print('\n'.join(out))


if __name__ == '__main__':
    main()
