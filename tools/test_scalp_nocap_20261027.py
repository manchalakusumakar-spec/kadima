"""SCALP NO-CAP v1 (27-Oct-2026): scalps size from all available cash, no 2-lot limit."""
import json, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_install_and_helpers():
    src = os.environ.get('KADIMA_LIVE_SRC')
    if not src:
        print('  (skipped: set KADIMA_LIVE_SRC to a live source copy)'); return
    tmp, data = tempfile.mkdtemp(), tempfile.mkdtemp()
    shutil.copytree(src, os.path.join(tmp, 'kadima'))
    inst = os.path.join(ROOT, 'tools', 'install_scalp_nocap_20261027.py')
    base = [sys.executable, inst, '--root', tmp, '--data', data]
    assert 'check ok' in subprocess.run(base + ['--check'], capture_output=True, text=True).stdout
    assert subprocess.run(base + ['--mode', 'live'], capture_output=True, text=True).returncode == 0
    assert subprocess.run(base, capture_output=True, text=True).returncode != 0      # idempotent
    code = f'''
import sys; sys.path.insert(0, {tmp!r})
import kadima.auto_executor as A, kadima.capital_guard as C
assert A._scalp_nocap() is True
C.broker_available_funds = lambda: 38487.16
alloc = A._scalp_nocap_allocation(30000.0)
assert abs(alloc - 38487.16 * 0.95) < 0.01, alloc
lots = int(alloc // (165.0 * 1.01 * 65))
assert lots == 3, lots          # 26-Oct: 3 lots of the 22450 CE instead of 1
C.broker_available_funds = lambda: None
assert A._scalp_nocap_allocation(30000.0) == 30000.0   # no funds reading: old band
print('helpers ok')
'''
    env = dict(os.environ, KADIMA_DATA_DIR=data)
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, cwd=tmp)
    assert 'helpers ok' in out.stdout, out.stderr[-800:]
    json.dump({'mode': 'off'}, open(os.path.join(data, 'scalp_capital.json'), 'w'))
    out = subprocess.run([sys.executable, '-c', f'import sys; sys.path.insert(0,{tmp!r}); import kadima.auto_executor as A; print(A._scalp_nocap())'],
                         capture_output=True, text=True, env=env, cwd=tmp)
    assert out.stdout.strip() == 'False', out.stderr[-400:]
    assert subprocess.run(base + ['--rollback'], capture_output=True, text=True).returncode == 0
    assert 'SCALP NO-CAP v1' not in open(os.path.join(tmp, 'kadima', 'auto_executor.py')).read()


if __name__ == '__main__':
    test_install_and_helpers(); print('ok test_install_and_helpers\n1 tests passed')
