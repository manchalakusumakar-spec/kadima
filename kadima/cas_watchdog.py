"""CAS watchdog requests the engine's owned exit; it NEVER submits broker orders.

A dead/unresponsive engine is an escalation, not permission for a second writer.
The previous direct-CAS-SELL watchdog could race a pending engine SELL or close
manual quantity combined with CAS in the broker's same-contract position row.
"""
import json
import os
import urllib.request
from . import audit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def request_exit(signal_id):
    if type(signal_id) is not int or signal_id <= 0:
        return False
    token=os.environ.get('KADIMA_API_TOKEN')
    if not token:
        return False
    req=urllib.request.Request(f'http://127.0.0.1:8081/manual_exit/{signal_id}',
        data=b'{"reason":"cas_watchdog_eod"}', method='POST',
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(req,timeout=3) as response:
            raw=response.read(16385)
            if len(raw)>16384:
                return False
        body=json.loads(raw)
        return isinstance(body,dict) and body.get('ok') is True and body.get('signal_id')==signal_id
    except Exception as exc:
        audit.log('cas_watchdog_request_unconfirmed',signal_id=signal_id,error_type=type(exc).__name__)
        return False


def owned_positions():
    """Verified local live-fill quantity, not merely a token in a positions table."""
    from . import storage,execution_ledger
    from .angel_orders import CAS_KIND
    rows=storage.load_open_positions()
    result={}
    for row in rows:
        if row.get('kind')!=CAS_KIND:
            continue
        sid=row['signal_id']
        fills=execution_ledger.connection().execute(
            "SELECT e.side,f.quantity,f.dry_run FROM execution_fills f "
            "JOIN execution_orders e ON e.intent=f.intent WHERE e.signal_id=?",(sid,)).fetchall()
        if not fills or any(f[2] for f in fills):
            continue
        owned=sum(f[1] if f[0]=='BUY' else -f[1] for f in fills)
        key=(str(row['exchange']),str(row['token']))
        if key in result or owned<=0 or owned!=row.get('remaining_qty'):
            raise ValueError('cas_watchdog_ownership_ambiguous')
        result[key]=dict(signal_id=sid,quantity=owned,symbol=row['symbol'])
    return result
