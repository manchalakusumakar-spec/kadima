"""Plan by default; install only source-matched audit changes, never arm trading.

The pinned installer supplies exposure/process checks, backups and restart rules.
Unknown installed source (including executor overlays) is refused and retained.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def digest(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


for relative, expected in {
    'stock_fast_update/install.py': '00ac2c1ba6b5a68a21cd2bf532dd53e1440e6e35',
    'mcx_live_update/install.py': '3d7042653f63c4857658f85cc6cd2e1551845324',
    'stock_fast_update/upgrade_dashboard.py': '3e72af124de85020e873614128f78d6dc080b185',
}.items():
    if digest((ROOT/relative).read_bytes()) != expected:
        raise RuntimeError('audit_installer_dependency_changed')

spec = importlib.util.spec_from_file_location('_audit_mcx_installer', ROOT/'mcx_live_update/install.py')
mcx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcx)
base = mcx.base
PREDECESSORS = json.loads((ROOT/'trade_audit_update/predecessors.json').read_text())
base.FILES = PREDECESSORS['backend']
_original_load_helper = base.load_helper
ui = _original_load_helper(ROOT/'stock_fast_update/upgrade_dashboard.py', base.UI_HELPER)
_original_verify_gateway = base.verify_gateway
_original_transaction = base.install_transaction


def page_with_stock(source):
    script = base.read_file(ROOT/'stock_fast_update/panel.js').decode()
    return ui.patch_dashboard(source, script)


def prepare_ui(current, kind):
    """Retain the reviewed stock panel/routes; unknown custom edits must be composed."""
    relative = 'dashboard_live/' + ('dashboard.html' if kind=='page' else 'gateway.py')
    incoming = base.read_file(ROOT/relative).decode()
    with_stock = page_with_stock(incoming) if kind=='page' else ui.patch_gateway(incoming)
    if current in (incoming, with_stock):
        return current
    variants = PREDECESSORS[kind]
    old_hash = digest(current.encode())
    if old_hash not in variants:
        raise base.Blocked('unreviewed_audit_'+kind+'_source_preserved')
    return with_stock if variants[old_hash] else incoming


def verify_gateway(current, reviewed):
    prepared = prepare_ui(current.decode() if isinstance(current, bytes) else current, 'gateway')
    _original_verify_gateway(prepared, reviewed)


def load_helper(path, expected):
    if Path(path) == ROOT/'stock_fast_update/upgrade_dashboard.py':
        # base.main calls this only after its process, paused and exposure checks.
        _original_load_helper(path, expected)
        return SimpleNamespace(patch_dashboard=lambda current, _script: prepare_ui(current, 'page'),
                               patch_gateway=lambda current: prepare_ui(current, 'gateway'))
    return _original_load_helper(path, expected)


def audit_local_api(path, token):
    """Read only the two new health routes without widening the pinned helper."""
    if path not in ('/api/kadima/audit?limit=1', '/api/kadima/prices?limit=1'):
        raise base.Blocked('unknown_audit_local_endpoint')
    request = urllib.request.Request('http://127.0.0.1:8081'+path,
                                     headers={'Authorization': 'Bearer '+token}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), base.NoRedirect())
    with opener.open(request, timeout=8) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise base.Blocked('oversize_api_response')
    return json.loads(raw)


def install_transaction(changes, units, backup, flat_check, source_check, import_check, post_check, **kwargs):
    def audit_post_check():
        post_check()
        token = json.loads(base.read_file(base.TOKEN_FILE))['api_token']
        data = audit_local_api('/api/kadima/audit?limit=1', token)
        if (not isinstance(data, dict) or not isinstance(data.get('rows'), list)
                or type(data.get('total')) is not int):
            raise base.Blocked('post_install_audit_api_invalid')
        print('AUDIT API: authenticated HTTP 200; plans and fills are separate.')
        prices = audit_local_api('/api/kadima/prices?limit=1', token)
        if (not isinstance(prices, dict) or not isinstance(prices.get('rows'), list)
                or type(prices.get('total')) is not int):
            raise base.Blocked('post_install_price_history_api_invalid')
        if prices.get('capture_enabled') is not True:
            raise base.Blocked('post_install_price_capture_disabled')
        print('PRICE HISTORY API: authenticated HTTP 200; capture requires subsequent fresh quotes.')
    return _original_transaction(changes, units, backup, flat_check, source_check,
                                 import_check, audit_post_check, **kwargs)


base.verify_gateway = verify_gateway
base.load_helper = load_helper
base.install_transaction = install_transaction


if __name__ == '__main__':
    try:
        import fcntl
        sys.dont_write_bytecode = True
        fd = os.open('/run/lock/kadima-activation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            base.main()
    except base.Blocked as exc:
        print('BLOCKED:', str(exc))
        raise SystemExit(1)
    except Exception as exc:
        print('BLOCKED:', type(exc).__name__, '(details suppressed)')
        raise SystemExit(1)
