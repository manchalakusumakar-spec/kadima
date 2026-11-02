#!/usr/bin/env python3
"""Offline index gate regression checks; isolated database, mocked feeds."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima-index-gate-')
sys.path.insert(0, '.')
from kadima import celestial, fast_momentum, gap_and_go, index_event_gate, macro_watch
from kadima.news_collector import NewsCollector, COMPANIES, FEEDS
from kadima.config import IST

now = datetime(2026, 10, 9, 9, 30, tzinfo=IST)
cfg = celestial.DEFAULTS | {'auto_trade_enabled': True}


def blocks(name, at=now):
    celestial.health('news', {'ok': True, 'covered_symbols': ['INFY']}, timestamp=at.isoformat())
    return fast_momentum.live_blocks(name, 'CE', at, 89.15)


with patch.object(celestial, 'read_config', return_value=cfg), patch.dict(os.environ, {
        'KADIMA_FAST_MOMENTUM_LIVE': '1', 'KADIMA_CELESTIAL_ENABLED': '1', 'KADIMA_GAP_LIVE': '1'}):
    for name in ('BANKNIFTY', 'NIFTY', 'SENSEX'):
        assert blocks(name) == [], name
        assert gap_and_go.live_blocks(name, now) == []
    assert 'fresh_news_coverage_required' in blocks('TCS')
    candidate = dict(id='offline', underlying='BANKNIFTY', direction='CE',
                     last_updated=now.isoformat(), spot_at=now.isoformat(),
                     premium_move_pct=8, gamma_score=89.15, volume_spike=True,
                     oi_support=True, spread_status='CLEAN', tradeable=True,
                     direction_clear=True, priority=1)
    ranked = celestial.ranked_candidates(now, candidates=[candidate])
    assert ranked[0]['eligible']
    with patch.object(celestial, 'ranked_candidates', return_value=ranked):
        assert celestial.top20(now)[0]['eligible']
    stock = candidate | {'underlying': 'TCS'}
    assert 'news_coverage_unavailable' in celestial.ranked_candidates(now, candidates=[stock])[0]['blocked_reasons']
    index_event_gate._minutes.clear()
    with patch.object(index_event_gate.audit, 'log') as log:
        index_event_gate.index_event_block('BANKNIFTY', now)
        index_event_gate.index_event_block('BANKNIFTY', now)
        assert log.call_count == 1
        index_event_gate.index_event_block('BANKNIFTY', now + timedelta(minutes=1))
        assert log.call_count == 2
    for event, day, early, late in (
            ('RBI MPC Meeting', '2026-10-09', (10, 0), (11, 0)),
            ('US Fed FOMC Meeting', '2026-10-28', (9, 20), (10, 0))):
        at = now if 'RBI' in event else datetime(2026, 10, 29, tzinfo=IST)
        with patch.object(macro_watch, 'ECONOMIC_EVENTS', [{'name': event, 'end_date': day}]):
            assert 'macro_event_window:' + event in blocks('BANKNIFTY', at.replace(hour=early[0], minute=early[1]))
            assert blocks('BANKNIFTY', at.replace(hour=late[0], minute=late[1])) == []
    event = {'id': 'market', 'symbols': ['*'], 'score': 0, 'block': True,
             'url': 'https://www.nseindia.com/', 'published_at': now.isoformat(),
             'expires_at': (now + timedelta(hours=1)).isoformat()}
    import json
    c = celestial.connection()
    with c:
        c.execute('INSERT INTO celestial_news VALUES (?,?,?,?)',
                  ('market', event['published_at'], event['expires_at'], json.dumps(event)))
    assert blocks('BANKNIFTY') == ['news_risk_block']
    with c:
        c.execute('DELETE FROM celestial_news')

mapping = b'SYMBOL,NAME OF COMPANY\nTCS,Tata Consultancy Services\n'
rss = b'<rss><channel><title>NSE</title><lastBuildDate>09-Oct-2026 09:00:00</lastBuildDate></channel></rss>'


def fetch(url):
    if url == COMPANIES:
        return mapping
    if url == FEEDS['announcements']:
        raise OSError('offline feed failure')
    return rss


with patch('kadima.news_collector.now_ist', return_value=now), patch('kadima.news_catalysts.now_ist', return_value=now), patch.object(celestial, 'fresh', return_value=True):
    assert NewsCollector(['TCS', 'UNMAPPED'], fetch=fetch).collect()
    health = celestial.health('news')
    assert health['ok'] and health['degraded'] and health['covered_symbols'] == ['TCS']
    assert 'announcements' in health['errors']
    assert not NewsCollector(['TCS'], fetch=lambda url: mapping if url == COMPANIES else b'invalid').collect()
    assert celestial.health('news')['ok'] is False
    assert not NewsCollector(['TCS'], fetch=lambda url: b'invalid').collect()
print('ALL PASS: index coverage, macro windows, market news, stock coverage, partial/all-failed feeds')
