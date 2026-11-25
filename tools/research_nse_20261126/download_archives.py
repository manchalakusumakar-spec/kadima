import sqlite3, os, time, subprocess, sys
from datetime import date
OUT='/home/ubuntu/kadima-research/nse'
os.makedirs(OUT, exist_ok=True)
f=sqlite3.connect('file:/var/lib/kadima/forecast.db?mode=ro',uri=True)
days=sorted({r[0][:10] for r in f.execute("select start_ts from history_candles where token='99926000'")})
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36'
def get(url, path):
    if os.path.exists(path) and os.path.getsize(path)>200: return 'cached'
    for host in ('https://nsearchives.nseindia.com','https://archives.nseindia.com'):
        r=subprocess.run(['curl','-s','-m','20','-A',UA,'-o',path,'-w','%{http_code}',host+url],capture_output=True,text=True)
        if r.stdout=='200' and os.path.getsize(path)>200:
            with open(path,'rb') as fh: head=fh.read(200)
            if b'<html' not in head.lower(): return 'ok'
        time.sleep(0.3)
    if os.path.exists(path): os.remove(path)
    return 'miss'
stats={}
for d in days:
    y,m,dd=d.split('-'); dm=f'{dd}{m}{y}'; dmy=f'{dd}{m}{y[2:]}'
    for kind,url in (('idx',f'/content/indices/ind_close_all_{dm}.csv'),('poi',f'/content/nsccl/fao_participant_oi_{dm}.csv'),('ma',f'/archives/equities/mkt/MA{dmy}.csv')):
        s=get(url, f'{OUT}/{kind}_{d}.csv'); stats[(kind,s)]=stats.get((kind,s),0)+1
        if s!='cached': time.sleep(0.25)
print(len(days), days[0], days[-1], stats, flush=True)
