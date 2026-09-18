"""Bounded public RSS checks, independent of broker and trading processes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse
import urllib.request
import xml.etree.ElementTree as ET

SOURCES = {
    'NSE announcements': 'https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml',
    'NSE board meetings': 'https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml',
    'NSE financial results': 'https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml',
}
PATH = Path('/var/lib/kadima-live-inputs/news.json')
IST = timezone(timedelta(hours=5, minutes=30))

def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=IST)
    except (ValueError, TypeError):
        pass
    try:
        result = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        result = datetime.strptime(value.strip(), '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
    return result if result.tzinfo else result.replace(tzinfo=IST)

def parse(data, name, now):
    if len(data)>2000000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('rss_budget_or_declaration')
    root = ET.fromstring(data)
    channel = root.find('channel')
    if root.tag!='rss' or channel is None or 'NSE' not in channel.findtext('title','').upper():
        raise ValueError('unexpected_rss_identity')
    items = channel.findall('item')
    if len(items)>5000:
        raise ValueError('rss_item_budget')
    out = []
    for item in items:
        title = item.findtext('title','').strip()[:1000]
        link = item.findtext('link','').strip()[:2000]
        try:
            ts = timestamp(item.findtext('pubDate',''))
        except (ValueError, TypeError, OverflowError):
            continue
        if not title or not timedelta(0)<=now-ts<=timedelta(days=2):
            continue
        if urlparse(link).scheme!='https':
            link = ''
        identity = hashlib.sha256((title+link+ts.isoformat()).encode()).hexdigest()
        out.append(dict(id=identity,title=title,url=link,source=name,
                        published_at=ts.isoformat(),received_at=now.isoformat()))
    return sorted(out,key=lambda x:timestamp(x['published_at']),reverse=True)[:40]

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme!='https' or urlparse(newurl).hostname not in ('nsearchives.nseindia.com','archives.nseindia.com'):
            raise ValueError('unexpected_redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(name, url, previous, now):
    old = previous or {}
    source = dict(name=name,checked_at=now.isoformat(),last_success=old.get('last_success'),status='unavailable')
    articles = old.get('articles',[])
    # Failure backoff is per source and never exceeds five minutes.
    if now.timestamp()<old.get('retry_at',0):
        return old
    try:
        request = urllib.request.Request(url,headers={'User-Agent':'KADIMA-PublicNews/1.0'})
        with urllib.request.build_opener(SafeRedirect()).open(request,timeout=12) as response:
            raw = response.read(2000001)
        articles = parse(raw,name,now)
        source.update(status='ok',last_success=now.isoformat(),failures=0)
    except Exception as exc:
        failures = min(old.get('failures',0)+1,4)
        source.update(error=type(exc).__name__,failures=failures,retry_at=now.timestamp()+min(300,60*2**(failures-1)))
    source['articles'] = articles
    return source

def snapshot(states, now):
    unique = {}
    for state in states.values():
        for article in state.get('articles',[]):
            if now-timestamp(article['published_at'])<=timedelta(days=2):
                unique[article['id']] = article
    return dict(ts=now.isoformat(),poll_seconds=60,
                sources=[{k:v for k,v in s.items() if k!='articles'} for s in states.values()],
                articles=sorted(unique.values(),key=lambda x:timestamp(x['published_at']),reverse=True)[:60])

def main():
    states = {}
    PATH.parent.mkdir(parents=True,exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        while True:
            started = time.monotonic()
            now = datetime.now(timezone.utc)
            jobs = {name:pool.submit(fetch,name,url,states.get(name),now) for name,url in SOURCES.items()}
            states = {name:job.result() for name,job in jobs.items()}
            tmp = PATH.with_suffix('.tmp')
            tmp.write_text(json.dumps(snapshot(states,datetime.now(timezone.utc))))
            tmp.chmod(0o644)
            os.replace(tmp,PATH)
            time.sleep(max(1,60-(time.monotonic()-started)))

if __name__=='__main__':
    main()
