import unittest
import copy
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import patch,Mock
from kadima.config import IST
from kadima import cas_blast_scanner as cas

BASE=datetime(2026,9,16,14,55,tzinfo=IST)


class CasDetection(unittest.TestCase):
    def train(self,window):
        metrics=None
        volume=0
        for second in range(0,1201,15):
            volume+=10 if second<=1080 else 40
            metrics=window.add(BASE+timedelta(seconds=second),10 if second<=1080 else 14.1,volume)
        return metrics

    def test_incremental_volume_and_elapsed_time_define_trigger(self):
        w=cas.StrikeWindow();metrics=self.train(w)
        self.assertAlmostEqual(metrics['premium_change_pct'],41)
        self.assertAlmostEqual(metrics['volume_ratio'],4)
        self.assertLessEqual(len(w.samples),160)

    def test_cumulative_volume_alone_is_not_a_spike(self):
        w=cas.StrikeWindow();metrics=None
        for second in range(0,1201,15):metrics=w.add(BASE+timedelta(seconds=second),14.1 if second==1200 else 10,100000+second)
        self.assertAlmostEqual(metrics['volume_ratio'],1)

    def test_gap_reset_duplicate_and_day_roll_do_not_manufacture_history(self):
        w=cas.StrikeWindow();self.train(w);size=len(w.samples)
        self.assertIsNone(w.add(BASE+timedelta(seconds=1200),50,5000))
        self.assertEqual(len(w.samples),size)
        self.assertIsNone(w.add(BASE+timedelta(seconds=1300),50,5000))
        self.assertEqual(len(w.samples),1)
        self.assertIsNone(w.add(BASE+timedelta(days=1),50,5000))
        self.assertEqual(len(w.samples),1)

    def test_volume_reset_or_invalid_price_requires_new_warmup(self):
        for price,volume in ((15,1),(float('nan'),10000)):
            w=cas.StrikeWindow();self.train(w)
            self.assertIsNone(w.add(BASE+timedelta(seconds=1215),price,volume))
            self.assertLessEqual(len(w.samples),1)

    def test_engine_quote_observation_persists_before_notification_and_never_orders(self):
        scanner=cas.CasBlastScanner()
        q=SimpleNamespace(token='T',symbol='TESTCE',strike=100,opt_type='CE',bid=14,ask=14.2)
        q2=copy.copy(q);q2.token='U';q2.symbol='TEST2CE'
        chain=SimpleNamespace(index=SimpleNamespace(name='NIFTY',opt_exchange='NFO'),quotes={'T':q,'U':q2},_last_poll=100,_expiry='24SEP2026')
        order=[]
        def broken_notification(*args,**kwargs):
            order.append('notification_failed');raise RuntimeError('notification offline')
        with patch.object(cas.time,'monotonic',return_value=100), \
             patch.object(cas,'implied_vol_and_greeks',return_value=(20,{'delta':.1})), \
             patch.object(cas.celestial,'health'),patch.object(cas.audit,'log'), \
             patch.object(cas.storage,'insert_signal',side_effect=lambda *a,**kw:order.append('saved') or 9), \
             patch.object(cas.alerts,'enqueue',side_effect=broken_notification):
            volume=0;found=[]
            for second in range(0,1201,15):
                now=BASE+timedelta(seconds=second);volume+=10 if second<=1080 else 40
                q.volume=volume;q.ltp=10 if second<=1080 else 14.1
                q.exchange_feed_time=q.exchange_trade_time=now.strftime('%d-%b-%Y %H:%M:%S')
                q2.volume=q.volume;q2.ltp=q.ltp;q2.exchange_feed_time=q.exchange_feed_time;q2.exchange_trade_time=q.exchange_trade_time
                found+=scanner.observe(chain,100,now,spot_at=now.isoformat())
            self.assertEqual(order,['saved','notification_failed','saved','notification_failed'])
            self.assertEqual(len(found),2);self.assertEqual(found[0]['signal_id'],9)
            self.assertEqual(found[0]['execution'],'observation_only')
            self.assertEqual(scanner.observe(chain,100,now,spot_at=now.isoformat()),[])
            # Broker-successful but undated quotes cannot count as fresh coverage.
            q.exchange_feed_time=q2.exchange_feed_time=None
            scanner.observe(chain,100,now+timedelta(seconds=15),spot_at=now.isoformat())
            self.assertEqual(scanner.coverage['NIFTY']['fresh_contracts'],0)

    def test_after_close_weekend_and_stale_chain_are_not_live(self):
        scanner=cas.CasBlastScanner();chain=SimpleNamespace(index=SimpleNamespace(name='NIFTY'),_last_poll=0)
        health=patch.object(cas.celestial,'health');health.start();self.addCleanup(health.stop)
        self.assertEqual(scanner.observe(chain,100,BASE.replace(hour=23)),[])
        self.assertEqual(scanner.observe(chain,100,BASE+timedelta(days=3)),[])
        with patch.object(cas.time,'monotonic',return_value=100):
            self.assertEqual(scanner.observe(chain,100,BASE),[])
            self.assertEqual(scanner.coverage['NIFTY']['status'],'stale_chain')


if __name__=='__main__':unittest.main()
