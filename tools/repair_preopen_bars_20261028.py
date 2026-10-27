"""Repair past 09:15 candles that absorbed NSE pre-open ticks (companion to the 28-Oct candle fix).

For each day and token whose 09:15 bars (1/3/5 min) exist, rebuild open/high/low/close from the
stored ticks at or after 09:15:00 inside the bar. Only rows that change are written; every changed
row is saved first to a JSON backup. Days whose ticks are no longer stored are listed and left alone.

    python3 tools/repair_preopen_bars_20261028.py            # dry run: list what would change
    python3 tools/repair_preopen_bars_20261028.py --apply    # write (after the close, engine stopped or idle)
    python3 tools/repair_preopen_bars_20261028.py --restore <backup.json>
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.environ.get('KADIMA_ROOT', os.path.expanduser('~/kadima-py'))
sys.path.insert(0, ROOT)
from kadima.config import DB_PATH  # noqa: E402


def main():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    if '--restore' in sys.argv:
        rows = json.load(open(sys.argv[sys.argv.index('--restore') + 1]))
        with c:
            for r in rows:
                c.execute('UPDATE candles SET open=?, high=?, low=?, close=? WHERE token=? AND interval=? AND start_ts=?',
                          (r['open'], r['high'], r['low'], r['close'], r['token'], r['interval'], r['start_ts']))
        print('restored', len(rows), 'rows')
        return
    apply = '--apply' in sys.argv
    bars = c.execute("SELECT token, interval, start_ts, open, high, low, close FROM candles "
                     "WHERE substr(start_ts, 12, 5)='09:15' AND interval IN (1,3,5)").fetchall()
    changes, no_ticks = [], set()
    for b in bars:
        start = datetime.fromisoformat(b['start_ts'])
        end = start + timedelta(minutes=b['interval'])
        t = c.execute('SELECT ltp FROM ticks WHERE token=? AND ts>=? AND ts<? AND ltp IS NOT NULL ORDER BY ts',
                      (b['token'], start.isoformat(), end.isoformat())).fetchall()
        pre = c.execute('SELECT COUNT(*) FROM ticks WHERE token=? AND ts>=? AND ts<?',
                        (b['token'], start.replace(hour=9, minute=0).isoformat(), start.isoformat())).fetchone()[0]
        if not t:
            no_ticks.add(b['start_ts'][:10])
            continue
        px = [x['ltp'] for x in t]
        new = dict(open=px[0], high=max(px), low=min(px), close=px[-1])
        if any(abs(new[k] - b[k]) > 0.001 for k in ('open', 'high', 'low', 'close')):
            changes.append((dict(b), new, pre))
    print(f'09:15 bars checked {len(bars)}, to repair {len(changes)}, days without ticks {len(no_ticks)}'
          + (f' (oldest {min(no_ticks)}, newest {max(no_ticks)})' if no_ticks else ''))
    for old, new, pre in changes[:40]:
        print(f"  {old['token']} {old['start_ts'][:10]} {old['interval']}m  open {old['open']}->{new['open']}  "
              f"high {old['high']}->{new['high']}  low {old['low']}->{new['low']}  (pre-open ticks {pre})")
    if not apply or not changes:
        return
    backup = os.path.join(ROOT, 'state', f"preopen_bars_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    json.dump([old for old, _, _ in changes], open(backup, 'w'))
    with c:
        for old, new, _ in changes:
            c.execute('UPDATE candles SET open=?, high=?, low=?, close=? WHERE token=? AND interval=? AND start_ts=?',
                      (new['open'], new['high'], new['low'], new['close'], old['token'], old['interval'], old['start_ts']))
    print('repaired', len(changes), 'rows; backup', backup)


if __name__ == '__main__':
    main()
