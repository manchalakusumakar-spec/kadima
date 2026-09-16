"""Closing-session acceleration watch, fed by successful engine chain polls.

Observed 2-minute premium change and incremental volume rate versus the prior
18 minutes. Warm from 14:50 IST, trigger from 15:15 to configured market close.
Existing index ladders only; missing/out-of-ladder contracts are not covered.
No entry, exit, sizing or broker request is performed here.
"""
from collections import deque
from datetime import datetime, time as wall_time, timedelta
import json
import math
import time

from . import alerts, audit, celestial, storage
from .config import IST, MARKET_CLOSE
from .greeks import implied_vol_and_greeks
from .utils import now_ist


def finite(value):
    return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)


def source_time(raw):
    try:
        value = datetime.strptime(raw,'%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
        return value if value.year >= 2000 else None
    except (ValueError,TypeError):
        return None


class StrikeWindow:
    def __init__(self):
        self.samples = deque(maxlen=160)
        self.triggered = False
        self.last_alert = None

    def add(self, stamp, price, volume):
        if not finite(price) or price <= 0 or not finite(volume) or volume < 0:
            self.samples.clear(); self.triggered=False
            return None
        if self.samples:
            last=self.samples[-1]
            gap=(stamp-last[0]).total_seconds()
            if gap == 0:
                return None
            if gap < 0 or gap > 60 or stamp.date()!=last[0].date() or volume < last[2]:
                self.samples.clear(); self.triggered=False
        self.samples.append((stamp,price,volume))
        while len(self.samples)>1 and (stamp-self.samples[1][0]).total_seconds()>=1200:
            self.samples.popleft()
        base=self.samples[0]
        if not 1200 <= (stamp-base[0]).total_seconds() <= 1260:
            return None
        middle=next((v for v in reversed(self.samples) if (stamp-v[0]).total_seconds()>=120),None)
        if middle is None or (stamp-middle[0]).total_seconds()>180:
            return None
        previous_volume=middle[2]-base[2]
        if previous_volume<=0:
            return None
        recent_rate=(volume-middle[2])/(stamp-middle[0]).total_seconds()
        baseline_rate=previous_volume/(middle[0]-base[0]).total_seconds()
        return dict(premium_change_pct=100*(price/middle[1]-1),
                    volume_ratio=recent_rate/baseline_rate,
                    baseline_at=base[0].isoformat(),recent_at=middle[0].isoformat())


class CasBlastScanner:
    def __init__(self):
        self.windows={}
        self.coverage={}

    def _report(self):
        celestial.health('cas_detection',dict(ok=bool(self.coverage) and all(v.get('status')=='observing' and v.get('fresh_contracts')==v.get('ladder_contracts') and celestial.fresh(v.get('sample_at'),now_ist(),60) for v in self.coverage.values()),indices=dict(self.coverage),
            scope='existing_index_ladders_only',execution='observation_only',
            note='20-minute warmup; source gaps reset baselines. Out-of-ladder strikes are not covered.'))

    def observe(self, chain, spot, now=None, *, spot_at=None):
        now=(now or now_ist()).astimezone(IST)
        name=chain.index.name
        if now.weekday()>=5 or not wall_time(14,50)<=now.time()<wall_time(*MARKET_CLOSE):
            self.windows.clear()
            self.coverage[name]=dict(status='outside_window',sample_at=now.isoformat())
            self._report()
            return []
        if not finite(spot) or spot<=0 or not 0<=time.monotonic()-getattr(chain,'_last_poll',0)<=30:
            self.coverage[name]=dict(status='stale_chain',sample_at=now.isoformat())
            self._report()
            return []
        try:
            expiry=datetime.strptime(chain._expiry,'%d%b%Y').replace(tzinfo=IST,
                hour=MARKET_CLOSE[0],minute=MARKET_CLOSE[1])
        except (ValueError,TypeError):
            self.coverage[name]=dict(status='expiry_unavailable',sample_at=now.isoformat())
            self._report()
            return []
        if not celestial.fresh(spot_at,now,30):
            self.coverage[name]=dict(status='underlying_stale',sample_at=now.isoformat())
            self._report()
            return []
        days=(expiry-now).total_seconds()/86400
        if days<=0:
            self.coverage[name]=dict(status='contract_expired',sample_at=now.isoformat())
            self._report()
            return []
        found=[]; fresh_count=warmed=band=0
        for q in chain.quotes.values():
            stamp=source_time(getattr(q,'exchange_feed_time',None))
            trade=source_time(getattr(q,'exchange_trade_time',None))
            key=chain.index.opt_exchange+':'+q.token
            if stamp is None or trade is None or not 0<=(now-stamp).total_seconds()<=45 or not 0<=(now-trade).total_seconds()<=90:
                continue
            fresh_count+=1
            hist=self.windows.setdefault(key,StrikeWindow())
            metrics=hist.add(stamp,q.ltp,q.volume)
            if not metrics:
                continue
            warmed+=1
            iv,g=implied_vol_and_greeks(spot,q.strike,q.ltp,days,q.opt_type)
            delta=(g or {}).get('delta')
            in_band=finite(delta) and .05<=abs(delta)<=.20
            band+=int(in_band)
            clean=finite(q.bid) and finite(q.ask) and 0<q.bid<=q.ask
            spread=100*(q.ask-q.bid)/((q.ask+q.bid)/2) if clean else None
            qualified=in_band and metrics['premium_change_pct']>=40 and metrics['volume_ratio']>=3
            if not qualified:
                hist.triggered=False
                continue
            if now.time()<wall_time(15,15) or hist.triggered or (hist.last_alert and (now-hist.last_alert).total_seconds()<120):
                continue
            row=dict(underlying=name,symbol=q.symbol,token=q.token,exchange=chain.index.opt_exchange,
                strike=q.strike,direction=q.opt_type,ltp=q.ltp,delta=round(delta,4),
                delta_source='model_from_option_price',spread_pct=spread,
                quote_at=stamp.isoformat(),observed_at=now.isoformat(),
                execution='observation_only',**metrics)
            # Durable signal precedes notification; queue failure cannot erase it.
            sid=storage.insert_signal(name,'DETECTED',metrics['premium_change_pct'],q.opt_type,
                json.dumps(row),kind='cas_blast')
            row['signal_id']=sid
            hist.triggered=True;hist.last_alert=now
            audit.log('cas_blast_detected',signal_id=sid,symbol=q.symbol,**metrics)
            try:
                alerts.enqueue(f"CAS BLAST watch — {q.symbol}\nPremium +{metrics['premium_change_pct']:.1f}% · volume rate {metrics['volume_ratio']:.1f}x\nObservation only · signal #{sid}",key=f'cas:{sid}',bypass=True)
            except Exception:
                # A broken notification path must not skip remaining contracts.
                audit.log('cas_notification_failed',signal_id=sid)
            found.append(row)
        self.windows={k:v for k,v in self.windows.items() if v.samples and 0<=(now-v.samples[-1][0]).total_seconds()<=1260}
        if len(self.windows)>512:
            self.windows=dict(sorted(self.windows.items(),key=lambda kv:kv[1].samples[-1][0],reverse=True)[:512])
        self.coverage[name]=dict(status='observing' if warmed else 'warming_or_missing_source_time',
            sample_at=now.isoformat(),ladder_contracts=len(chain.quotes),fresh_contracts=fresh_count,
            warmed_contracts=warmed,far_otm_contracts=band)
        self._report()
        return found
