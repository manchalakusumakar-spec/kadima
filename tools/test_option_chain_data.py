"""Offline option-chain regressions. No real broker or network calls."""
import io
import json
from datetime import date,datetime,timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0,'.')
from kadima import hourly_forecast as HF, hourly_forecast_model as M
from kadima import forecast_chain_data as F, forecast_bhavcopy as B, storage, market_session_gate as G
from kadima.rate_limiter import SharedQuoteLimiter,protect_quote_api

def database():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.executescript(F.SCHEMA);return c

def add(c,table,day,ce,pe):
    expiry='2026-10-13'
    for side,ois in (('CE',ce),('PE',pe)):
        for strike,oi in zip((100,150),ois):
            if table=='chain_wide':row=('NIFTY',day,expiry,strike,side,10,oi,100,None,125)
            elif table=='chain_eod':row=('NIFTY',day,expiry,strike,side,10,oi,None,100,125)
            else:row=('NIFTY',day,expiry,strike,side,10,10,oi,0,100)
            c.execute('INSERT INTO '+table+' VALUES (?,?,?,?,?,?,?,?,?,?)',row)

policy=dict(schema=1,valid_from='2026-01-01',valid_through='2026-12-31',holidays=['2026-10-02'],sources=['offline'],arm_at='09:00',entries_at='09:15',stop_entries_at='15:35')
now=datetime(2026,10,9,10,tzinfo=HF.IST)
c=database();trading=sqlite3.connect(':memory:');trading.row_factory=sqlite3.Row
trading.execute('CREATE TABLE oi_snapshots(index_name,ts,opt_type,strike,oi)')
for side,oi in (('CE',100),('PE',300)):
    trading.execute('INSERT INTO oi_snapshots VALUES (?,?,?,?,?)',('NIFTY',(now-timedelta(seconds=10)).isoformat(),side,200,oi))
with patch.object(HF,'conn',return_value=c),patch.object(storage,'conn',return_value=trading),patch.object(G,'read_json',return_value=policy):
    assert HF.chain_at('NIFTY',now)==dict(pcr=3,ce_wall=200,pe_wall=200)
    old=HF.oi_features('NIFTY',now,50)
    assert 'max_pain' not in old and 'prev_day_pcr' not in old
    before=M.oi_tilt(old)
    add(c,'chain_wide',(now-timedelta(minutes=30,seconds=1)).isoformat(),(100,100),(100,100))
    add(c,'chain_wide',(now-timedelta(seconds=1)).isoformat(),(100,100),(200,200))
    add(c,'chain_eod','2026-10-08',(100,100),(50,50))
    add(c,'bhav_options','2026-10-08',(50,100),(100,200))
    # Same-day and future EOD cannot influence this morning's slot.
    add(c,'bhav_options','2026-10-09',(100,100),(10000,10000))
    add(c,'bhav_options','2026-10-12',(100,100),(20000,20000))
    assert HF.chain_at('NIFTY',now)['pcr']==2
    oi=HF.oi_features('NIFTY',now,50)
    assert oi['prev_day_pcr']==2 and oi['prev_day_source']=='bhav_options'
    assert oi['prev_day_ce_wall']==150 and oi['prev_day_pe_wall']==150
    assert oi['oi_build']==0.5 and oi['ce_oi_build']==0 and oi['pe_oi_build']==200
    assert oi['max_pain']==150 and -1<=M.oi_tilt(oi)[0]<=1
    add(c,'chain_wide',now.isoformat(),(1,1),(9999,9999))
    add(c,'chain_wide',(now+timedelta(seconds=1)).isoformat(),(1,1),(99999,99999))
    assert HF.oi_features('NIFTY',now,50)==oi
    c.execute("DELETE FROM bhav_options WHERE day='2026-10-08'")
    assert HF.oi_features('NIFTY',now,50)['prev_day_pcr']==0.5
    c.execute("DELETE FROM chain_eod WHERE day='2026-10-08'")
    assert 'prev_day_pcr' not in HF.oi_features('NIFTY',now,50)
    c.execute('DELETE FROM chain_wide');c.execute('DELETE FROM bhav_options')
    assert HF.oi_features('NIFTY',now,50)==old and M.oi_tilt(old)==before
    # Source exactly t0 is excluded even on the old narrow fallback.
    trading.execute('DELETE FROM oi_snapshots')
    trading.execute('INSERT INTO oi_snapshots VALUES (?,?,?,?,?)',('NIFTY',now.isoformat(),'CE',200,100))
    assert HF.chain_at('NIFTY',now) is None
print('PASS wide preference, legacy fallback, previous-session sources, OI builds, max pain and no look-ahead')

udiff='TradDt,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,NewBrdLotQty\n2026-10-09,IDO,NIFTY,2026-10-13,22250,PE,23.8,23.8,65000,-650,6500,65\n2026-10-09,IDO,TCS,2026-10-27,3000,CE,10,10,100,1,10,10\n'
legacy='INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,CLOSE,SETTLE_PR,OPEN_INT,CHG_IN_OI,CONTRACTS,TIMESTAMP\nOPTIDX,BANKNIFTY,27-OCT-2026,55000,CE,100,101,30000,-30,100,09-OCT-2026\n'
assert len(B.parse_csv(udiff,now.date()))==1
assert B.parse_csv(udiff,now.date())[0][9]==6500  # retain published volume
assert B.parse_csv(udiff.replace('6500,65','5,65'),now.date())[0][9]==5
actual=udiff.replace('NewBrdLotQty\n','NewBrdLotQty,FininstrmActlXpryDt\n').replace('6500,65\n','6500,65,2026-10-12\n').replace('10,10\n','10,10,2026-10-27\n')
assert B.parse_csv(actual,now.date())[0][2]=='2026-10-12'
assert B.parse_csv(legacy,now.date())[0][0]=='BANKNIFTY'
assert B.parse_csv(legacy,now.date())[0][8]==-30
try:B.parse_csv(udiff,date(2026,10,8))
except ValueError:pass
else:raise AssertionError('wrong-day CSV accepted')
payload=io.BytesIO()
with zipfile.ZipFile(payload,'w') as z:z.writestr('bhav.csv',udiff)
class Response(io.BytesIO):
    headers={'Content-Type':'application/zip'}
def success(req,timeout):
    assert timeout==20 and req.full_url.startswith(B.ARCHIVE+'/content/')
    return Response(payload.getvalue())
result=B.ingest(now.date(),c,success)
assert result['status']=='ok' and result['rows_saved']==1
called=[]
def fallback(req,timeout):
    called.append(req.full_url)
    if len(called)==1:raise urllib.error.HTTPError(req.full_url,404,'missing',{},None)
    return success(req,timeout)
assert B.download(now.date(),fallback)[0]=='ok' and len(called)==2
def denied(req,timeout):raise urllib.error.HTTPError(req.full_url,403,'denied',{},None)
other=now.date()-timedelta(days=1)
add(c,'chain_eod',other.isoformat(),(100,100),(100,100))
result=B.ingest(other,c,denied)
assert result['status']=='blocked' and 'chain_eod_fallback_rows=4' in result['detail']
assert c.execute("SELECT status FROM nightly_status WHERE day=?",(other.isoformat(),)).fetchone()[0]=='blocked'
def must_not_repeat(req,timeout):raise AssertionError('blocked download repeated in the same day')
assert B.ingest(other,c,must_not_repeat)['status']=='blocked'
assert B.download(now.date(),lambda req,timeout:Response(b'<html>blocked</html>'))[0]=='blocked'
with patch.object(G,'read_json',return_value=policy):
    days=B.trading_days(date(2026,10,5),2)
    assert days==[date(2026,10,1),date(2026,10,5)]
print('PASS UDiFF/legacy parsers, 404-only fallback, blocked status/EOD fallback and holiday calendar')

with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'budget.db'
    limiter=SharedQuoteLimiter(str(path),interval=0.01)
    assert not limiter.try_low_priority()
    assert limiter.wait_and_allow(timeout_sec=0.2)
    db=sqlite3.connect(path)
    db.execute('UPDATE budget SET next=?',(time.time()-1,));db.execute('INSERT INTO quote_waiters(ticket,expires) VALUES (?,?)',('foreground',time.time()+30));db.commit()
    assert not limiter.try_low_priority()
    db.execute('DELETE FROM quote_waiters');db.commit()
    assert limiter.try_low_priority() and not limiter.try_low_priority()
    db.execute('BEGIN IMMEDIATE')
    start=time.monotonic();assert not limiter.try_low_priority();assert time.monotonic()-start<0.1
    db.rollback();db.close()
    class API:
        calls=0
        def getMarketData(self,**kwargs):self.calls+=1;return {'status':True}
    api=API();protect_quote_api(api)
    with patch('kadima.rate_limiter.SHARED_QUOTE.try_low_priority',return_value=False):
        try:api.getMarketData._kadima_low_priority(mode='FULL',exchangeTokens={})
        except TimeoutError:pass
        else:raise AssertionError('busy quote was sent')
        assert api.calls==0
    with patch('kadima.rate_limiter.SHARED_QUOTE.try_low_priority',return_value=True):
        api.getMarketData._kadima_low_priority(mode='FULL',exchangeTokens={});assert api.calls==1
    entered,release=threading.Event(),threading.Event()
    class ForegroundAPI:
        def getMarketData(self,**kwargs):
            entered.set();release.wait(1);return {'status':True}
    active=ForegroundAPI();protect_quote_api(active)
    with patch('kadima.rate_limiter.SHARED_QUOTE.wait_and_allow',return_value=True), \
            patch('kadima.rate_limiter.SHARED_QUOTE.try_low_priority',return_value=True) as grant:
        worker=threading.Thread(target=lambda:active.getMarketData(mode='FULL',exchangeTokens={}))
        worker.start();assert entered.wait(1)
        try:active.getMarketData._kadima_low_priority(mode='FULL',exchangeTokens={})
        except TimeoutError:pass
        else:raise AssertionError('forecast competed with an in-flight foreground request')
        assert not grant.called
        release.set();worker.join(1);assert not worker.is_alive()
print('PASS low-priority poll yields to waiters/locks/shared spacing and never sends when busy')

from kadima.scrip_master import ScripMaster
from kadima.config import Index
from kadima.session import SESSION
index=Index('NIFTY','NSE','spot','NFO','NIFTY',50,1)
master=[]
for expiry in ('13OCT2026','20OCT2026'):
    for strike in range(50,1600,50):
        for side in ('CE','PE'):
            master.append(dict(name='NIFTY',exch_seg='NFO',instrumenttype='OPTIDX',expiry=expiry,
                strike=str(strike*100),symbol='NIFTY'+expiry+str(strike)+side,token=str(len(master)+1)))
st=SimpleNamespace(is_live_tick=True,last_updated=now,spot=800,chain=SimpleNamespace(index=index))
engine=SimpleNamespace(states={'NIFTY':st},scrips=ScripMaster(master))
collector=F.Collector(engine,('NIFTY',))
batches=[]
def quote(**kwargs):
    tokens=kwargs['exchangeTokens']['NFO'];batches.append(tokens);assert len(tokens)<=50
    return dict(status=True,data=dict(fetched=[dict(symbolToken=t,exchange='NFO',ltp=100,opnInterest=1000,tradeVolume=3000) for t in tokens]))
quote._kadima_low_priority=quote
with patch.object(HF,'conn',return_value=c),patch.object(SESSION,'api',SimpleNamespace(getMarketData=quote)),patch.object(SESSION,'_stale',return_value=False),patch.object(F,'now_ist',return_value=now+timedelta(seconds=1)),patch.object(collector.stop_event,'wait',return_value=False),patch.object(F.audit,'log'):
    collector.collect('NIFTY',now,now+timedelta(minutes=2),False)
    assert c.execute('SELECT COUNT(*) FROM chain_wide').fetchone()[0]==62 and len(batches)==2
    collector.collect('NIFTY',now,now+timedelta(minutes=2),True)
    assert c.execute("SELECT COUNT(*) FROM chain_eod WHERE day='2026-10-09'").fetchone()[0]==124
    assert len(batches)==5
    c.execute('DELETE FROM chain_wide')
    def incomplete(**kwargs):return dict(status=True,data=dict(fetched=[]))
    incomplete._kadima_low_priority=incomplete
    SESSION.api=SimpleNamespace(getMarketData=incomplete)
    try:collector.collect('NIFTY',now,now+timedelta(minutes=2),False)
    except ValueError:pass
    else:raise AssertionError('partial snapshot accepted')
    assert c.execute('SELECT COUNT(*) FROM chain_wide').fetchone()[0]==0
print('PASS existing-session/master collector: <=50-token batching, nearest/two-expiry snapshots and atomic incomplete-cycle skip')

for i in range(61):
    point=(now-timedelta(days=i)).isoformat()
    c.execute('INSERT INTO chain_wide VALUES (?,?,?,?,?,?,?,?,?,?)',('NIFTY',point,'2026-10-13',100,'CE',10,100,1,None,100))
before=c.execute('SELECT COUNT(*) FROM chain_eod').fetchone()[0]
F.prune_wide(c)
assert c.execute('SELECT COUNT(DISTINCT substr(ts,1,10)) FROM chain_wide').fetchone()[0]==60
assert c.execute('SELECT COUNT(*) FROM chain_eod').fetchone()[0]==before
print('PASS 60 observed-session retention; EOD retained forever')
print('ALL OPTION CHAIN DATA TESTS PASS')
