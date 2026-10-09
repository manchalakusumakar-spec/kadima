"""Read-only: the strikes KADIMA shortlisted on a given day.

    cd ~/kadima-py && python3 tools/strikes_today.py              # today
    cd ~/kadima-py && python3 tools/strikes_today.py 2026-10-09

Sections:
  1. Signals raised (Bull Shield / Gamma Blast / Celestial / manual), with the strike picked.
  2. The last Celestial candidate list (the Top 20 shortlist) saved on the server.
  3. Positions/orders taken that day.
Opens kadima.db read-only; changes nothing.
"""
import json
import os
import sqlite3
import sys
from datetime import date

DATA = os.environ.get('KADIMA_DATA_DIR', '/var/lib/kadima')


def ro():
    c = sqlite3.connect(f"file:{os.path.join(DATA, 'kadima.db')}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def strike(x):
    return '-' if x is None else (f'{x:g}' if isinstance(x, (int, float)) else str(x))


def f(x, d=1):
    return '-' if x is None else (f'{x:.{d}f}' if isinstance(x, (int, float)) else str(x))


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    c = ro()

    print(f'== 1. Signals raised on {day}')
    rows = c.execute("SELECT id, ts, index_name, tier, score, direction, kind, payload FROM signals "
                     "WHERE substr(ts,1,10)=? ORDER BY ts", (day,)).fetchall()
    if not rows:
        print('   none')
    print(f"   {'time':5} {'#':>5} {'kind':14} {'index':10} {'dir':3} {'tier':10} {'score':>5}  strike / contract")
    for r in rows:
        try:
            p = json.loads(r['payload'] or '{}')
        except ValueError:
            p = {}
        s = p.get('strike') if isinstance(p.get('strike'), dict) else {}
        contract = (s.get('symbol') or p.get('symbol') or '')
        extra = []
        if s.get('ltp') is not None or p.get('option_ltp') is not None:
            extra.append('LTP ' + f(s.get('ltp', p.get('option_ltp'))))
        if s.get('delta') is not None:
            extra.append('delta ' + f(s.get('delta'), 2))
        if p.get('best_strike') is not None:
            extra.append('strike ' + strike(p.get('best_strike')))
        print(f"   {r['ts'][11:16]} {r['id']:>5} {(r['kind'] or '')[:14]:14} {(r['index_name'] or '')[:10]:10} "
              f"{r['direction'] or '-':3} {(r['tier'] or '')[:10]:10} {f(r['score']):>5}  {contract} {' '.join(extra)}")

    print(f'\n== 2. Last Celestial shortlist saved (Top 20 candidates)')
    if not has(c, 'celestial_candidates'):
        print('   no candidate table (Celestial not enabled)')
    else:
        cands = []
        for r in c.execute('SELECT producer, ts, payload FROM celestial_candidates WHERE substr(ts,1,10)=?', (day,)):
            try:
                cands.append(json.loads(r['payload']))
            except ValueError:
                pass
        if not cands:
            print('   none saved for this day (the list is replaced every cycle and cleared after a day)')
        cands.sort(key=lambda x: (-(x.get('gamma_score') or 0), x.get('underlying', '')))
        print(f"   {'underlying':12} {'dir':3} {'strike':>8} {'symbol':24} {'gamma':>5} {'shield':>6} "
              f"{'prem+%':>6} {'LTP':>7} {'OI':>3} {'sprd':5} {'trade':5} as of")
        for x in cands[:40]:
            shield = x.get('bull_shield_score') if x.get('direction') == 'CE' else x.get('bear_shield_score')
            print(f"   {x.get('underlying', '')[:12]:12} {x.get('direction', ''):3} {strike(x.get('best_strike')):>8} "
                  f"{(x.get('symbol') or '')[:24]:24} {f(x.get('gamma_score'), 0):>5} {f(shield, 0):>6} "
                  f"{f(x.get('premium_move_pct'), 0):>6} {f(x.get('option_ltp'), 2):>7} "
                  f"{'Y' if x.get('oi_support') else 'n':>3} {(x.get('spread_status') or '')[:5]:5} "
                  f"{'Y' if x.get('tradeable') else 'n':5} {(x.get('last_updated') or '')[11:19]}")
        if len(cands) > 40:
            print(f'   ... {len(cands) - 40} more')

    print(f'\n== 3. Positions / orders on {day}')
    pos = c.execute("SELECT signal_id, kind, index_name, direction, symbol, lots, lot_size, entry_price, "
                    "exit_price, status, exit_reason, entry_ts FROM positions WHERE substr(entry_ts,1,10)=? "
                    "ORDER BY entry_ts", (day,)).fetchall()
    if not pos:
        print('   none')
    for p in pos:
        pnl = ((p['exit_price'] - p['entry_price']) * (p['lots'] or 0) * (p['lot_size'] or 0)
               if p['exit_price'] and p['entry_price'] else None)
        print(f"   {p['entry_ts'][11:16]} #{p['signal_id']} {p['kind'] or ''} {p['symbol']} x{p['lots']} "
              f"entry {f(p['entry_price'], 2)} exit {f(p['exit_price'], 2)} {p['status']} "
              f"{p['exit_reason'] or ''} P&L {f(pnl, 0)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
