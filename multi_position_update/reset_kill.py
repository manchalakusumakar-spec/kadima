"""One-time operator-approved kill reset before the multi-position upgrade.

Only the existing guarded reset endpoint may clear the latch. Never writes a
trading database, releases the file kill switch, resumes auto or submits orders.
Run outside the arming window; install.py must perform its own exposure checks.
"""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import urllib.error
import urllib.request

ROOT = Path('/home/ubuntu/kadima-py')
AUTH = Path('/etc/kadima-dashboard/config.json')
IST = timezone(timedelta(hours=5, minutes=30))
EXPECTED = {
    'kadima/control_api.py': '6734f4e822b2c6cf14fae88539a4bfd87b7292fb',
    'kadima/celestial.py': 'dfac18b9d76f11428357cc7d6b4d8f88d15278fa',
    'kadima/execution_ledger.py': '1fde45163927f6fceff4a6ae8573e4242ac4fb18',
}
URLS = {'global': 'top20', 'stock': 'stock-fast', 'reset': 'reset-kill'}


class Blocked(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def outside_arming_window(at=None):
    at = (at or now()).astimezone(IST)
    # Five-minute buffers: no reset can race the normal 09:00 arming boundary.
    if at.weekday() < 5 and 8*60+55 <= at.hour*60+at.minute < 15*60+40:
        raise Blocked('reset_outside_scheduled_window_required')


def read_regular(path, limit=2_000_000):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path:
        raise Blocked('noncanonical_reset_input')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Blocked('regular_reset_input_required')
        raw = handle.read(limit+1)
    if len(raw) > limit:
        raise Blocked('reset_input_too_large')
    return raw


def verify_sources(root=ROOT):
    for relative, expected in EXPECTED.items():
        raw = read_regular(root/relative)
        actual = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if actual != expected:
            raise Blocked('reviewed_reset_handler_required')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        token = json.loads(read_regular(AUTH, 65536)).get('api_token')
        if not isinstance(token, str) or not 0 < len(token) < 8192:
            raise Blocked('local_auth_unavailable')
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, action):
        if action not in URLS:
            raise Blocked('unsupported_reset_action')
        req = urllib.request.Request(
            'http://127.0.0.1:8081/api/kadima/'+URLS[action],
            headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'},
            data=b'{}' if action == 'reset' else None,
            method='POST' if action == 'reset' else 'GET')
        try:
            with self.opener.open(req, timeout=60 if action == 'reset' else 10) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except urllib.error.HTTPError as exc:
            detail = None
            if action == 'reset':
                try:
                    detail = json.loads(exc.read(10000)).get('detail')
                except (ValueError, OSError, AttributeError):
                    pass
            if detail in ('verified_flat_account_required', 'celestial_rollout_not_enabled',
                          'executor_not_registered'):
                raise Blocked(detail) from None
            raise Blocked(action+'_http_'+str(exc.code)) from None
        except (OSError, ValueError):
            # Never retry a write whose response may have been lost.
            reason = 'reset_result_unconfirmed_read_status_before_retry' if action == 'reset' else action+'_read_unavailable'
            raise Blocked(reason) from None


def snapshot(client):
    gd, sd = client.call('global'), client.call('stock')
    at = now()
    for data in (gd, sd):
        try:
            stamp = datetime.fromisoformat(data['ts'])
            if stamp.tzinfo is None or not 0 <= (at-stamp).total_seconds() <= 30:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise Blocked('fresh_control_snapshot_required') from None
    cfg, stock = gd.get('config'), sd.get('controls')
    if not isinstance(cfg, dict) or not isinstance(stock, dict):
        raise Blocked('invalid_reset_control_schema')
    if (cfg.get('auto_trade_enabled') is not False or stock.get('live_enabled') is not False
            or sd.get('global_auto_requested') is not False):
        raise Blocked('both_auto_switches_must_remain_paused')
    if (cfg.get('rollout_enabled') is not True or sd.get('celestial_rollout') is not True
            or cfg.get('broker_orders_enabled') is not True or sd.get('broker_order_permission') is not True):
        raise Blocked('reviewed_runtime_permissions_required')
    if type(cfg.get('kill_latched')) is not bool or cfg['kill_latched'] is not sd.get('kill_latched'):
        raise Blocked('kill_status_unknown_or_inconsistent')
    for value in (cfg, stock):
        if type(value.get('revision')) is not int or value['revision'] < 0:
            raise Blocked('valid_control_revisions_required')
    if gd.get('positions') != [] or sd.get('positions') != []:
        raise Blocked('visible_positions_or_unknown_state_require_review')
    return cfg, stock


def reset_once(client, apply=False):
    outside_arming_window()
    cfg, stock = snapshot(client)
    if cfg['kill_latched'] is False:
        return 'ALREADY CLEAR: no reset sent; global auto and stock live remain OFF.'
    if not apply:
        return 'PLAN ONLY: guarded reset available; no control write performed.'
    outside_arming_window()
    # The endpoint itself reconciles broker books and verifies no local/pending exposure.
    result = client.call('reset')
    if (result.get('kill_latched') is not False or result.get('auto_trade_enabled') is not False
            or result.get('revision') != cfg['revision']+1):
        raise Blocked('reset_response_not_confirmed')
    after, after_stock = snapshot(client)
    expected = dict(cfg, kill_latched=False, auto_trade_enabled=False, revision=cfg['revision']+1)
    if after != expected or after_stock != stock:
        raise Blocked('post_reset_controls_changed_do_not_upgrade')
    outside_arming_window()
    return 'RESET VERIFIED: broker-flat reset accepted; global auto and stock live remain OFF.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Send one operator-approved guarded reset')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Blocked('sudo_required')
    outside_arming_window()
    handles = []
    try:
        for name in ('kadima-stock-fast-install', 'kadima-activation', 'kadima-session-auto-install',
                     'kadima-multi-position-install', 'kadima-kill-reset'):
            fd = os.open('/run/lock/'+name+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, 'w'); handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('another_maintenance_action_is_running') from None
        verify_sources()
        print(reset_once(Client(), args.apply))
        print('LOT LIMIT, RISK SETTINGS, FILE KILL AND SCHEDULE STATE: unchanged.')
    finally:
        for handle in reversed(handles):
            handle.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED:', str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        print('No reset retry or auto resume performed. Inspect the result before continuing.')
        raise SystemExit(1)
