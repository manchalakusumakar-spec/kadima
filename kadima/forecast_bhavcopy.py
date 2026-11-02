"""Official NSE F&O archive ingestion into forecast.db only; no broker calls.

Sources: https://www.nseindia.com/all-reports-derivatives (ard-udiffcb)
and https://www.nseindia.com/static/resources/forms-formats-members (UDiFF).
Only the published UDiFF archive and its legacy archive fallback are requested.
No option-chain endpoint, cookie warmup, mirror or scraping fallback is used.
"""
import csv
from datetime import date, datetime, timedelta
import io
import math
import socket
import time
import urllib.error
import urllib.request
import zipfile

from .config import IST

NAMES={'NIFTY','BANKNIFTY','FINNIFTY'}
ARCHIVE='https://nsearchives.nseindia.com'
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
MAX_ZIP=8*1024*1024
MAX_CSV=80*1024*1024


def urls(day):
    # English month names must not depend on a host's LC_TIME.
    month=('JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC')[day.month-1]
    return (f'{ARCHIVE}/content/fo/BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip',
            f'{ARCHIVE}/content/historical/DERIVATIVES/{day.year}/{month}/fo{day.day:02d}{month}{day.year}bhav.csv.zip')


def parse_day(value):
    for fmt in ('%Y-%m-%d','%d-%b-%Y','%d%b%Y','%d-%m-%Y','%m/%d/%Y'):
        try:return datetime.strptime(value.strip(),fmt).date().isoformat()
        except ValueError:pass
    raise ValueError('invalid_bhavcopy_date')


def number(value, optional=False, integer=False, signed=False):
    if optional and (value is None or str(value).strip() in ('','-')):return None
    n=float(value)
    if not math.isfinite(n) or (not signed and n<0) or (integer and not n.is_integer()):
        raise ValueError('invalid_bhavcopy_number')
    return int(n) if integer else n


def parse_csv(text,day):
    reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not reader.fieldnames:raise ValueError('missing_bhavcopy_header')
    reader.fieldnames=[x.strip() for x in reader.fieldnames]
    udiff='TckrSymb' in reader.fieldnames
    required={'TckrSymb','XpryDt','StrkPric','OptnTp','ClsPric','OpnIntrst','TtlTradgVol'} if udiff else {'INSTRUMENT','SYMBOL','EXPIRY_DT','STRIKE_PR','OPTION_TYP','CLOSE','OPEN_INT','CONTRACTS'}
    if not required.issubset(reader.fieldnames):raise ValueError('unsupported_bhavcopy_layout')
    out=[]
    for r in reader:
        name=((r.get('TckrSymb') if udiff else r.get('SYMBOL')) or '').strip()
        side=((r.get('OptnTp') if udiff else r.get('OPTION_TYP')) or '').strip()
        kind=((r.get('FinInstrmTp') if udiff else r.get('INSTRUMENT')) or '').strip()
        if name not in NAMES or side not in ('CE','PE') or (kind and kind not in ('IDO','OPTIDX')):continue
        recorded=(r.get('TradDt') or r.get('BizDt')) if udiff else r.get('TIMESTAMP')
        if recorded and parse_day(recorded)!=day.isoformat():raise ValueError('wrong_bhavcopy_trading_day')
        expiry=parse_day((r.get('FininstrmActlXpryDt') or r['XpryDt']) if udiff else r['EXPIRY_DT'])
        if expiry<day.isoformat():continue
        strike=number(r['StrkPric' if udiff else 'STRIKE_PR'])
        close=number(r['ClsPric' if udiff else 'CLOSE'])
        settle=number(r.get('SttlmPric' if udiff else 'SETTLE_PR'),optional=True)
        oi=number(r['OpnIntrst' if udiff else 'OPEN_INT'],integer=True)
        change=number(r.get('ChngInOpnIntrst' if udiff else 'CHG_IN_OI'),optional=True,integer=True,signed=True)
        volume=number(r['TtlTradgVol' if udiff else 'CONTRACTS'],integer=True)
        # Preserve NSE's published FO volume, without guessing a lot conversion.
        # The official layout names this Total Traded Qty; legacy names it
        # CONTRACTS. These source semantics are documented in the report.
        if strike<=0:raise ValueError('invalid_option_strike')
        out.append((name,day.isoformat(),expiry,strike,side,close,settle,oi,change,volume))
    if not out:raise ValueError('no_matching_index_options')
    if len({r[:5] for r in out})!=len(out):raise ValueError('duplicate_bhavcopy_contracts')
    return out


def download(day,opener=urllib.request.urlopen):
    """At most two requests: legacy only on UDiFF 404. Never bypass a block."""
    for attempt,url in enumerate(urls(day)):
        req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/zip'})
        try:
            with opener(req,timeout=20) as response:
                content_type=response.headers.get('Content-Type','').lower()
                if 'html' in content_type:return 'blocked',None,'html_instead_of_zip'
                payload=response.read(MAX_ZIP+1)
            if len(payload)>MAX_ZIP:return 'blocked',None,'archive_size_limit'
            if not payload.startswith(b'PK'):return 'blocked',None,'non_zip_response'
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                files=[f for f in archive.infolist() if f.filename.lower().endswith('.csv') and not f.is_dir()]
                if len(files)!=1 or files[0].file_size>MAX_CSV:return 'blocked',None,'invalid_archive_members'
                text=archive.read(files[0]).decode('utf-8-sig')
            return 'ok',parse_csv(text,day),'udiff' if attempt==0 else 'legacy'
        except urllib.error.HTTPError as exc:
            if exc.code==404:
                if attempt==0:continue
                return 'missing',None,'both_archives_404'
            return 'blocked',None,'http_'+str(exc.code)
        except (urllib.error.URLError,TimeoutError,socket.timeout):
            return 'blocked',None,'network_timeout_or_denied'
        except (zipfile.BadZipFile,UnicodeError,ValueError,KeyError,TypeError):
            return 'blocked',None,'invalid_archive_or_layout'
    return 'missing',None,'archive_unavailable'


def ingest(day,c,opener=urllib.request.urlopen):
    cached=c.execute("SELECT status,rows_saved,detail,checked_at FROM nightly_status WHERE step='bhavcopy' AND day=?",(day.isoformat(),)).fetchone()
    if cached and (cached['status']=='ok' or cached['checked_at'][:10]==datetime.now(IST).date().isoformat()):
        return dict(day=day.isoformat(),status=cached['status'],rows_saved=cached['rows_saved'],detail='cached_'+cached['detail'])
    status,rows,detail=download(day,opener)
    saved=0
    with c:
        if rows:
            c.executemany('INSERT OR REPLACE INTO bhav_options VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
            saved=len(rows)
        if status!='ok':
            fallback=c.execute('SELECT COUNT(*) FROM chain_eod WHERE day=?',(day.isoformat(),)).fetchone()[0]
            detail+=f'; chain_eod_fallback_rows={fallback}'
        c.execute('INSERT OR REPLACE INTO nightly_status VALUES (?,?,?,?,?,?)',
            ('bhavcopy',day.isoformat(),status,saved,datetime.now(IST).isoformat(),detail))
    return dict(day=day.isoformat(),status=status,rows_saved=saved,detail=detail)


def trading_days(end,count):
    from . import market_session_gate
    policy=market_session_gate.validate_policy(market_session_gate.read_json(market_session_gate.POLICY))
    holidays=set(policy['holidays'])
    result=[]
    while len(result)<count:
        if end.weekday()<5 and end.isoformat() not in holidays:result.append(end)
        end-=timedelta(days=1)
    return list(reversed(result))


def run_nightly(c,now,day=None,sleeper=time.sleep,opener=urllib.request.urlopen):
    """One initial 30-session backfill; afterwards fetch the latest session."""
    if day:
        days=[day]
    else:
        first=c.execute("SELECT 1 FROM nightly_status WHERE step='bhavcopy_backfill' AND status='ok' LIMIT 1").fetchone() is None
        end=now.date() if now.hour>=16 else now.date()-timedelta(days=1)
        days=trading_days(end,30 if first else 1)
    results=[]
    for i,point in enumerate(days):
        if i:sleeper(2)
        result=ingest(point,c,opener)
        results.append(result)
        print('bhavcopy: '+result['day']+' '+result['status']+' rows='+str(result['rows_saved'])+' '+result['detail'],flush=True)
    if not day and len(days)==30:
        with c:
            c.execute('INSERT OR REPLACE INTO nightly_status VALUES (?,?,?,?,?,?)',
                ('bhavcopy_backfill',now.date().isoformat(),'ok',sum(r['rows_saved'] for r in results),now.isoformat(),'30 sessions attempted; blocked/missing days retained'))
    return results
