"""Export scanner-selected contracts without placing orders or changing quotes."""
from collections import deque
from copy import copy
from datetime import datetime
import json
import math
import os
import time

from . import strike_selector
from .config import IST, MARKET_CLOSE
from .utils import now_ist


def premium_pace(chain, stamp):
    """Bounded per-contract history from existing successful polls only."""
    now=datetime.fromisoformat(stamp)
    if now.tzinfo is None or not 0 <= (now_ist()-now).total_seconds() <= 30:
        chain._display_premiums={}
        return {}
    histories=getattr(chain,'_display_premiums',{})
    retained={}
    metrics={}
    for q in chain.quotes.values():
        if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0
                   for v in (q.ltp,q.bid,q.ask)) or q.ask<q.bid:
            continue
        key=(chain.index.opt_exchange,chain._expiry,str(q.token),q.symbol)
        samples=histories.get(key,deque(maxlen=512))
        if samples:
            gap=(now-samples[-1][0]).total_seconds()
            if gap<0 or gap>90 or now.date()!=samples[-1][0].date():
                samples.clear()
            elif gap==0:
                retained[key]=samples
                continue
        samples.append((now,q.ltp))
        while len(samples)>1 and (now-samples[1][0]).total_seconds()>=300:
            samples.popleft()
        retained[key]=samples
        base=samples[0]
        span=(now-base[0]).total_seconds()
        if 300<=span<=390:
            move=100*(q.ltp/base[1]-1)
            metrics[str(q.token)]=dict(baseline_at=base[0].isoformat(),
                premium_move_pct=move,pace_status='ready' if move>0 else 'not_rising')
        else:
            metrics[str(q.token)]=dict(pace_status='warming_up')
    chain._display_premiums=retained
    return metrics


def selected_contract(chain, direction, spot, days, stamp):
    try:
        if not 0 <= time.monotonic()-chain._last_poll <= 30:
            return None, 'chain_stale'
        expiry=datetime.strptime(chain._expiry,'%d%b%Y').replace(
            tzinfo=IST,hour=MARKET_CLOSE[0],minute=MARKET_CLOSE[1])
        if expiry <= now_ist() or not math.isfinite(days) or days <= 0:
            return None, 'expired_contract'
        pace=premium_pace(chain,stamp)
        if direction not in ('CE', 'PE'):
            return None, 'direction_unconfirmed'
        view=copy(chain)
        view.quotes={k:copy(q) for k,q in chain.quotes.items()
                     if q.tradeable and all(isinstance(v,(int,float)) and math.isfinite(v) and v>0
                         for v in (q.ltp,q.bid,q.ask)) and q.ask>=q.bid}
        pick=strike_selector.best(view,direction,spot,days_to_expiry=days)
        if pick is None:
            return None, 'no_qualified_contract'
        return dict(pick.as_dict(),token=pick.quote.token,exchange=chain.index.opt_exchange,
                    expiry=expiry.date().isoformat(),updated_at=stamp,
                    **pace.get(str(pick.quote.token),{'pace_status':'warming_up'})), 'selected'
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
