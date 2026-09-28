"""Offline composed-runtime tests. Never mutates the checkout or imports host state."""
from pathlib import Path
import os
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent


def main():
    blob = lambda b: hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()
    manifest = json.loads((PACKAGE/'manifest.json').read_bytes())
    actual = {p.relative_to(PACKAGE/'runtime').as_posix() for p in (PACKAGE/'runtime').rglob('*.py')}
    assert actual == set(manifest['runtime']), 'runtime manifest coverage'
    for relative, pins in manifest['runtime'].items():
        assert blob((PACKAGE/'runtime'/relative).read_bytes()) == pins['new'], relative
        baseline = PACKAGE.parent/('mcx_live_update/auto_executor.py' if relative == 'kadima/auto_executor.py' else relative)
        assert (blob(baseline.read_bytes()) if baseline.exists() else None) == pins['old'], relative
    for relative, pin in manifest['scheduler'].items():
        assert blob((PACKAGE/'scheduler'/relative).read_bytes()) == pin, relative
    assert (PACKAGE/'scheduler/priority_sessions.py').read_bytes() == (PACKAGE/'runtime/kadima/priority_sessions.py').read_bytes()
    with tempfile.TemporaryDirectory(prefix='kadima-priority-tests-') as name:
        stage = Path(name)
        shutil.copytree(PACKAGE.parent/'kadima', stage/'kadima', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(PACKAGE/'runtime', stage, dirs_exist_ok=True)
        shutil.copytree(PACKAGE/'tests', stage/'tests')
        for name in ('test_execution_lifecycle.py', 'test_execution_integrations.py',
                     'test_celestial.py',
                     'test_trade_audit.py', 'test_trade_learning.py'):
            shutil.copyfile(PACKAGE.parent/'tests'/name, stage/'tests'/name)
        # These older fixtures predate the already-deployed session and shared
        # portfolio overlay. Supply the real dependency interfaces to their
        # broker stubs; all assertions and runtime functions stay unchanged.
        fixture = stage/'tests/test_execution_lifecycle.py'
        raw = fixture.read_text().replace("enforce=Mock(return_value=(1, 'ok')), record_realized_pnl=Mock())",
            "enforce=Mock(return_value=(1, 'ok')), record_realized_pnl=Mock(), "
            "broker_available_funds=Mock(return_value=10000), CAPITAL_SAFETY_BUFFER_PCT=5.0)")
        raw = raw.replace("chain=types.SimpleNamespace(),", "chain=types.SimpleNamespace(index=types.SimpleNamespace(opt_exchange='NFO')),")
        raw = raw.replace("chain=types.SimpleNamespace(_entry_guard=lambda:True)",
            "chain=types.SimpleNamespace(_entry_guard=lambda:True, index=types.SimpleNamespace(opt_exchange='NFO'))")
        fixture.write_text(raw)
        fixture = stage/'tests/test_celestial.py'
        fixture.write_text(fixture.read_text().replace("SimpleNamespace(),100,None,1,'gamma_blast'",
            "SimpleNamespace(index=SimpleNamespace(opt_exchange='NFO')),100,None,1,'gamma_blast'"))
        shutil.copytree(PACKAGE/'scheduler', stage/'scheduler')
        shutil.copyfile(PACKAGE/'install.py', stage/'install.py')
        shutil.copyfile(PACKAGE/'normalize_source_modes.py', stage/'normalize_source_modes.py')
        shutil.copyfile(PACKAGE/'probe_watchdog.py', stage/'probe_watchdog.py')
        # Exercise the immutable installer's real metadata-preserving file writer.
        shutil.copytree(PACKAGE.parent/'stock_fast_update', stage/'stock_fast_update',
                        ignore=shutil.ignore_patterns('__pycache__'))
        # Runtime dependencies already exist in the installed release.
        env = dict(os.environ, ORDER_PLACEMENT_ENABLED='0', KADIMA_CELESTIAL_ENABLED='0',
                   KADIMA_PRIORITY_TRADING='0', KADIMA_SESSION_AUTOTRADE='0',
                   KADIMA_PRICE_HISTORY_DISABLED='1', PYTHONPATH=str(stage), HOME=str(stage))
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                cwd=stage, env=env)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
