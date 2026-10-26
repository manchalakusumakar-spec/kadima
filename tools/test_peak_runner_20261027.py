"""PEAK RUNNER v1 (27-Oct-2026): scalps run past +20% and exit on a 15-s peak forecast."""
import os, shutil, subprocess, sys, tempfile, time
os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from kadima import scalp_runner as R


def cfg(**kw):
    v = dict(R.DEFAULTS, mode='live'); v.update(kw)
    R._cfg.update(at=time.monotonic() + 1e9, value=v); R._state.clear()


def run(path, entry=100.0, sid=1, dt=2.0):
    peak, stop = entry, entry * 0.87
    for i, px in enumerate(path):
        peak = max(peak, px)
        stop, reason, handled = R.step(sid, entry, peak, px, stop, now=i * dt)
        if reason:
            return i, px, reason, stop
    return None, None, None, stop


def test_no_target_exit_while_rising():
    cfg()
    i, px, reason, stop = run([100 + k for k in range(60)])          # +59% straight up
    assert reason is None and stop >= 120.0                            # floor locked at +20%


def test_exits_after_the_peak_turns():
    cfg()
    up = [100 + 2 * k for k in range(40)]                              # to 178
    down = [178 - 1.5 * k for k in range(1, 30)]
    i, px, reason, _ = run(up + down)
    assert reason == 'scalp_runner_peak_forecast' and px > 160, (i, px, reason)


def test_single_dip_is_not_a_peak():
    cfg()
    path = [100 + 2 * k for k in range(30)] + [156, 158] + [158 + 2 * k for k in range(1, 20)]
    assert run(path)[2] is None


def test_floor_exit_at_plus_20():
    cfg()
    path = [100, 110, 121, 122, 121, 119.5]
    assert run(path)[2] == 'scalp_runner_floor'


def test_shadow_and_off_keep_old_target():
    cfg(mode='shadow')
    assert R.step(1, 100.0, 125.0, 125.0, 90.0, now=0)[2] is False
    cfg(mode='off')
    assert R.step(1, 100.0, 125.0, 125.0, 90.0, now=0)[2] is False


def test_installer_check_apply_rollback():
    src = os.environ.get('KADIMA_LIVE_SRC')
    if not src:
        print('  (skipped: set KADIMA_LIVE_SRC to a live source copy)'); return
    tmp, data = tempfile.mkdtemp(), tempfile.mkdtemp()
    shutil.copytree(src, os.path.join(tmp, 'kadima'))
    inst = os.path.join(ROOT, 'tools', 'install_peak_runner_20261027.py')
    base = [sys.executable, inst, '--root', tmp, '--data', data]
    assert 'check ok' in subprocess.run(base + ['--check'], capture_output=True, text=True).stdout
    assert subprocess.run(base + ['--mode', 'live'], capture_output=True, text=True).returncode == 0
    assert 'PEAK RUNNER v1' in open(os.path.join(tmp, 'kadima', 'scalp_v2.py')).read()
    assert subprocess.run(base, capture_output=True, text=True).returncode != 0
    assert subprocess.run(base + ['--rollback'], capture_output=True, text=True).returncode == 0
    assert 'PEAK RUNNER v1' not in open(os.path.join(tmp, 'kadima', 'scalp_v2.py')).read()


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn(); n += 1; print('ok', name)
    print(f'{n} tests passed')
