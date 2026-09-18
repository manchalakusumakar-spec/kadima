import ast
from datetime import datetime, timezone, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch
import news_worker as news
import upgrade

NOW=datetime(2026,9,18,6,tzinfo=timezone.utc)
RSS=b'<rss><channel><title>NSE Announcements</title><item><title>A result</title><link>https://nseindia.com/notice</link><pubDate>18-Sep-2026 11:00:00</pubDate></item></channel></rss>'

class FeedTests(unittest.TestCase):
    def test_source_date_and_limits(self):
        rows=news.parse(RSS,'NSE',NOW)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['published_at'],'2026-09-18T11:00:00+05:30')
        self.assertEqual(news.parse(RSS,'NSE',NOW-timedelta(hours=1)),[])
        self.assertEqual(news.parse(RSS,'NSE',NOW+timedelta(days=3)),[])
        for bad in [b'<!DOCTYPE rss>'+RSS,b'<!ENTITY x "y">'+RSS,b'x'*2000001,RSS.replace(b'NSE Announcements',b'Wrong')]:
            with self.assertRaises(ValueError):news.parse(bad,'NSE',NOW)

    def test_failure_preserves_source_time_and_backoff(self):
        rows=news.parse(RSS,'NSE',NOW)
        old=dict(articles=rows,last_success=NOW.isoformat(),status='ok')
        with patch('urllib.request.build_opener',side_effect=OSError('network')):
            result=news.fetch('NSE','https://nsearchives.nseindia.com/x',old,NOW+timedelta(minutes=1))
            self.assertEqual(result['last_success'],old['last_success'])
            self.assertEqual(result['articles'],rows)
            self.assertEqual(result['status'],'unavailable')
            self.assertIs(news.fetch('NSE','unused',result,NOW+timedelta(seconds=90)),result)

    def test_snapshot_deduplicates_and_expires(self):
        state=dict(articles=news.parse(RSS,'NSE',NOW))
        self.assertEqual(len(news.snapshot({'a':state,'b':state},NOW)['articles']),1)
        self.assertEqual(news.snapshot({'a':state},NOW+timedelta(days=3))['articles'],[])

    def test_gateway_auth_precedes_read_and_is_idempotent(self):
        base="@app.get('/{name}')\ndef read(name): pass\n"
        out=upgrade.gateway(base)
        ast.parse(out)
        self.assertLess(out.index('authenticated(request)'),out.index("with open('/var/lib"))
        self.assertEqual(out,upgrade.gateway(out))
        with self.assertRaises(ValueError):upgrade.gateway('unrecognized')

    def test_dashboard_installed_version(self):
        source=Path(__file__).resolve().parents[1]/'dashboard_live/dashboard.html'
        if not source.exists():source=Path(__file__).with_name('base-dashboard.html')
        out=upgrade.dashboard(source.read_text())
        self.assertEqual(out,upgrade.dashboard(out))
        self.assertIn('Score-based probability',out)
        self.assertIn("liveView.probability(r.final_score",out)
        self.assertIn("liveView.probability(r.score,current)",out)
        self.assertIn("liveView.forecast(a,feeds.state.data",out)
        with self.assertRaises(ValueError):upgrade.dashboard('<html>unknown</html>')

if __name__=='__main__':unittest.main()
