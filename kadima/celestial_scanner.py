"""Bounded five-minute premium radar over actual successful option-chain polls.

Uses incremental volume rates, not cumulative day volume as a spike. Does not
claim that the rotating stock shortlist covers every option at every instant.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import math
import time
from . import celestial, storage
import json
from .utils import now_ist


@dataclass
class PremiumWindow:
    samples: deque = field(default_factory=lambda: deque(maxlen=80))

    def add(self, now, ltp, volume, oi):
        if not all(celestial.finite(v) for v in (ltp,volume,oi)) or ltp <= 0:
            self.samples.clear()
            return None
        if self.samples:
            last = self.samples[-1]
            gap = (now-last[0]).total_seconds()
            if gap == 0:
                return None
            if gap < 0 or gap > 90 or now.date() != last[0].date() or volume < last[2]:
                self.samples.clear()
        self.samples.append((now,ltp,volume,oi))
        while len(self.samples)>1 and (now-self.samples[1][0]).total_seconds() >= 300:
            self.samples.popleft()
        base = self.samples[0]
        span = (now-base[0]).total_seconds()
        if not 300 <= span <= 390:
            return None
        middle = next((s for s in reversed(self.samples) if (now-s[0]).total_seconds()>=60),None)
        if middle is None:
            return None
        recent_sec = (now-middle[0]).total_seconds()
        prior_sec = (middle[0]-base[0]).total_seconds()
        if prior_sec <= 0 or middle[2] <= base[2]:
            return None
        ratio = ((volume-middle[2])/recent_sec) / ((middle[2]-base[2])/prior_sec)
        return dict(premium_move_pct=100*(ltp/base[1]-1), volume_ratio=ratio,
                    volume_spike=ratio>=1.5, oi_change=oi-base[3], baseline_at=base[0].isoformat())


class Radar:
    def __init__(self):
        self.windows = {}
        self.detected = {}

    def publish_chain(self, chain, spot, spot_at, result, *, shield_score=None, shield_direction=None, vwap=None):
        now = now_ist()
        producer = chain.index.opt_exchange + ':' + chain.index.name
        if not 0 <= time.monotonic()-getattr(chain,'_last_poll',0) <= 30:
            celestial.publish(producer,[])
            return []
        if celestial.enabled():
            from .celestial_exit import publish_structure
            publish_structure(chain, spot, spot_at, result, vwap=vwap,
                              shield_score=shield_score, shield_direction=shield_direction)
        expiry_string = chain.scrips.nearest_expiry(chain.index)
        expiry = datetime.strptime(expiry_string,'%d%b%Y').date()
        # Confirm current master membership; tokens/symbols are never manufactured.
        contracts = {str(c['token']):c for c in chain.scrips.strikes_around(chain.index,chain.atm,1)}
        rows = []
        current_keys = set()
        for q in chain.quotes.values():
            key = producer+':'+str(q.token)
            current_keys.add(key)
            window = self.windows.setdefault(key,PremiumWindow())
            metrics = window.add(now,q.ltp,q.volume,q.oi)
            if not metrics or metrics['premium_move_pct'] < 5 or q.token not in contracts:
                self.detected.pop(key,None)
                continue
            contract = contracts[q.token]
            if contract['symbol'] != q.symbol or expiry < now.date():
                continue
            direction_clear = getattr(result,'direction',None) == q.opt_type
            # Require the scorer's directional OI evidence, not just an OI increase.
            components = getattr(result,'components',None)
            oi_support = direction_clear and bool(getattr(components,'oi_velocity',None))
            clean = all(celestial.finite(v,.000001) for v in (q.bid,q.ask)) and q.ask >= q.bid
            spread = (100*(q.ask-q.bid)/((q.ask+q.bid)/2)) if clean else None
            is_itm = q.strike < spot if q.opt_type=='CE' else q.strike > spot
            is_atm = q.strike == chain.atm
            otm_allowed = getattr(result,'score',0)>=90 and metrics['premium_move_pct']>=15
            tradeable = bool(q.tradeable and clean and spread<=1.5 and (is_atm or is_itm or otm_allowed))
            row = dict(id=chain.index.opt_exchange+':'+q.token,token=q.token,exchange=chain.index.opt_exchange,
                symbol=q.symbol,underlying=chain.index.name,direction=q.opt_type,best_strike=q.strike,
                option_ltp=q.ltp,expiry=expiry.isoformat(),
                priority=1 if expiry==now.date() else 2 if chain.index.weight>0 else 3,
                gamma_score=float(getattr(result,'score',0)),
                bull_shield_score=shield_score if shield_direction=='CE' else None,
                bear_shield_score=shield_score if shield_direction=='PE' else None,
                oi_support=bool(oi_support),direction_clear=direction_clear,
                spread_status='CLEAN' if clean and spread<=1.5 else 'WIDE' if clean else 'UNKNOWN',
                tradeable=tradeable,last_updated=now.isoformat(),spot_at=spot_at,
                sector_score=0,event_risk_penalty=0,**metrics)
            rows.append(row)
            # Persist observed strike acceleration independently of entry gates.
            # Both broker timestamps and the underlying must be current.
            from .cas_blast_scanner import source_time
            feed=source_time(getattr(q,'exchange_feed_time',None))
            trade=source_time(getattr(q,'exchange_trade_time',None))
            observed=bool(metrics['volume_spike'] and metrics['oi_change']!=0 and clean and spread<=1.5
                and feed and trade and 0<=(now-feed).total_seconds()<=45
                and 0<=(now-trade).total_seconds()<=90 and celestial.fresh(spot_at,now,30))
            if observed and self.detected.get(key)!=now.date():
                storage.insert_signal(chain.index.name,'DETECTED',row['gamma_score'],q.opt_type,
                    json.dumps(dict(row,execution='observation_only')),kind='gamma_strike')
                self.detected[key]=now.date()
            elif not observed:
                self.detected.pop(key,None)
        # Free demoted token histories, bound even across many rolling ladders.
        self.windows = {k:v for k,v in self.windows.items()
                        if v.samples and (now-v.samples[-1][0]).total_seconds()<=600
                        and (not k.startswith(producer+':') or k in current_keys)}
        if len(self.windows)>4096:
            self.windows = {k:self.windows[k] for k in sorted(self.windows)[-4096:]}
        self.detected={k:v for k,v in self.detected.items() if k in self.windows and v==now.date()}
        celestial.publish(producer,rows)
        return rows
