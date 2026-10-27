"""Keep pre-open (09:00-09:08) index ticks out of the 09:15 candle. 28-Oct-2026.

Why: utils.candle_start() floors any tick before 09:15 to the 09:15 bar, and the engine
starts at 09:00, so NSE pre-open prices became the day's "open" and "high"
(27 Oct: open 22,985.95 / high 23,050.55, real open ~22,707). Every reader of the
day's open (forecast trend input, gap logic, trend-by-09:30, session high) was misled.

What it does (on the LIVE kadima/candles.py, anchored, idempotent):
  * CandleBuilder.add_tick returns early for ticks before MARKET_OPEN (09:15).
    Spot/tick handling elsewhere in the engine is untouched.
Then a self-test runs the patched CandleBuilder against a throw-away database.

    python3 tools/install_preopen_candle_fix_20261028.py --check   # dry run, no change
    python3 tools/install_preopen_candle_fix_20261028.py           # patch (backup kept)
    python3 tools/install_preopen_candle_fix_20261028.py --rollback
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.environ.get('KADIMA_ROOT', os.path.expanduser('~/kadima-py'))
F = os.path.join(ROOT, 'kadima', 'candles.py')
BAK = F + '.bak-preopen-20261028'
MARK = '# PREOPEN-FIX-20261028'
ANCHOR = '        if ltp is None:\n            return\n'
INSERT = ('        if (ts.hour, ts.minute) < MARKET_OPEN:   ' + MARK + '\n'
          '            return      # pre-open prices are not trades: never fold them into the 09:15 bar\n')
IMPORT_OLD = 'from .config import CANDLE_INTERVALS_MIN'
IMPORT_NEW = 'from .config import CANDLE_INTERVALS_MIN, MARKET_OPEN'

SELFTEST = r'''
import os, sys, tempfile
os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1])
from datetime import datetime
from kadima.config import IST
from kadima import candles, storage
b = candles.CandleBuilder('99926000')
d = lambda h, m, s=0: datetime(2026, 10, 27, h, m, s, tzinfo=IST)
for t, p in [(d(9, 0, 5), 22985.95), (d(9, 7, 0), 23050.55), (d(9, 15, 0), 22707.5),
             (d(9, 16, 0), 22690.0), (d(9, 21, 0), 22700.0), (d(9, 21, 30), 22702.0)]:
    b.add_tick(t, p, None)
r = {(x[0], x[1][11:16]): x[2:] for x in storage.conn().execute(
    'select interval, start_ts, open, high, low, close from candles order by interval, start_ts')}
ok = r[(5, '09:15')][0] == 22707.5 and r[(5, '09:15')][1] == 22707.5 and r[(1, '09:15')][0] == 22707.5
ok = ok and r[(5, '09:20')][0] == 22700.0 and not any(k[1] < '09:15' for k in r)
print('SELFTEST', 'OK' if ok else 'FAIL', r)
sys.exit(0 if ok else 1)
'''


def py():
    v = os.path.join(ROOT, 'venv', 'bin', 'python')
    return v if os.path.exists(v) else sys.executable


def selftest():
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as t:
        t.write(SELFTEST)
    return subprocess.call([py(), '-I', t.name, ROOT]) == 0


def main():
    args = sys.argv[1:]
    src = open(F).read()
    if '--rollback' in args:
        if not os.path.exists(BAK):
            sys.exit('no backup ' + BAK)
        shutil.copy2(BAK, F)
        print('rolled back', F)
        return
    if MARK in src:
        print('already installed')
        return
    if src.count(ANCHOR) != 1:
        sys.exit('ABORT: anchor not found exactly once in candles.py; nothing changed')
    if IMPORT_NEW not in src and src.count(IMPORT_OLD) != 1:
        sys.exit('ABORT: config import line not found; nothing changed')
    new = src.replace(ANCHOR, ANCHOR + INSERT, 1)
    if IMPORT_NEW not in new:
        new = new.replace(IMPORT_OLD, IMPORT_NEW, 1)
    if '--check' in args:
        print('check ok: anchor and import found; would insert pre-open guard')
        return
    shutil.copy2(F, BAK)
    tmp = F + '.new-preopen'
    open(tmp, 'w').write(new)
    if subprocess.call([py(), '-m', 'py_compile', tmp]) != 0:
        os.remove(tmp)
        sys.exit('ABORT: patched file does not compile; nothing changed')
    os.replace(tmp, F)
    if not selftest():
        shutil.copy2(BAK, F)
        sys.exit('ABORT: self-test failed; original restored')
    print('installed; backup at', BAK)


if __name__ == '__main__':
    main()
