"""BEST ENTRY v1 (26-Oct-2026): pullback entry, stop below pullback low, loss-side exit holds."""
import os, shutil, subprocess, sys, tempfile, time
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from kadima import best_entry as B

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 26, tzinfo=IST)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def cfg(**kw):
    v = dict(B.DEFAULTS); v['mode'] = 'live'; v.update(kw)
    B._cfg.update(at=time.monotonic() + 1e9, value=v)
    B._arms.clear(); B._done.clear(); B._persist.clear(); B._logged.clear()


def series(start, points, step=5):
    """points: list of premiums, one every `step` seconds from `start`."""
    return [(start + timedelta(seconds=i * step), p) for i, p in enumerate(points)]


def warmup(end, base=160.0, wiggle=4.0):
    """10 minutes of normal 1-min candles (range ~wiggle) ending at `end`."""
    out = []
    t = end - timedelta(minutes=10)
    i = 0
    while t < end:
        out.append((t, base + (wiggle if i % 2 else 0)))
        t += timedelta(seconds=30); i += 1
    return out


def go(sid, ltp, samples, now, **kw):
    return B.decide(kind=kw.get('kind', 'gamma_blast'), manual=kw.get('manual', False),
                    signal_id=sid, token='T1', ltp=ltp, stop=kw.get('stop', ltp * 0.95),
                    delta=kw.get('delta', 0.55), samples=samples, now=now)


def test_off_and_manual_pass_through():
    cfg(mode='off')
    s = warmup(at(10, 0))
    assert go(1, 182.1, s, at(10, 0))['go']
    cfg()
    assert go(1, 182.1, s, at(10, 0), manual=True)['go']
    assert go(1, 182.1, s, at(10, 0), kind='cas')['go']


def test_no_history_never_blocks():
    cfg()
    r = go(2, 182.1, [(at(9, 59, 50), 180.0)], at(10, 0))
    assert r['go'] and r['why'] == 'not_applied'


def test_deep_itm_noted_not_blocked():
    cfg()
    r = go(3, 182.1, warmup(at(10, 0)), at(10, 0), delta=0.86)
    assert r.get('block') == 'best_entry_waiting_pullback'   # normal flow, never skipped


def test_incident_replay_waits_then_buys_pullback_with_wider_stop():
    """22450 CE shape: signal at 182, dip to 170, bounce, then the big run."""
    cfg()
    s = warmup(at(10, 0))
    r = go(4, 182.1, s, at(10, 0))
    assert not r['go'] and r['block'] == 'best_entry_waiting_pullback'
    # dip
    s += series(at(10, 0, 5), [180, 176, 172, 170, 170.5])
    r = go(4, 170.0, s, at(10, 0, 20))
    assert not r['go'] and r['block'] == 'best_entry_waiting_bounce'
    # bounce off the low (higher low), not chasing
    s += series(at(10, 1, 5), [172, 173.5, 174])
    r = go(4, 174.0, s, at(10, 1, 20), stop=165.5)
    assert r['go'] and r['why'] == 'pullback_held' and r['fraction'] == 1.0
    assert r['stop'] < 170.0, r            # below the pullback low, not inside noise
    assert r['stop'] >= 174.0 * 0.65       # 35% hard cap
    # old entry 182.1 / exit 172.3 -> -5.4%; new entry 174 with stop below 170 survives 172.3
    assert 172.3 > r['stop']


def test_no_dip_half_size_after_wait_and_expiry():
    cfg()
    s = warmup(at(10, 0))
    assert not go(5, 182.0, s, at(10, 0))['go']
    s += series(at(10, 0, 5), [182.5, 183, 184, 185])
    assert not go(5, 185.0, s, at(10, 2))['go']            # under 3 min: wait
    r = go(5, 186.0, s, at(10, 3, 5))
    assert r['go'] and r['why'] == 'no_pullback_half_size' and r['fraction'] == 0.5
    assert B.scale_lots(4, r) == 2 and B.scale_lots(1, r) == 1 and B.scale_lots(3, r) == 1


def test_spike_waits_but_never_missed():
    cfg()
    s = warmup(at(10, 0))
    assert not go(6, 162.0, s, at(10, 0))['go']
    # one huge 1-min candle 10:02 (range 30 >> 2 x ATR), price at its top
    s += series(at(10, 2), [160, 175, 190])
    r = go(6, 190.0, s, at(10, 3, 30))
    assert not r['go'] and r['block'] == 'best_entry_spike_wait'
    r = go(6, 190.0, s, at(10, 6, 1))                  # deadline: buy half, never skip
    assert r['go'] and r['why'] == 'deadline_half_size' and r['fraction'] == 0.5
    assert r['stop'] >= 190.0 * 0.65


def test_entry_order_walked_up_not_cancelled():
    from unittest.mock import patch as mpatch
    from kadima import angel_orders
    cfg()
    B._reprice.clear()
    p = SimpleNamespace(signal_id=11, kind='gamma_blast', exchange='NFO', token='T1',
                        symbol='NIFTY27OCT2622450CE', remaining_qty=0, product='INTRADAY')
    row = {'intent': 'K1', 'quantity': 65}
    st = SimpleNamespace(order_id='O1', filled_qty=0, raw={'price': '180.0'})
    calls = []
    fake = lambda *a, **k: (calls.append(k['price']) or SimpleNamespace(ok=True, reason='ok'))
    with mpatch.object(angel_orders, 'modify_order', fake), \
         mpatch.object(angel_orders, 'product_for', lambda pos: 'INTRADAY'):
        assert B.entry_reprice(p, row, st, 10, lambda e, t: 182.0)
        assert calls == [183.8]                          # LTP + 1%
        B._reprice['K1']['last'] -= 10
        st.raw['price'] = '183.8'
        assert B.entry_reprice(p, row, st, 20, lambda e, t: 200.0)
        assert calls[-1] == 187.2                        # capped at first limit + 4%
        assert not B.entry_reprice(p, row, st, 95, lambda e, t: 185.0)   # window over: old cancel
        cfg(mode='shadow')
        assert not B.entry_reprice(p, row, st, 10, lambda e, t: 185.0)


def test_shadow_never_blocks():
    cfg(mode='shadow')
    s = warmup(at(10, 0))
    r = go(7, 182.0, s, at(10, 0))
    assert r['go'] and r['why'] == 'shadow'


def pos(**kw):
    p = dict(signal_id=9, kind='gamma_blast', status='open', entry_price=174.0,
             entry_ts=at(10, 1, 30), structural_stop=168.0, effective_stop=168.0, token='T1')
    p.update(kw)
    return SimpleNamespace(**p)


def test_exit_grace_holds_loss_but_not_hard_cap_or_profit():
    cfg()
    p = pos()
    assert B.filter_exit(p, 166.0, 'structural_stop_breached', at(10, 5), []) is None
    assert B.filter_exit(p, 166.0, 'opposite_gamma_confirmed', at(10, 5), []) is None
    assert B.filter_exit(p, 110.0, 'structural_stop_breached', at(10, 5), []) == 'structural_stop_breached'
    assert B.filter_exit(p, 180.0, 'dynamic_profit_trail_breached', at(10, 5), []) == 'dynamic_profit_trail_breached'
    for r in ('daily_loss_limit_breached', 'eod_force_close'):
        assert B.filter_exit(p, 166.0, r, at(10, 5), []) == r
    assert B.filter_exit(pos(kind='cas'), 166.0, 'x', at(10, 5), []) == 'x'


def test_exit_after_grace_needs_close_or_big_breach():
    cfg()
    p = pos()
    now = at(10, 20, 10)
    # completed 1-min candles closing above the stop, current tick dips just below
    s = [(at(10, 15) + timedelta(seconds=20 * i), 170.0 + (2 if i % 2 else 0)) for i in (0, 1, 2)]
    s += [(at(10, 16) + timedelta(seconds=20 * i), 170.0 + (2 if i % 2 else 0)) for i in range(12)]
    s.append((at(10, 20, 5), 167.5))
    assert B.filter_exit(p, 167.5, 'structural_stop_breached', now, s) is None
    # considerable breach on a tick (>= 0.5 ATR below the stop)
    assert B.filter_exit(p, 166.0, 'structural_stop_breached', now, s) == 'structural_stop_breached'
    # completed 1-min close below the stop
    s2 = s + [(at(10, 20, 10), 167.0), (at(10, 20, 50), 167.2)]
    assert B.filter_exit(p, 167.5, 'structural_stop_breached', at(10, 21, 5), s2) == 'structural_stop_breached'


def test_exit_signal_must_persist():
    cfg()
    p = pos()
    assert B.filter_exit(p, 170.0, 'opposite_gamma_confirmed', at(10, 20), []) is None
    assert B.filter_exit(p, 170.0, 'opposite_gamma_confirmed', at(10, 20, 30), []) is None
    assert B.filter_exit(p, 170.0, 'opposite_gamma_confirmed', at(10, 21, 0), []) == 'opposite_gamma_confirmed'
    # signal vanished for > 30 s -> timer restarts
    assert B.filter_exit(p, 170.0, 'opposite_gamma_confirmed', at(10, 25), []) is None


def test_errors_fail_to_old_behaviour():
    cfg()
    assert B.decide(kind='gamma_blast', manual=False, signal_id=1, token='T', ltp=100.0,
                    stop=90.0, delta=None, samples=[('bad', 'x')], now=at(10, 0))['go']
    assert B.filter_exit(pos(entry_ts='garbage'), 170.0, 'structural_stop_breached',
                         at(10, 20), [('bad',)]) == 'structural_stop_breached'


def test_installer_check_apply_rollback():
    tmp = tempfile.mkdtemp()
    shutil.copytree(os.path.join(ROOT, 'kadima'), os.path.join(tmp, 'kadima'))
    data = tempfile.mkdtemp()
    py, inst = sys.executable, os.path.join(ROOT, 'tools', 'install_best_entry_20261026.py')
    base = [py, inst, '--root', tmp, '--data', data]
    out = subprocess.run(base + ['--check'], capture_output=True, text=True)
    assert out.returncode == 0 and 'check ok' in out.stdout, out.stderr
    assert subprocess.run(base + ['--mode', 'live'], capture_output=True, text=True).returncode == 0
    ae = open(os.path.join(tmp, 'kadima', 'auto_executor.py')).read()
    assert ae.count('BEST ENTRY v1') >= 5
    assert 'BEST ENTRY v1' in open(os.path.join(tmp, 'kadima', 'execution_ledger.py')).read()
    again = subprocess.run(base, capture_output=True, text=True)
    assert again.returncode != 0 and 'already contains' in again.stderr   # idempotent
    assert subprocess.run(base + ['--rollback'], capture_output=True, text=True).returncode == 0
    assert 'BEST ENTRY v1' not in open(os.path.join(tmp, 'kadima', 'auto_executor.py')).read()


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn(); n += 1; print('ok', name)
    print(f'{n} tests passed')
