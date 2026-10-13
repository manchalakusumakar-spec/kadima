"""Forecast-only wide/EOD chains, collected opportunistically in a sibling thread.

Reuses the engine master, spot and authenticated SDK instance. Never logs in,
never changes an OptionChain, and never queues ahead of normal quote work.
"""
from datetime import datetime, time as clock, timedelta
import math
import threading

from . import audit
from .config import IST
from .utils import now_ist

SCHEMA = '''
CREATE TABLE IF NOT EXISTS chain_wide (
 index_name TEXT NOT NULL, ts TEXT NOT NULL, expiry TEXT NOT NULL,
 strike REAL NOT NULL, opt_type TEXT NOT NULL, ltp REAL NOT NULL,
 oi INTEGER NOT NULL, volume INTEGER NOT NULL, iv REAL, spot REAL NOT NULL,
 PRIMARY KEY(index_name,ts,strike,opt_type));
CREATE INDEX IF NOT EXISTS chain_wide_time ON chain_wide(index_name,ts);
CREATE TABLE IF NOT EXISTS chain_eod (
 index_name TEXT NOT NULL, day TEXT NOT NULL, expiry TEXT NOT NULL,
 strike REAL NOT NULL, opt_type TEXT NOT NULL, ltp REAL NOT NULL,
 oi INTEGER NOT NULL, oi_change INTEGER, volume INTEGER NOT NULL, spot REAL NOT NULL,
 PRIMARY KEY(index_name,day,expiry,strike,opt_type));
CREATE TABLE IF NOT EXISTS bhav_options (
 index_name TEXT NOT NULL, day TEXT NOT NULL, expiry TEXT NOT NULL,
 strike REAL NOT NULL, opt_type TEXT NOT NULL, close REAL NOT NULL,
 settle REAL, oi INTEGER NOT NULL, oi_change INTEGER, contracts INTEGER,
 PRIMARY KEY(index_name,day,expiry,strike,opt_type));
CREATE TABLE IF NOT EXISTS nightly_status (
 step TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
 rows_saved INTEGER NOT NULL, checked_at TEXT NOT NULL, detail TEXT NOT NULL,
 PRIMARY KEY(step,day));
'''


def aggregate(rows):
    ce = [r for r in rows if r['opt_type']=='CE' and r['oi']>0]
    pe = [r for r in rows if r['opt_type']=='PE' and r['oi']>0]
    if not ce or not pe:
        return None
    sce,spe = sum(r['oi'] for r in ce),sum(r['oi'] for r in pe)
    return dict(pcr=spe/sce,ce_wall=max(ce,key=lambda r:r['oi'])['strike'],
                pe_wall=max(pe,key=lambda r:r['oi'])['strike'],ce_oi=sce,pe_oi=spe)


def wide_at(c, name, point):
    # Strictly before the decision: a sample labelled t0 is not input to t0.
    row=c.execute('SELECT ts FROM chain_wide WHERE index_name=? AND ts<? ORDER BY ts DESC LIMIT 1',
                  (name,point.isoformat())).fetchone()
    if not row or not 0 < (point-datetime.fromisoformat(row['ts'])).total_seconds() <= 300:
        return []
    return [dict(r) for r in c.execute('SELECT * FROM chain_wide WHERE index_name=? AND ts=?', (name,row['ts']))]


def max_pain(rows):
    if not rows:
        return None
    strikes=sorted({r['strike'] for r in rows})
    return min(strikes,key=lambda settlement:sum(r['oi'] *
        (max(0,settlement-r['strike']) if r['opt_type']=='CE' else max(0,r['strike']-settlement)) for r in rows))


def previous_chain(c,name,t0):
    """Latest observed prior session, preferring official bhavcopy for that day.

    Never jump back to an older official day when a newer broker EOD exists.
    ISO expiry ordering selects the nearest nonexpired expiry for that session.
    """
    cutoff=t0.astimezone(IST).date().isoformat()
    from . import market_session_gate
    try:
        policy=market_session_gate.validate_policy(market_session_gate.read_json(market_session_gate.POLICY))
        holidays=set(policy['holidays'])
    except (OSError,ValueError):
        return None  # cannot label an older observed date the previous session
    day=t0.astimezone(IST).date()-timedelta(days=1)
    while day.weekday()>=5 or day.isoformat() in holidays:
        day-=timedelta(days=1)
    day=day.isoformat()
    for table in ('bhav_options','chain_eod'):
        row=c.execute(f'SELECT MIN(expiry) FROM {table} WHERE index_name=? AND day=? AND expiry>=?', (name,day,cutoff)).fetchone()
        if row[0]:
            rows=[dict(r) for r in c.execute(f'SELECT strike,opt_type,oi FROM {table} WHERE index_name=? AND day=? AND expiry=?', (name,day,row[0]))]
            result=aggregate(rows)
            if result:return dict(result,day=day,source=table)
    return None


def extra_features(c,name,t0,step):
    out={}
    previous=previous_chain(c,name,t0)
    if previous:
        out.update(prev_day_pcr=previous['pcr'],prev_day_ce_wall=previous['ce_wall'],
                   prev_day_pe_wall=previous['pe_wall'],prev_day=previous['day'],prev_day_source=previous['source'])
    latest=wide_at(c,name,t0)
    if latest:
        out['max_pain']=max_pain(latest)
        old=wide_at(c,name,t0-timedelta(minutes=30))
        # Compare exactly matching expiry/strike/side identities in the current
        # ATM +/-15 window. A rolling ladder or expiry switch must not fake OI.
        current={(r['expiry'],r['strike'],r['opt_type']):r for r in latest}
        before={(r['expiry'],r['strike'],r['opt_type']):r for r in old}
        if current and current.keys()==before.keys():
            atm=round(latest[0]['spot']/step)*step
            changes={side:sum(r['oi']-before[k]['oi'] for k,r in current.items()
                            if r['opt_type']==side and abs(r['strike']-atm)<=15*step) for side in ('CE','PE')}
            total=sum(r['oi'] for r in old)
            if total>0:
                out.update(oi_build=(changes['PE']-changes['CE'])/total,
                           ce_oi_build=changes['CE'],pe_oi_build=changes['PE'])
    return out


def prune_wide(c):
    with c:
        for name, in c.execute('SELECT DISTINCT index_name FROM chain_wide').fetchall():
            days=[r[0] for r in c.execute('SELECT DISTINCT substr(ts,1,10) FROM chain_wide WHERE index_name=? ORDER BY 1 DESC LIMIT 60', (name,))]
            if len(days)==60:
                c.execute('DELETE FROM chain_wide WHERE index_name=? AND substr(ts,1,10)<?', (name,days[-1]))


def contracts(master,index,day,spot,eod=False):
    rows=[]
    for r in master._options_for(index):
        try:
            expiry=datetime.strptime(r['expiry'],'%d%b%Y').date()
            strike=float(r['strike'])/100
            side=r['symbol'][-2:]
        except (KeyError,ValueError,TypeError):continue
        if expiry>=day and side in ('CE','PE') and math.isfinite(strike) and strike>0:
            rows.append(dict(token=str(r['token']),expiry=expiry.isoformat(),strike=strike,opt_type=side))
    expiries=sorted({r['expiry'] for r in rows})[:2 if eod else 1]
    atm=round(spot/index.strike_step)*index.strike_step
    return [r for r in rows if r['expiry'] in expiries and
            (eod or abs(r['strike']-atm)<=15*index.strike_step)]


class Collector:
    def __init__(self,engine,indices,db=None):
        self.engine,self.indices,self.db=engine,indices,db
        self.stop_event=threading.Event()
        self.thread=None
        self.attempted=set()
        self.pruned=None

    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True,name='forecast-chain-data')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        # SDK HTTP may still finish; this is an isolated read-only data request.
        if self.thread:self.thread.join(timeout=1)

    def run(self):
        while not self.stop_event.is_set():
            try:self.tick(now_ist())
            except Exception as exc:
                audit.log('forecast_chain_cycle_skipped',error_type=type(exc).__name__)
            self.stop_event.wait(5)

    def tick(self,now):
        from . import hourly_forecast as HF,market_session_gate
        local=now.astimezone(IST)
        if local.weekday()>=5:return
        policy=market_session_gate.validate_policy(market_session_gate.read_json(market_session_gate.POLICY))
        if local.date().isoformat() in policy['holidays']:return
        day=local.date().isoformat()
        self.attempted={x for x in self.attempted if x[0]==day}
        if clock(15,35)<=local.time().replace(tzinfo=None) and self.pruned!=day:
            prune_wide(HF.conn(self.db));self.pruned=day
        minute=local.replace(second=0,microsecond=0)
        wide=(clock(9,15)<=minute.time().replace(tzinfo=None)<=clock(15,30) and
              minute.minute%5==0 and 20<=local.second<40)
        eod=clock(15,30)<=local.time().replace(tzinfo=None)<clock(15,32)
        for name in self.indices:
            for kind,do in (('wide',wide),('eod',eod)):
                key=(day,name,kind,minute.isoformat() if kind=='wide' else day)
                if not do or key in self.attempted:continue
                self.attempted.add(key)
                deadline=minute+timedelta(seconds=59) if kind=='wide' else local.replace(hour=15,minute=32,second=0,microsecond=0)
                try:self.collect(name,local,deadline,kind=='eod')
                except Exception as exc:
                    audit.log('forecast_chain_cycle_skipped',index=name,kind=kind,error_type=type(exc).__name__)

    def collect(self,name,now,deadline,eod):
        from . import hourly_forecast as HF
        from .session import SESSION
        from .rate_limiter import SHARED_QUOTE
        st=self.engine.states.get(name)
        if (st is None or not st.is_live_tick or st.last_updated is None or not st.spot or
                not math.isfinite(st.spot) or not 0 <= (now-st.last_updated).total_seconds()<=30):
            raise ValueError('fresh_engine_spot_unavailable')
        api=SESSION.api
        poll=getattr(getattr(api,'getMarketData',None),'_kadima_low_priority',None)
        if poll is None or SESSION._lock.locked() or SESSION._stale():
            raise ValueError('existing_authenticated_budgeted_session_unavailable')
        requested=contracts(self.engine.scrips,st.chain.index,now.date(),st.spot,eod)
        if not requested:raise ValueError('master_contracts_unavailable')
        spot=st.spot
        fetched={}
        calls=0
        for offset in range(0,len(requested),50):
            if self.stop_event.is_set() or now_ist()>=deadline:
                raise TimeoutError('snapshot_deadline')
            batch=requested[offset:offset+50]
            response=poll(mode='FULL',exchangeTokens={st.chain.index.opt_exchange:[r['token'] for r in batch]})
            calls+=1
            if not isinstance(response,dict) or response.get('status') is not True:
                raise ValueError('quote_response_unavailable')
            got=(response.get('data') or {}).get('fetched',[])
            wanted={r['token'] for r in batch}
            for r in got:
                token=str(r.get('symbolToken'))
                if token not in wanted or token in fetched or r.get('exchange',st.chain.index.opt_exchange)!=st.chain.index.opt_exchange:
                    raise ValueError('quote_identity_mismatch')
                values=(float(r['ltp']),float(r['opnInterest']),float(r['tradeVolume']))
                if not all(math.isfinite(x) and x>=0 for x in values) or not values[1].is_integer() or not values[2].is_integer():
                    raise ValueError('invalid_quote_values')
                fetched[token]=values
            if not wanted.issubset(fetched):raise ValueError('incomplete_quote_batch')
            if offset+50<len(requested) and self.stop_event.wait(SHARED_QUOTE.interval*2.05):
                raise TimeoutError('collector_stopped')
        stamp=now_ist()
        if stamp>=deadline:raise TimeoutError('snapshot_finished_late')
        c=HF.conn(self.db)
        with c:
            if eod:
                day=stamp.date().isoformat()
                previous=c.execute('SELECT MAX(day) FROM chain_eod WHERE index_name=? AND day<?',(name,day)).fetchone()[0]
                old={(r['expiry'],r['strike'],r['opt_type']):r['oi'] for r in c.execute('SELECT expiry,strike,opt_type,oi FROM chain_eod WHERE index_name=? AND day=?',(name,previous))}
                rows=[]
                for r in requested:
                    ltp,oi,volume=fetched[r['token']]
                    prior=old.get((r['expiry'],r['strike'],r['opt_type']))
                    rows.append((name,day,r['expiry'],r['strike'],r['opt_type'],ltp,int(oi),None if prior is None else int(oi)-prior,int(volume),spot))
                c.executemany('INSERT OR IGNORE INTO chain_eod VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
            else:
                c.executemany('INSERT OR IGNORE INTO chain_wide VALUES (?,?,?,?,?,?,?,?,?,?)',
                    [(name,stamp.isoformat(),r['expiry'],r['strike'],r['opt_type'],fetched[r['token']][0],int(fetched[r['token']][1]),int(fetched[r['token']][2]),None,spot) for r in requested])
        audit.log('forecast_chain_saved',index=name,kind='eod' if eod else 'wide',rows=len(requested),quote_calls=calls)
