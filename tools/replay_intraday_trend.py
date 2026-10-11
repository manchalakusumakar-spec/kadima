"""Offline chronological research replay. Never creates an executor or broker session.

Option spreads are assumed, not measured. Spot quotes are approximated by the
most recently closed minute bar and its close timestamp. Costs use the public
Angel One standard tariff, not the user's unverified account plan. No live flag
is set: SETUP_READY rows are simulated while retaining the observation block.
"""
import argparse,atexit,collections,gzip,hashlib,heapq,itertools,json,os,re,shutil,sys,tempfile
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
import offline_safety

PIN='8e8fb53873fdb7b13a46081c19a9b17e7b8a62b4e397ca29c44c57ebd29f8a46'
def read(path):
    with gzip.open(path,'rt') as f:
        for line in f:yield json.loads(line)
def costs(buy,sell,qty):
    b,s=buy*qty,sell*qty;turn=b+s
    brokerage=40.;stt=s*.0015;exchange=turn*.000355299
    sebi=turn*.000001;ipft=turn*.000000001
    gst=.18*(brokerage+exchange+sebi+ipft);stamp=b*.00003
    return dict(brokerage=brokerage,stt=stt,exchange=exchange,sebi=sebi,ipft=ipft,gst=gst,stamp=stamp)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument('--data',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--expected-lane-sha256',default=PIN)
    ap.add_argument('--source-commit',default='7c67c672b5d485af336893433ffe52ba2cf893eb')
    ap.add_argument('--source-manifest',type=Path)
    ap.add_argument('--claims-mode',choices=('simulated-only','unavailable'),default='simulated-only')
    ap.add_argument('--spread-pct',type=float,default=1.0);a=ap.parse_args()
    if not 0<a.spread_pct<=10:raise ValueError('invalid spread assumption')
    offline_safety.install()
    os.environ.pop('KADIMA_CELESTIAL_ENABLED',None)
    os.environ['KADIMA_DATA_DIR']=tempfile.mkdtemp(prefix='intraday-offline-')
    atexit.register(shutil.rmtree,os.environ['KADIMA_DATA_DIR'],ignore_errors=True)
    sys.path.insert(0,str(a.root.resolve()))
    if a.source_manifest:
        for name,record in json.loads(a.source_manifest.read_text()).items():
            if hashlib.sha256((a.root/'kadima'/name).read_bytes()).hexdigest()!=record['sha256']:
                raise ValueError('source manifest mismatch: '+name)
    from kadima import intraday_trend as lane,indicators,trade_policy,config,storage,celestial
    from kadima.option_chain import OptionChain,OptionQuote
    source_sha=hashlib.sha256(Path(lane.__file__).read_bytes()).hexdigest()
    if source_sha!=a.expected_lane_sha256:raise ValueError('lane source hash mismatch')
    policy_before=json.dumps(lane.POLICY,sort_keys=True)
    spread_guard_calls=0
    claim_reason_counts=collections.Counter()
    # All application persistence is in the temporary data directory.
    lot_sizes=json.loads((a.data/'lot_sizes.json').read_text())
    bars=list(read(a.data/'candles.jsonl.gz')) # 6,152 exported rows; no live DB imports.
    days=sorted({r['start_ts'][:10] for r in bars});split=max(1,round(len(days)*.6))
    histories=collections.defaultdict(list);states={};lanes={};positions={};stats={};trades=[];censored=[]
    def key(index,family,day):return ('calibration' if day in days[:split] else 'validation',index,family)
    def stat(k):
        if k not in stats:stats[k]=dict(setups=0,rejections=collections.Counter(),contract_rejections=collections.Counter())
        return stats[k]
    for name in ('NIFTY','BANKNIFTY'):
        states[name]=NS(name=name,spot=None,snap=None,technical_at=None,last_updated=None,chain=None)
        lanes[name]=lane.Lane();positions[name]={}
    tok={config.INDICES[n].token:n for n in states}
    candle_events=sorted(((datetime.fromisoformat(r['start_ts'])+timedelta(minutes=r['interval']),0,r) for r in bars),
                         key=lambda x:(x[0],x[2]['interval'],x[2]['token']))
    # Same timestamps can contain multiple bars; stable sorting without comparing dicts.
    def polls():
        for ts,group in itertools.groupby(read(a.data/'oi_snapshots.jsonl.gz'),lambda r:r['ts']):
            rows=list(group);yield datetime.fromisoformat(ts),1,rows
    time_events=[(datetime.fromisoformat(d+'T15:10:00+05:30'),2,None) for d in days]
    def close(name,p,price,now,reason):
        fees=costs(p.entry_price,price,p.lot_size);net=(price-p.entry_price)*p.lot_size-sum(fees.values())
        trades.append(dict(split=p.split,index=name,family=p.family,entry_at=p.entry_ts.isoformat(),exit_at=now.isoformat(),
            token=p.token,symbol=p.symbol,strike=p.strike,entry=p.entry_price,exit=price,
            peak_premium=p.peak_ltp,policy_peak_price=p.peak_price,
            qty=p.lot_size,net=net,costs=fees,exit_reason=reason))
        p.status='closed';p.exit_ts=now
    last_day=None
    for now,kind,event in heapq.merge(candle_events,polls(),time_events,key=lambda x:(x[0],x[1])):
        day=now.date().isoformat()
        if day!=last_day:
            for name,ps in positions.items():
                for p in ps.values():
                    if p.status!='closed':
                        censored.append(dict(index=name,family=p.family,entry=p.entry_ts.isoformat(),reason='no_fresh_exit_quote'));p.status='closed'
                lanes[name].pending.clear()
            last_day=day
        if kind==0:
            r=event;name=tok[r['token']];st=states[name]
            histories[(r['token'],r['interval'])].append(r)
            if r['interval']==1:st.spot=r['close'];st.last_updated=now
            if r['interval']==3:
                rows=histories[(r['token'],3)][-150:]
                # Stored index volumes are zero in this export: session VWAP falls
                # back to the closed candle's VWAP, as indicators.compute does.
                st.snap=indicators.compute(rows,ema_fast_p=config.EMA_FAST,ema_slow_p=config.EMA_SLOW,
                    ma_long_p=config.MA_LONG,rsi_p=config.RSI_PERIOD);st.technical_at=now
            if r['interval']==5:
                try:setup,evaluations=lane.detect(histories[(r['token'],5)][-90:],st.spot,st.snap,st.technical_at,now)
                except ValueError as exc:
                    stat(key(name,'ALL',day))['rejections'][str(exc)]+=1;continue
                for family,reason in evaluations.items():
                    if reason!='SETUP':stat(key(name,family,day))['rejections'][reason]+=1
                lanes[name].pending.pop(name,None)
                if setup:
                    episode=':'.join(('IT',name,setup['family'],setup['direction'],setup['bar_start']))
                    row=dict(setup,episode=episode,underlying=name,strategy=lane.STRATEGY,policy_id=lane.POLICY_ID,
                             ts=now.isoformat(),blocked_reasons=[],stage='SETUP_OBSERVED')
                    lanes[name].pending[name]=row;stat(key(name,setup['family'],day))['setups']+=1
        elif kind==1:
            name=event[0]['index_name'];st=states[name]
            if not st.spot:continue
            # Every poll is used only once it has arrived; no backfill from future polls.
            latest={r['token']:r for r in event}
            for p in positions[name].values():
                if p.status=='closed' or p.token not in latest:continue
                quote=latest[p.token];ltp=quote['ltp']
                if not ltp or ltp<=0:continue
                p.last_quote=(now,ltp);p.peak_price=max(p.peak_price,ltp)
                p.peak_ltp=max(p.peak_ltp,ltp)
                p.effective_stop,active=trade_policy.ratchet(p.entry_price,p.peak_price,p.effective_stop,p.structural_stop)
                reason=lane.exit_reason(p,now)
                if ltp<=p.structural_stop:reason=reason or 'structural_stop_breached'
                elif active and ltp<=p.effective_stop:reason=reason or 'dynamic_profit_trail_breached'
                if not reason and hasattr(lane,'celestial_exit_guard'):
                    celestial.health('quote:'+p.exchange+':'+p.token,dict(
                        bid=ltp*(1-a.spread_pct/200),ask=ltp*(1+a.spread_pct/200)),timestamp=now.isoformat())
                    reason=lane.celestial_exit_guard(p,now);spread_guard_calls+=1
                if reason:close(name,p,ltp*(1-a.spread_pct/200),now,reason)
            if name not in lanes[name].pending:continue
            setup=lanes[name].pending[name];s=stat(key(name,setup['family'],day))
            # Keep the observed near-ATM strikes, same selected expiry, no future contracts.
            valid=[]
            for r in event:
                match=re.search(r'(\d{2}[A-Z]{3}\d{2})',r['symbol'] or '')
                if match:
                    expiry=datetime.strptime(match[1],'%d%b%y').replace(tzinfo=config.IST)
                    if expiry.date()>=now.date():valid.append((expiry,r))
            if not valid:s['rejections']['expiry_unavailable']+=1;continue
            expiry=min(x[0] for x in valid);chain=OptionChain.__new__(OptionChain)
            chain.index=config.INDICES[name];chain._expiry=expiry.strftime('%d%b%Y').upper()
            import time
            chain._last_poll=time.monotonic();chain.quotes={}
            for ex,r in valid:
                if ex!=expiry:continue
                q=OptionQuote(**{k:r.get(k) for k in ('token','symbol','strike','opt_type','ltp','oi','oi_change','volume','iv','delta','gamma','theta','vega')})
                if q.ltp and q.ltp>0:q.bid=q.ltp*(1-a.spread_pct/200);q.ask=q.ltp*(1+a.spread_pct/200)
                chain.quotes[q.token]=q
            st.chain=chain
            if hasattr(lane,'strike_claims'):
                # No historical Gamma/scalping candidate/position snapshots exist
                # in this export. Simulated-only is an explicit no-external-claims
                # research assumption; unavailable exercises the real fail-closed gate.
                claims=None
                if a.claims_mode=='simulated-only':
                    simulated={n+':'+sid:p for n,ps in positions.items() for sid,p in ps.items()}
                    claims=lane.strike_claims(NS(state=NS(positions=simulated),fast_rows={}))
                row=lanes[name].prepare(st,positions[name],now,claims=claims)
            else:row=lanes[name].prepare(st,positions[name],now)
            if row is None:continue
            for contract in row.get('contracts',[]):
                if contract.get('reason') not in ('SELECTED','PASSED_NOT_SELECTED'):s['contract_rejections'][contract['reason']]+=1
                if contract.get('reason','').startswith(('held_by_','strike_claimed_by_')):
                    claim_reason_counts[contract['reason']]+=1
            blocks=[b for b in row['blocked_reasons'] if b!='intraday_trend_observation_only']
            if row['stage']!='SETUP_READY' or blocks:
                for reason in blocks:s['rejections'][reason]+=1
                continue
            qty=lot_sizes.get(row['token'])
            if not qty:s['rejections']['historical_lot_size_unavailable']+=1;continue
            sid=lane.signal_id(row)
            p=NS(kind=lane.STRATEGY,index=name,status='open',entry_ts=now,entry_price=row['entry'],peak_price=row['entry'],
                structural_stop=row['stop'],effective_stop=row['stop'],token=row['token'],symbol=row['symbol'],exchange=row['exchange'],lot_size=qty,
                strike=row['strike'],peak_ltp=chain.quotes[row['token']].ltp,
                family=row['family'],split=key(name,row['family'],day)[0],exit_ts=None,last_quote=(now,chain.quotes[row['token']].ltp))
            positions[name][str(sid)]=p;lanes[name].pending.pop(name,None)
        else:
            for name,ps in positions.items():
                for p in ps.values():
                    if p.status=='closed':continue
                    stamp,ltp=p.last_quote
                    if 0<=(now-stamp).total_seconds()<=30:close(name,p,ltp*(1-a.spread_pct/200),now,'intraday_trend_time_exit')
                    # Otherwise wait for the next actual observed quote; no day-end substitution.
    for name,ps in positions.items():
        for p in ps.values():
            if p.status!='closed':censored.append(dict(index=name,family=p.family,entry=p.entry_ts.isoformat(),reason='no_fresh_exit_quote'))
    result=[]
    for k,s in sorted(stats.items()):
        group=[t for t in trades if (t['split'],t['index'],t['family'])==k];nets=[t['net'] for t in group]
        equity=peak=dd=0.
        for n in nets:equity+=n;peak=max(peak,equity);dd=max(dd,peak-equity)
        result.append(dict(split=k[0],index=k[1],family=k[2],**s,trades=len(group),wins=sum(n>0 for n in nets),
            losses=sum(n<0 for n in nets),breakeven=sum(n==0 for n in nets),net=sum(nets),avg=sum(nets)/len(nets) if nets else None,
            worst=min(nets) if nets else None,max_drawdown=dd if nets else None,
            exits=dict(collections.Counter(t['exit_reason'] for t in group))))
    aggregates=[]
    for split_name in ('calibration','validation'):
        for index_name in ('NIFTY','BANKNIFTY','ALL'):
            group=[t for t in trades if t['split']==split_name and (index_name=='ALL' or t['index']==index_name)]
            nets=[t['net'] for t in group];equity=peak=dd=0.
            for n in nets:equity+=n;peak=max(peak,equity);dd=max(dd,peak-equity)
            aggregates.append(dict(split=split_name,index=index_name,trades=len(group),wins=sum(n>0 for n in nets),
                losses=sum(n<0 for n in nets),net=sum(nets),avg=sum(nets)/len(nets) if nets else None,
                worst=min(nets) if nets else None,max_drawdown=dd if nets else None,
                exits=dict(collections.Counter(t['exit_reason'] for t in group))))
    if json.dumps(lane.POLICY,sort_keys=True)!=policy_before:raise AssertionError('policy mutated')
    a.output.write_text(json.dumps(dict(policy_id=lane.POLICY_ID,policy=lane.POLICY,policy_unchanged=True,
        source_commit=a.source_commit,source_sha256=source_sha,calibration=days[:split],validation=days[split:],aggregates=aggregates,
        spread_pct_assumption=a.spread_pct,stats=result,trades=trades,censored=censored,broker_sdk_loaded=any(k.startswith('SmartApi') for k in sys.modules),
        session_guard_blocks=len(offline_safety.BLOCKED),nonzero_index_candle_volumes=sum(bool(r['volume']) for r in bars),
        claims_mode=a.claims_mode,claim_reason_counts=dict(claim_reason_counts),spread_guard_evaluations=spread_guard_calls,
        strike_claims='historical competing-engine claims not reconstructable; simulated-only assumes no external claims; unavailable passes None and fails closed'),indent=2)+'\n')
    storage.conn().close()
    shutil.rmtree(os.environ['KADIMA_DATA_DIR'])
    print(json.dumps({'trades':len(trades),'censored':len(censored),'days':days,'SDK_loaded':False}))
if __name__=='__main__':main()
