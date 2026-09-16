"""Export scanner-selected contracts without placing orders or changing quotes."""
from copy import copy
from datetime import datetime
import json
import math
import os
import time

from . import strike_selector
from .config import IST, MARKET_CLOSE
from .utils import now_ist


def selected_contract(chain, direction, spot, days, stamp):
    if direction not in ('CE', 'PE'):
        return None, 'direction_unconfirmed'
    try:
        if not 0 <= time.monotonic()-chain._last_poll <= 30:
            return None, 'chain_stale'
        expiry=datetime.strptime(chain._expiry,'%d%b%Y').replace(
            tzinfo=IST,hour=MARKET_CLOSE[0],minute=MARKET_CLOSE[1])
        if expiry <= now_ist() or not math.isfinite(days) or days <= 0:
            return None, 'expired_contract'
        view=copy(chain)
        view.quotes={k:copy(q) for k,q in chain.quotes.items()
                     if q.tradeable and all(isinstance(v,(int,float)) and math.isfinite(v) and v>0
                         for v in (q.ltp,q.bid,q.ask)) and q.ask>=q.bid}
        pick=strike_selector.best(view,direction,spot,days_to_expiry=days)
        if pick is None:
            return None, 'no_qualified_contract'
        return dict(pick.as_dict(),token=pick.quote.token,exchange=chain.index.opt_exchange,
                    expiry=expiry.date().isoformat(),updated_at=stamp), 'selected'
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None, 'contract_data_unavailable'


def write_snapshot(payload):
    """Same authenticated universe route serves both scanner modes."""
    path=os.path.expanduser('~/kadima-py/state/universe_snapshot.json')
    os.makedirs(os.path.dirname(path),exist_ok=True)
    temporary=path+'.tmp'
    with open(temporary,'w') as handle:
        json.dump(payload,handle,allow_nan=False)
    os.replace(temporary,path)
