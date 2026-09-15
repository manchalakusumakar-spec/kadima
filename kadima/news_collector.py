"""Read-only NSE RSS collector with exact company mapping and freshness gates.

Sources: https://www.nseindia.com/static/rss-feed . RSS is a rolling public
snapshot, not an exchange data-feed SLA. No positive score is inferred merely
from a results filing or corporate action. Ambiguous headlines stay neutral.
"""
import csv
import hashlib
import io
import re
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from . import audit, celestial, news_catalysts
from .config import IST
from .utils import now_ist

FEEDS = {
    'announcements': 'https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml',
    'board_meetings': 'https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml',
    'corporate_actions': 'https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml',
    'financial_results': 'https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml',
}
COMPANIES = 'https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv'


def download(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'KADIMA-NewsCollector/1.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        if urlparse(response.url).hostname not in ('nsearchives.nseindia.com', 'archives.nseindia.com'):
            raise ValueError('Unexpected exchange feed redirect')
        data = response.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError('Exchange feed exceeds memory budget')
    return data


def normalized(name):
    return ' '.join(name.upper().split())


def company_map(data):
    out = {}
    for row in csv.DictReader(io.StringIO(data.decode('utf-8-sig'))):
        row = {k.strip(): v.strip() for k, v in row.items()}
        symbol, name = row.get('SYMBOL'), row.get('NAME OF COMPANY')
        if not symbol or not name:
            raise ValueError('NSE company mapping schema changed')
        key = normalized(name)
        if key in out and out[key] != symbol:
            raise ValueError('Ambiguous company identity')
        out[key] = symbol
    if not out:
        raise ValueError('Empty company identity map')
    return out


def stamp(value):
    value = value.strip()
    try:
        ts = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        ts = datetime.strptime(value, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts


def classify(text):
    """Conservative explicit text rules, heuristic scores, never probabilities."""
    text = re.sub('<[^>]+>', ' ', text).lower()
    # Denials, historical references and attributed allegations are ambiguous.
    if re.search(r'\b(no|not|denies|denied|denial|rumou?r|alleged|clarification)\b', text):
        return 0, False
    if any(x in text for x in ('trading suspended', 'admitted into insolvency', 'fraud confirmed')):
        return 0, True
    positive = any(x in text for x in ('credit rating upgraded', 'awarded a contract', 'received an order worth'))
    negative = any(x in text for x in ('credit rating downgraded', 'defaulted on payment', 'contract terminated'))
    if positive == negative:
        return 0, False
    return (10 if positive else -10), False


def parse_feed(data, *, category, mapping, symbols, now):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XML declarations are not accepted')
    root = ET.fromstring(data)
    channel = root.find('channel')
    if root.tag != 'rss' or channel is None:
        raise ValueError('Expected an NSE RSS channel')
    items = channel.findall('item')
    if len(items) > 5000:
        raise ValueError('RSS item budget exceeded')
    title = channel.findtext('title', '')
    if 'NSE' not in title.upper():
        raise ValueError('Unexpected feed identity')
    build = channel.findtext('lastBuildDate')
    published = [stamp(item.findtext('pubDate', '')) for item in items]
    latest = stamp(build) if build else max(published, default=None)
    # A successful HTTP response containing an old/empty file is not live news.
    if latest is None or not 0 <= (now - latest).total_seconds() <= 36 * 3600:
        raise ValueError('Publisher feed is stale or undated')
    events = {}
    for item, published_at in zip(items, published):
        company = re.split(r'\s+-\s+Ex-Date:', item.findtext('title', ''), flags=re.I)[0]
        symbol = mapping.get(normalized(company))
        if symbol not in symbols:
            continue
        if published_at > now:
            raise ValueError('Future-dated exchange item')
        expiry = published_at + timedelta(hours=36)
        if expiry <= now:
            continue
        url = item.findtext('link', '').strip()
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname not in
                ('www.nseindia.com', 'nsearchives.nseindia.com', 'archives.nseindia.com')
                or parsed.username or parsed.password):
            raise ValueError('Invalid announcement source')
        description = item.findtext('description', '')
        score, block = classify(description) if category == 'announcements' else (0, False)
        # Shared document URL deduplicates stories/categories about one filing.
        identity = url if '/corporate/' in parsed.path else url + '|' + symbol + '|' + description
        eid = 'nse:' + hashlib.sha256(identity.encode()).hexdigest()
        events[eid] = dict(id=eid, symbols=[symbol], score=score, block=block, url=url,
                           published_at=published_at.isoformat(), expires_at=expiry.isoformat())
    return list(events.values()), latest.isoformat()


class NewsCollector:
    def __init__(self, symbols, fetch=download):
        self.symbols = set(symbols)
        self.fetch = fetch
        self._stop = threading.Event()
        self._mapping = None
        self._mapping_day = None

    def collect(self):
        now = now_ist()
        events, sources, errors = {}, {}, {}
        try:
            if self._mapping is None or self._mapping_day != now.date():
                self._mapping = company_map(self.fetch(COMPANIES))
                self._mapping_day = now.date()
            covered = self.symbols & set(self._mapping.values())
            for category, url in FEEDS.items():
                try:
                    items, publisher_at = parse_feed(self.fetch(url), category=category,
                        mapping=self._mapping, symbols=covered, now=now)
                    events.update({e['id']: e for e in items})
                    sources[category] = dict(url=url, publisher_at=publisher_at, ok=True)
                except Exception as exc:
                    errors[category] = type(exc).__name__
            if not covered or self.symbols - covered:
                errors['company_mapping'] = 'unresolved_symbols'
        except Exception as exc:
            covered = set()
            errors['company_mapping'] = type(exc).__name__
        if errors:
            celestial.health('news', dict(ok=False, source='NSE_RSS', covered_symbols=[],
                sources=sources, errors=errors, reason='news_sources_incomplete_or_stale'))
            audit.log('news_collector_degraded', categories=sorted(errors))
            return False
        # Ingestion is atomic; no empty-success batch is submitted on failures.
        news_catalysts.ingest(list(events.values()), source='NSE_RSS',
                             observed_at=now.isoformat(), covered_symbols=sorted(covered))
        celestial.health('news', dict(ok=True, source='NSE_RSS', covered_symbols=sorted(covered),
            sources=sources, scope='NSE_stock_corporate_RSS_only', event_count=len(events)))
        return True

    def run(self):
        while not self._stop.is_set():
            try:
                self.collect()
            except Exception as exc:
                celestial.health('news', dict(ok=False, covered_symbols=[], reason='collector_failed'))
                audit.log('news_collector_failed', error=type(exc).__name__)
            self._stop.wait(120)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name='celestial-news').start()

    def stop(self):
        self._stop.set()
