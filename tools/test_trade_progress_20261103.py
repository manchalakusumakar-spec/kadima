"""TRADE PROGRESS v1 (03-Nov-2026): live % achieved / remaining for open trades (display only)."""
import os, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
SRC = os.environ.get('KADIMA_LIVE_SRC')          # a live source copy (needs config.py)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))


def _mod():
    tmp = tempfile.mkdtemp()
    shutil.copytree(SRC, os.path.join(tmp, 'kadima'))
    shutil.copy(os.path.join(ROOT, 'kadima', 'trade_progress.py'), os.path.join(tmp, 'kadima'))
    os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
    sys.path.insert(0, tmp)
    from kadima import trade_progress as T, live_forecast as L
    return T, L


def run():
    if not SRC:
        print('skipped: set KADIMA_LIVE_SRC'); return
    T, L = _mod()
    now = datetime(2026, 10, 26, 9, 53, 30, tzinfo=IST)
    days = (datetime(2026, 10, 27, 15, 30, tzinfo=IST) - now).total_seconds() / 86400
    assert T.parse_symbol('NIFTY27OCT2622450CE')[2:] == (22450.0, 'CE')
    assert T.parse_symbol('M&M27OCT263400PE')[0] == 'M&M'
    assert T.parse_symbol('garbage') is None
    L.LIVE['NIFTY'] = {'pulse': {'line': [{'ts': (now + timedelta(minutes=m)).isoformat(), 'v': 22540 + 8 * m}
                                          for m in range(16)]}}
    r = T.project(entry=165.0, ltp=212.9, peak=212.9, stop=143.5, spot=22540, strike=22450, side='CE',
                  days=days, index='NIFTY', now=now)
    assert r['basis'] == 'forecast' and 250 < r['target'] < 340, r
    assert abs(r['achieved_pct'] + r['remaining_pct'] - 100) < 0.2 and r['status'] == 'BUILDING'
    # forecast says no more upside -> target collapses to the peak: 100% achieved
    L.LIVE['NIFTY'] = {'pulse': {'line': [{'ts': (now + timedelta(minutes=m)).isoformat(), 'v': 22540 - 5 * m}
                                          for m in range(16)]}}
    r = T.project(entry=165.0, ltp=300.0, peak=300.0, stop=143.5, spot=22540, strike=22450, side='CE',
                  days=days, index='NIFTY', now=now)
    assert r['target'] == 300.0 and r['achieved_pct'] == 100.0 and r['status'] == 'NEAR TARGET'
    # peak above the forecast target -> BEYOND FORECAST, target lifted to the peak
    r = T.project(entry=165.0, ltp=290.0, peak=330.0, stop=143.5, spot=22540, strike=22450, side='CE',
                  days=days, index='NIFTY', now=now)
    assert r['target'] >= 330.0 and r['status'] == 'BEYOND FORECAST'
    # PE below entry near the stop; no forecast for the index -> IV basis
    L.LIVE.clear()
    r = T.project(entry=81.63, ltp=60.75, peak=88.75, stop=56.0, spot=22740, strike=22650, side='PE',
                  days=days, index='NIFTY', now=now)
    assert r['basis'] == 'iv' and r['status'] == 'NEAR STOP' and 80 < r['stop_used_pct'] < 83
    # installer check / apply / idempotent / rollback on a copy
    web = tempfile.mkdtemp(); shutil.copy(os.environ['KADIMA_LIVE_WEB'], os.path.join(web, 'dashboard.html'))
    tmp = tempfile.mkdtemp(); shutil.copytree(SRC, os.path.join(tmp, 'kadima'))
    inst = os.path.join(ROOT, 'tools', 'install_trade_progress_20261103.py')
    base = [sys.executable, inst, '--root', tmp, '--web', web]
    assert 'check ok' in subprocess.run(base + ['--check'], capture_output=True, text=True).stdout
    assert subprocess.run(base, capture_output=True, text=True).returncode == 0
    d = open(os.path.join(web, 'dashboard.html')).read()
    assert 'renderTradeProgress' in d and d.count('</body>') == 1
    assert subprocess.run(base, capture_output=True, text=True).returncode != 0
    assert subprocess.run(base + ['--rollback'], capture_output=True, text=True).returncode == 0
    assert 'TRADE PROGRESS v1' not in open(os.path.join(tmp, 'kadima', 'control_api.py')).read()
    print('7 checks passed')


if __name__ == '__main__':
    run()
