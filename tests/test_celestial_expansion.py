"""Offline all-root scan, source freshness, and exit-evidence regressions."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, Mock

from kadima import storage, celestial, full_universe_scanner as full
from kadima import news_collector as news, celestial_exit as exits
from kadima.scrip_master import ScripMaster
from kadima.config import IST

NOW = datetime(2026, 9, 16, 11, tzinfo=IST)

class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):return NOW if tz is None else NOW.astimezone(tz)


def master_rows(count=22):
    rows = []
    for i in range(count):
        name = 'STOCK'+str(i)
        rows.append(dict(name=name, symbol=name+'-EQ', token='S'+str(i), exch_seg='NSE', instrumenttype=''))
        for strike in (97.5, 100, 102.5):
            for side in ('CE','PE'):
                rows.append(dict(name=name, symbol=name+str(strike)+side, token=name+str(strike)+side,
                    exch_seg='NFO', instrumenttype='OPTSTK', expiry='24SEP2026',
                    lotsize='10', strike=str(strike*100)))
    return rows


def rss(items='', build='Wed, 16 Sep 2026 10:59:00 +0530'):
    return ('<rss><channel><title>NSE News</title><lastBuildDate>'+build+'</lastBuildDate>'+items+'</channel></rss>').encode()


def item(description='Credit rating upgraded', title='Example Limited', url='https://nsearchives.nseindia.com/corporate/a.pdf'):
    return '<item><title>'+title+'</title><link>'+url+'</link><description>'+description+'</description><pubDate>16-Sep-2026 10:55:00</pubDate></item>'


class Expansion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(storage, 'DATA_DIR', self.tmp.name),
            patch.object(storage, 'DB_PATH', self.tmp.name+'/state.db'),
            patch.object(storage, '_local', threading.local()),
            patch.dict('os.environ', {'KADIMA_CELESTIAL_ENABLED':'1'})]
        for module in (celestial, full, news, exits):
            self.patches.append(patch.object(module, 'now_ist', return_value=NOW))
        self.patches.append(patch('kadima.audit.log'))
        self.patches.append(patch.object(full,'write_snapshot'))
        self.patches.append(patch('kadima.scrip_master.datetime', FrozenDatetime))
        for p in self.patches:p.start()

    def tearDown(self):
        if getattr(storage._local, 'conn', None):storage._local.conn.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_fractional_strikes_and_all_22_roots_are_retained(self):
        master = ScripMaster(master_rows())
        indices, missing = full.stock_roots(master, NOW.date())
        self.assertEqual(len(indices),22)
        self.assertFalse(missing)
        self.assertEqual(indices['STOCK0'].strike_step,2.5)
        scanner = full.FullUniverseScanner(master)
        self.assertEqual(len(scanner.chains),22)

    def test_expired_and_unmapped_roots_are_explicit(self):
        rows = master_rows(2)
        rows = [r for r in rows if r.get('token')!='S1']
        rows.append(rows[1] | dict(name='EXPIRED', token='old', expiry='01SEP2026'))
        indices, missing = full.stock_roots(ScripMaster(rows), NOW.date())
        self.assertEqual(set(indices), {'STOCK0'})
        self.assertEqual([x['name'] for x in missing], ['STOCK1'])

    def test_bfo_stock_roots_are_out_of_scope_not_missing_nfo_coverage(self):
        rows = master_rows(2)
        bfo = rows[1].copy()
        bfo.update(exch_seg='BFO', token='BFO1', symbol='STOCK0BFOCE')
        rows.append(bfo)
        indices, missing = full.stock_roots(ScripMaster(rows), NOW.date())
        self.assertEqual(set(indices), {'STOCK0','STOCK1'})
        self.assertFalse(any(x.get('exchange') == 'BFO' for x in missing))

    def test_all_batches_respect_50_tokens_and_have_no_promotion_cutoff(self):
        scanner = full.FullUniverseScanner(ScripMaster(master_rows(22)))
        calls = []
        def fetch(keys):
            calls.append(set(keys))
            return {k:dict(exchange=k[0],symbolToken=k[1],ltp=100,avgPrice=99,
                opnInterest=100000,tradeVolume=100000,depth={'buy':[{'price':99.5}],'sell':[{'price':100.5}]},
                _received_at=NOW.isoformat()) for k in keys}
        result = SimpleNamespace(score=0,tier='NONE',direction=None,components=SimpleNamespace(oi_velocity=None))
        with patch.object(full,'fetch_batch',side_effect=fetch), patch.object(full.gamma,'score',return_value=result), \
             patch.object(scanner.radar,'publish_chain',return_value=[]):
            scanner.cycle();scanner.cycle()
        self.assertTrue(all(len(k)<=50 for k in calls))
        self.assertEqual(len(scanner.last),22)
        self.assertEqual(celestial.health('stock_universe')['option_chains_fresh'],22)
        self.assertTrue(celestial.health('stock_universe')['ok'])
        published=full.write_snapshot.call_args.args[0]
        self.assertEqual(len(published['tracked']),22)
        self.assertTrue(all('best_contract' in row for row in published['tracked']))

    def test_missing_contract_cannot_mark_chain_fresh(self):
        scanner = full.FullUniverseScanner(ScripMaster(master_rows(1)))
        chain = scanner.chains['STOCK0'];chain.rebuild_ladder(100)
        rows={('NSE','S0'):dict(ltp=100,_received_at=NOW.isoformat())}
        with patch.object(full,'fetch_batch',return_value=rows):scanner.poll_group(['STOCK0'])
        self.assertFalse(scanner.last)
        self.assertEqual(chain._last_poll,0)

    def test_exchange_token_collisions_do_not_cross_join(self):
        api = Mock(getMarketData=Mock(return_value={'status':True,'data':{'fetched':[
            {'exchange':'NSE','symbolToken':'1','ltp':100},
            {'exchange':'NFO','symbolToken':'1','ltp':10}]}}))
        with patch.object(full.SESSION,'ensure'), patch.object(full.SESSION,'api',api):
            rows=full.fetch_batch({('NSE','1'),('NFO','1')})
        self.assertEqual(rows[('NFO','1')]['ltp'],10)

    def test_unidentified_or_duplicate_quote_rejected(self):
        for rows in ([{'symbolToken':'1','ltp':100}],
                     [{'exchange':'NSE','symbolToken':'1','ltp':100}]*2):
            api=Mock(getMarketData=Mock(return_value={'status':True,'data':{'fetched':rows}}))
            with patch.object(full.SESSION,'ensure'),patch.object(full.SESSION,'api',api),self.assertRaises(ValueError):
                full.fetch_batch({('NSE','1')})

    def test_company_mapping_is_exact_not_substring(self):
        mapping=news.company_map(b'SYMBOL,NAME OF COMPANY\nEXAMPLE,Example Limited\n')
        events,_=news.parse_feed(rss(item()),category='announcements',mapping=mapping,symbols={'EXAMPLE'},now=NOW)
        self.assertEqual(events[0]['symbols'],['EXAMPLE'])
        self.assertEqual(events[0]['score'],10)
        events,_=news.parse_feed(rss(item(title='Other Example Limited')),category='announcements',mapping=mapping,symbols={'EXAMPLE'},now=NOW)
        self.assertEqual(events,[])

    def test_company_mapping_resolves_duplicate_only_against_requested_universe(self):
        data=(b'SYMBOL,NAME OF COMPANY\n'
              b'FEL,Future Enterprises Limited\n'
              b'FELDVR,Future Enterprises Limited\n'
              b'EXAMPLE,Example Limited\n')
        mapping=news.company_map(data, {'FEL','EXAMPLE'})
        self.assertEqual(mapping['FUTURE ENTERPRISES LIMITED'],'FEL')
        self.assertEqual(mapping['EXAMPLE LIMITED'],'EXAMPLE')
        mapping=news.company_map(data, {'EXAMPLE'})
        self.assertNotIn('FUTURE ENTERPRISES LIMITED',mapping)
        with self.assertRaises(ValueError):
            news.company_map(data)

    def test_relevant_duplicate_company_remains_fail_closed_via_coverage(self):
        data=(b'SYMBOL,NAME OF COMPANY\n'
              b'AAA,Same Limited\n'
              b'BBB,Same Limited\n')
        mapping=news.company_map(data, {'AAA','BBB'})
        self.assertEqual(mapping,{})
        collector=news.NewsCollector(['AAA','BBB'],lambda url:data if url==news.COMPANIES else rss())
        with patch.object(news.news_catalysts,'ingest') as ingest:
            self.assertFalse(collector.collect());ingest.assert_not_called()
        self.assertEqual(celestial.health('news')['errors']['company_mapping'],'unresolved_symbols')

    def test_results_filing_and_denial_never_invent_direction(self):
        self.assertEqual(news.classify('Company denies trading suspended rumor'),(0,False))
        events,_=news.parse_feed(rss(item()),category='financial_results',mapping={'EXAMPLE LIMITED':'EXAMPLE'},symbols={'EXAMPLE'},now=NOW)
        self.assertEqual(events[0]['score'],0)

    def test_quiet_but_valid_rss_keeps_coverage_while_undated_fails(self):
        events,latest=news.parse_feed(
            rss(build='01-Aug-2026 10:00:00'),
            category='financial_results',mapping={},symbols=set(),now=NOW)
        self.assertEqual(events,[])
        self.assertTrue(latest.startswith('2026-08-01'))
        with self.assertRaises(ValueError):
            news.parse_feed(b'<rss><channel><title>NSE</title></channel></rss>',
                category='announcements',mapping={},symbols=set(),now=NOW)

    def test_trusted_nse_http_links_are_canonicalised_but_offsite_still_rejected(self):
        mapping={'EXAMPLE LIMITED':'EXAMPLE'}
        events,_=news.parse_feed(
            rss(item(url='http://nsearchives.nseindia.com/corporate/a.pdf')),
            category='announcements',mapping=mapping,symbols={'EXAMPLE'},now=NOW)
        self.assertEqual(events[0]['url'],
                         'https://nsearchives.nseindia.com/corporate/a.pdf')
        with self.assertRaises(ValueError):
            news.parse_feed(rss(item(url='http://example.test/a.pdf')),
                category='announcements',mapping=mapping,symbols={'EXAMPLE'},now=NOW)

    def test_malformed_xml_or_offsite_attribution_rejected(self):
        for data in (b'<!DOCTYPE rss><rss/>',rss(item(url='https://example.test/news'))):
            with self.assertRaises(ValueError):
                news.parse_feed(data,category='announcements',mapping={'EXAMPLE LIMITED':'EXAMPLE'},symbols={'EXAMPLE'},now=NOW)

    def test_collector_failure_clears_coverage_without_empty_success(self):
        def fetch(url):
            if url==news.COMPANIES:return b'SYMBOL,NAME OF COMPANY\nEXAMPLE,Example Limited\n'
            if url==news.FEEDS['financial_results']:raise TimeoutError()
            return rss(item())
        collector=news.NewsCollector(['EXAMPLE'],fetch)
        with patch.object(news.news_catalysts,'ingest') as ingest:
            self.assertFalse(collector.collect());ingest.assert_not_called()
        self.assertFalse(celestial.health('news')['ok'])
        self.assertEqual(celestial.health('news')['covered_symbols'],[])

    def test_successful_collector_requires_every_configured_source(self):
        collector=news.NewsCollector(['EXAMPLE'],lambda url:b'SYMBOL,NAME OF COMPANY\nEXAMPLE,Example Limited\n' if url==news.COMPANIES else rss())
        with patch.object(news.news_catalysts,'ingest') as ingest:self.assertTrue(collector.collect())
        self.assertEqual(ingest.call_args.kwargs['covered_symbols'],['EXAMPLE'])
        sources=celestial.health('news')['sources']
        self.assertEqual(len(sources),4)
        self.assertTrue(all('publisher_age_hours' in value for value in sources.values()))

    def position(self):
        return SimpleNamespace(signal_id=1,index='TEST',exchange='NFO',token='T',direction='CE')

    def test_premium_ema_warms_on_completed_minutes_and_resets_gaps(self):
        pos=self.position()
        for sec in range(0,541,30):self.assertIsNone(exits.evaluate(pos,100,NOW+timedelta(seconds=sec)))
        self.assertEqual(exits.evaluate(pos,99,NOW+timedelta(seconds=550)),'premium_below_completed_1m_ema9')
        self.assertIsNone(exits.evaluate(pos,80,NOW+timedelta(seconds=800)))

    def test_spread_exit_requires_fresh_bid_and_ask(self):
        celestial.health('quote:NFO:T',dict(bid=95,ask=105))
        self.assertEqual(exits.evaluate(self.position(),100),'spread_widened_above_3pct')
        self.assertIsNone(exits.evaluate(self.position(),100,NOW+timedelta(seconds=31)))

    def test_vwap_and_opposite_gamma_exits_use_fresh_evidence(self):
        celestial.health('structure:TEST',dict(spot=99,vwap=100,spot_at=NOW.isoformat()))
        self.assertEqual(exits.evaluate(self.position(),100),'underlying_lost_vwap')
        self.assertIsNone(exits.evaluate(self.position(),100,NOW+timedelta(seconds=31)))
        celestial.health('structure:TEST',dict(spot=100,vwap=99,spot_at=NOW.isoformat(),direction='PE',score=80))
        self.assertEqual(exits.evaluate(self.position(),100),'opposite_gamma_confirmed')

    def test_missing_oi_is_not_invented_as_an_opposite_signal(self):
        celestial.health('structure:TEST',dict(spot=100,vwap=99,spot_at=NOW.isoformat(),direction=None,score=0,oi_direction=None))
        self.assertIsNone(exits.evaluate(self.position(),100))


if __name__=='__main__':unittest.main()
