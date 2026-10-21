"""Hold winners / cut losers (21-Oct-2026): rule unit tests + a read-only replay
of a synthetic day shaped like 21-Oct (with-trend PE sold at +4%, counter-trend CE)."""
import os, sqlite3, subprocess, sys, tempfile
DATA = os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from datetime import datetime, timedelta

from kadima import hold_or_cut as H
from kadima import storage
from kadima.config import IST

ATR = 10.0


def test_counter_trend_loser_cut_after_two_closes():
    s = H.new_state(1000.0)
    assert H.step(s, 'CE', 100, 98, 996, ATR, 'DOWN') == ('none', 'undecided')   # 0.4 ATR
    assert H.step(s, 'CE', 100, 97, 994, ATR, 'DOWN') == ('none', 'undecided')   # 1st close beyond 0.5
    assert H.step(s, 'CE', 100, 96, 993, ATR, 'DOWN') == ('exit', 'cut_counter_trend')


def test_with_trend_loser_needs_one_atr():
    s = H.new_state(1000.0)
    for spot in (994, 993):
        assert H.step(s, 'CE', 100, 97, spot, ATR, 'UP')[0] == 'none'
    H.step(s, 'CE', 100, 95, 989, ATR, 'UP')
    assert H.step(s, 'CE', 100, 94, 988, ATR, 'UP') == ('exit', 'cut_index_against')


def test_one_bad_close_resets():
    s = H.new_state(1000.0)
    H.step(s, 'CE', 100, 97, 994, ATR, 'DOWN')
    H.step(s, 'CE', 100, 99, 999, ATR, 'DOWN')
    assert H.step(s, 'CE', 100, 97, 994, ATR, 'DOWN')[0] == 'none'


def test_winner_holds_through_a_pause_then_exits_on_giveback():
    s = H.new_state(23000.0)
    assert H.step(s, 'PE', 185, 192, 22994, ATR, 'DOWN') == ('hold', H.SOFT_HOLD)   # +0.6 ATR, +3.8%
    assert s['phase'] == 'winner'
    for spot in (22960, 22940, 22950, 22945):          # best 6 ATR, bounce 1 ATR: hold
        assert H.step(s, 'PE', 185, 220, spot, ATR, 'DOWN')[0] == 'hold'
    H.step(s, 'PE', 185, 205, 22972, ATR, 'DOWN')       # gave back 3.2 ATR (needs 3)
    assert H.step(s, 'PE', 185, 204, 22973, ATR, 'DOWN') == ('exit', 'winner_index_gave_back')


def test_winner_breakeven_lock_and_trend_turn():
    s = H.new_state(23000.0)
    H.step(s, 'PE', 185, 192, 22990, ATR, 'DOWN')
    assert H.step(s, 'PE', 185, 186.5, 22992, ATR, 'DOWN') == ('exit', 'winner_breakeven_lock')
    s = H.new_state(23000.0)
    H.step(s, 'PE', 185, 192, 22990, ATR, 'DOWN')
    assert H.step(s, 'PE', 185, 200, 22985, ATR, 'UP') == ('exit', 'winner_trend_turned')


def test_counter_trend_never_becomes_winner_and_hard_cap():
    s = H.new_state(1000.0)
    assert H.step(s, 'CE', 100, 104, 1006, ATR, 'DOWN')[0] == 'none'
    assert s['phase'] == 'open'
    assert H.step(s, 'CE', 100, 65, None, None, 'DOWN') == ('exit', 'hard_cap')


def test_missing_index_data_keeps_existing_logic():
    s = H.new_state(1000.0)
    assert H.step(s, 'CE', 100, 101, None, ATR, 'UP') == ('none', 'no_index_data')


def test_fib_variant_gives_a_trending_winner_more_room():
    def run(variant):
        s = H.new_state(23000.0)
        H.step(s, 'PE', 185, 192, 22990, ATR, 'DOWN', variant=variant)
        H.step(s, 'PE', 185, 240, 22900, ATR, 'DOWN', variant=variant)      # best 100 pts
        H.step(s, 'PE', 185, 215, 22955, ATR, 'DOWN', variant=variant)      # gave back 55
        return H.step(s, 'PE', 185, 215, 22955, ATR, 'DOWN', variant=variant)[0]
    assert run('base') == 'exit'      # 55 > 50% of 100
    assert run('fib') == 'hold'       # 55 < 61.8% of 100


def test_oi_variant_tightens_at_the_wall_and_releases_once_through():
    s = H.new_state(23000.0)
    H.step(s, 'PE', 185, 192, 22990, ATR, 'DOWN', variant='oi', wall=22900)
    H.step(s, 'PE', 185, 240, 22900, ATR, 'DOWN', variant='oi', wall=22900)   # at the put wall
    H.step(s, 'PE', 185, 225, 22940, ATR, 'DOWN', variant='oi', wall=22900)   # 40 back, >38.2%
    assert H.step(s, 'PE', 185, 225, 22942, ATR, 'DOWN', variant='oi', wall=22900)[0] == 'hold'  # 42 pts from wall: not "at" it
    s = H.new_state(23000.0)
    H.step(s, 'PE', 185, 192, 22990, ATR, 'DOWN', variant='oi', wall=22900)
    H.step(s, 'PE', 185, 240, 22900, ATR, 'DOWN', variant='oi', wall=22900)
    H.step(s, 'PE', 185, 228, 22904, ATR, 'DOWN', variant='oi', wall=22900)
    assert s['giveback'] == 0
    s2 = H.new_state(23000.0)
    for spot in (22990, 22890, 22880, 22800):                                  # closes through the wall
        H.step(s2, 'PE', 185, 250, spot, ATR, 'DOWN', variant='oi', wall=22900)
    assert s2['through'] >= H.CONFIRM


def test_wall_from_picks_the_favourable_side():
    rows = [(22900, 'PE', 900), (22800, 'PE', 1500), (23000, 'PE', 3000),
            (23000, 'CE', 2000), (23100, 'CE', 2500), (22800, 'CE', 9000)]
    assert H.wall_from(rows, 22950, 'PE') == 22800
    assert H.wall_from(rows, 22950, 'CE') == 23100
    assert H.wall_from([], 22950, 'CE') is None


def test_shadow_logs_a_veto_and_never_trades():
    from types import SimpleNamespace
    from unittest.mock import patch
    from kadima import audit, celestial, trend_rules
    now = datetime(2026, 10, 21, 10, 5, 30, tzinfo=IST)
    pos = SimpleNamespace(signal_id=77, kind='gamma_blast', status='open', index='NIFTY',
                          direction='PE', symbol='X', entry_price=185.0)
    logged = []
    spot = {'v': 22990.0}
    with patch.object(celestial, 'health', lambda name, payload=None: dict(
                ts=now.isoformat(), spot_at=now.isoformat(), spot=spot['v'], vwap=None)), \
            patch.object(celestial, 'fresh', lambda *a, **k: True), \
            patch.object(trend_rules, 'inputs', lambda token, now: dict(atr=ATR)), \
            patch.object(trend_rules, 'day_trend', lambda *a, **k: 'DOWN'), \
            patch.object(audit, 'log', lambda e, **k: logged.append((e, k))):
        H.shadow(pos, 186.0, None, now)
        spot['v'] = 22980.0
        out = H.shadow(pos, 192.0, 'premium_below_completed_1m_ema9', now + timedelta(minutes=1))
    assert out['base'] == ('hold', H.SOFT_HOLD)
    verdicts = {(k['variant'], k['verdict']) for e, k in logged if e == 'hold_or_cut_shadow'}
    assert ('base', 'would_hold') in verdicts and ('oi', 'would_hold') in verdicts


# ---------------------------------------------------------------- replay
TOKEN = '99926000'
DAY = datetime(2026, 10, 21, tzinfo=IST).date()


def spot_at(m):
    return 22900 - 0.8 * m if m <= 150 else 22780 + (4 if m % 6 < 3 else -4)


def build_db():
    c = storage.conn()
    with c:
        for d in range(1, 11):                          # 10 prior sessions, ~1% range, smooth
            day = DAY - timedelta(days=d)
            t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
            for i in range(75):
                px = 22990 * (1 + 0.01 * (i / 75 - 0.5))
                c.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (TOKEN, 5, (t0 + timedelta(minutes=5 * i)).isoformat(), px, px + 2, px - 2, px, 0, None, 1))
        t0 = datetime(2026, 10, 21, 9, 15, tzinfo=IST)
        for m in range(375):
            px = spot_at(m)
            c.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (TOKEN, 1, (t0 + timedelta(minutes=m)).isoformat(), px, px + 1, px - 1, px, 0, None, 1))
        for b in range(75):
            seg = [spot_at(m) for m in range(5 * b, 5 * b + 5)]
            c.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (TOKEN, 5, (t0 + timedelta(minutes=5 * b)).isoformat(), seg[0], max(seg) + 1,
                       min(seg) - 1, seg[-1], 0, None, 1))
        for m in range(375):
            t = (t0 + timedelta(minutes=m, seconds=50)).isoformat()
            px = spot_at(m)
            c.execute('INSERT INTO oi_snapshots (ts,index_name,token,symbol,strike,opt_type,ltp) '
                      'VALUES (?,?,?,?,?,?,?)', (t, 'NIFTY', '1001', 'NIFTY27OCT2623050PE', 23050, 'PE',
                                                 185 + 0.6 * (spot_at(45) - px)))
            c.execute('INSERT INTO oi_snapshots (ts,index_name,token,symbol,strike,opt_type,ltp) '
                      'VALUES (?,?,?,?,?,?,?)', (t, 'NIFTY', '1002', 'NIFTY27OCT2622900CE', 22900, 'CE',
                                                 144 + 0.5 * (px - spot_at(75))))
        pos = "INSERT INTO positions (signal_id,kind,index_name,direction,symbol,token,exchange,entry_price," \
              "entry_ts,lots,lot_size,remaining_qty,status,exit_price,exit_ts,exit_reason) VALUES " \
              "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        for snap in range(0, 375, 3):                    # chain OI walls every 3 minutes
            t = (t0 + timedelta(minutes=snap, seconds=20)).isoformat()
            for k, typ, oi in ((22800, 'PE', 5000), (22700, 'PE', 3000), (23000, 'CE', 6000)):
                c.execute('INSERT INTO oi_snapshots (ts,index_name,token,symbol,strike,opt_type,ltp,oi) '
                          'VALUES (?,?,?,?,?,?,?,?)', (t, 'NIFTY', 'w', 'w', k, typ, 1, oi))
        c.execute(pos, (1, 'gamma_blast', 'NIFTY', 'PE', 'NIFTY27OCT2623050PE', '1001', 'NFO', 185.0,
                        (t0 + timedelta(minutes=45)).isoformat(), 1, 65, 0, 'closed', 192.4,
                        (t0 + timedelta(minutes=52)).isoformat(), 'premium_below_completed_1m_ema9'))
        c.execute(pos, (2, 'gamma_blast', 'NIFTY', 'CE', 'NIFTY27OCT2622900CE', '1002', 'NFO', 144.0,
                        (t0 + timedelta(minutes=75)).isoformat(), 1, 65, 0, 'closed', 126.0,
                        (t0 + timedelta(minutes=120)).isoformat(), 'structural_stop_breached'))
    c.close()
    storage._local.conn = None


def test_replay_holds_the_pe_and_cuts_the_ce_without_writing():
    build_db()
    db = os.path.join(DATA, 'kadima.db')
    before = os.path.getmtime(db), os.path.getsize(db)
    out = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'replay_hold_or_cut.py'), '2026-10-21'],
                         capture_output=True, text=True, env=dict(os.environ, KADIMA_DATA_DIR=DATA))
    print(out.stdout, out.stderr)
    assert out.returncode == 0
    lines = out.stdout.splitlines()
    i1 = next(i for i, l in enumerate(lines) if l.startswith('1 '))
    i2 = next(i for i, l in enumerate(lines) if l.startswith('2 '))
    pnl = lambda l: int(l.split(' P&L ')[1].split()[0])
    for v, line in zip(('base', 'fib', 'oi'), lines[i1 + 1:i1 + 4]):
        assert line.split()[0] == v, line
        assert pnl(line) > (192.4 - 185) * 65 * 5      # held the winner well past +4%
    for v, line in zip(('base', 'fib', 'oi'), lines[i2 + 1:i2 + 4]):
        assert 'cut_counter_trend' in line, line
        assert pnl(line) > (126 - 144) * 65             # cut sooner than the actual exit
    assert lines[-1].startswith('TOTAL')
    assert (os.path.getmtime(db), os.path.getsize(db)) == before
    assert not os.path.exists(os.path.join(DATA, 'audit', '2026-10-21.jsonl'))


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn(); print('PASS', name)
            except Exception as exc:  # noqa: BLE001
                failed += 1; print('FAIL', name, repr(exc))
    sys.exit(1 if failed else 0)
