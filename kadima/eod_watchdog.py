"""Independent timer: guarded engine exit request or exclusive offline fallback.

An alive but unresponsive engine is alerted, never raced by a second writer.
Manual positions are never submitted. Accepted orders await broker confirmation.
"""
import json
import os
import urllib.error
import urllib.request

from . import alerts, audit, execution_owner, owned_watchdog


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_engine():
    token = os.environ.get('KADIMA_API_TOKEN')
    if not token:
        raise ValueError('watchdog_api_auth_missing')
    request = urllib.request.Request('http://127.0.0.1:8081/api/kadima/watchdog',
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'},
        data=b'{}', method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=40) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('watchdog_response_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get('ok')) is not bool:
        raise ValueError('watchdog_response_invalid')
    return value


def main():
    try:
        try:
            with execution_owner.acquire():
                from .auto_executor import AutoExecutor
                executor = AutoExecutor()
                owned_watchdog.restore(executor)
                report = dict(mode='exclusive_fallback', **owned_watchdog.cycle(executor))
        except BlockingIOError:
            report = dict(mode='engine_owned', **request_engine())
        audit.log('owned_watchdog_check', **report)
        if report.get('due_owned_positions') or report.get('ok') is not True:
            alerts.send('EOD watchdog: owned exposure needs attention; exit requests are not fill confirmation.', bypass=True)
        print(json.dumps(report, sort_keys=True))
        return 0 if report.get('ok') is True else 1
    except Exception as exc:
        # Preserve the blocked result, but distinguish authentication, route
        # and reconciliation failures without recording bodies or headers.
        diagnostic = dict(error_type=type(exc).__name__)
        if isinstance(exc, urllib.error.HTTPError) and type(exc.code) is int and 400 <= exc.code <= 599:
            diagnostic['http_status'] = exc.code
        audit.log('owned_watchdog_blocked', **diagnostic)
        alerts.send('EOD watchdog could not verify owned exposure. Check broker positions manually; no blind exit was sent.', bypass=True)
        print(json.dumps(dict(ok=False, state='blocked', **diagnostic)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
