"""Validated, source-attributed catalyst ingestion, not invented headline sentiment.

A trusted collector submits completed structured batches. An empty successful
batch means checked/no news; a missing or failed collector never means neutral.
No subscription or external service is silently created by this adapter.
"""
import json
from datetime import datetime
from urllib.parse import urlparse
from . import celestial
from .utils import now_ist


def ingest(events, *, source, observed_at, covered_symbols):
    if (not isinstance(source, str) or not 1 <= len(source) <= 100
            or not celestial.fresh(observed_at, seconds=300)
            or not isinstance(events, list) or len(events) > 500):
        raise ValueError('Invalid source, observation time or batch')
    if (not isinstance(covered_symbols,list) or not 1<=len(covered_symbols)<=500
            or any(not isinstance(s,str) or not s or len(s)>40 or s=='*' for s in covered_symbols)):
        raise ValueError('Explicit successfully checked symbol coverage required')
    now = now_ist()
    validated = []
    for event in events:
        if set(event) != {'id','symbols','score','block','url','published_at','expires_at'}:
            raise ValueError('Invalid catalyst schema')
        if (not isinstance(event['id'], str) or not 1 <= len(event['id']) <= 200
                or not isinstance(event['symbols'], list) or not 1 <= len(event['symbols']) <= 300
                or any(not isinstance(s,str) or not s or len(s)>40 for s in event['symbols'])
                or type(event['score']) is not int or event['score'] not in (-20,-10,0,10,20)
                or type(event['block']) is not bool):
            raise ValueError('Invalid catalyst identity, score or symbols')
        url = urlparse(event['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('HTTPS source attribution required')
        start, end = map(datetime.fromisoformat, (event['published_at'], event['expires_at']))
        if start.tzinfo is None or end.tzinfo is None or not start <= now < end:
            raise ValueError('Future, expired or timezone-naive catalyst')
        if (end-start).total_seconds() > 86400 * 7:
            raise ValueError('Catalyst requires review within seven days')
        validated.append(event | {'source':source})
    c = celestial.connection()
    with c:
        for e in validated:
            c.execute('INSERT OR REPLACE INTO celestial_news VALUES (?,?,?,?)',
                      (e['id'],e['published_at'],e['expires_at'],json.dumps(e,allow_nan=False)))
        c.execute('DELETE FROM celestial_news WHERE expires_at < ?', (now.isoformat(),))
    celestial.health('news',dict(ok=True,source=source,event_count=len(validated),
                                covered_symbols=covered_symbols),timestamp=observed_at)
    return {'ok':True,'accepted':len(validated)}
