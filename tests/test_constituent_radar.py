import ast
from datetime import datetime, timedelta
import json
from pathlib import Path
import queue
import time
import unittest
from kadima.constituent_radar import ConstituentRadar, IST

NOW = datetime(2026,10,7,10,tzinfo=IST)

def fixture():
    members = [dict(symbol='S'+str(i),weight=.02,sector='BANK' if i<25 else 'OTHER') for i in range(50)]
    rows = [dict(symbol=m['symbol']+'-EQ',exch_seg='NSE',token=str(i)) for i,m in enumerate(members)]
    manifest = dict(valid_from='2026-10-07',valid_until='2026-10-08',source='synthetic test only',members=members)
    return manifest,rows

def radar():
    m,r=fixture()
    return ConstituentRadar(m,r,NOW.date())

def tick(r,i,seconds,price=100,volume=None):
    at=NOW+timedelta(seconds=seconds)
    r.on_tick(dict(token=str(i),ts=at,exchange_ts=at,exchange_type=1,ltp=price,volume=seconds+1000 if volume is None else volume))

class Tests(unittest.TestCase):
    def test_weights_and_resolution(self):
        for change in ('count','sum','duplicate','nan','token','expired'):
            m,rows=fixture()
            if change=='count':m['members'].pop()
            if change=='sum':m['members'][0]['weight']=.5
            if change=='duplicate':m['members'][1]['symbol']='S0'
            if change=='nan':m['members'][0]['weight']=float('nan')
            if change=='token':rows.pop()
            if change=='expired':m['valid_until']='2026-10-06'
            self.assertEqual(ConstituentRadar(m,rows,NOW.date()).tokens,[],change)

    def test_weighted_returns_and_vwap(self):
        r=radar()
        for i in range(50):
            tick(r,i,0)
            tick(r,i,60,101 if i<25 else 99)
        s=r.snapshot(NOW+timedelta(seconds=60))
        self.assertAlmostEqual(s['horizons']['1']['weighted_return_bps'],0)
        self.assertAlmostEqual(s['horizons']['1']['positive_weight'],.5)
        self.assertIsNone(s['probability'])
        self.assertFalse(s['horizons']['3']['ready'])
        self.assertEqual(len(s['horizons']['1']['top_positive']),5)

    def test_weighted_not_equal_vote(self):
        m,rows=fixture();m['members'][0]['weight']=.51
        for member in m['members'][1:]:member['weight']=.01
        r=ConstituentRadar(m,rows,NOW.date())
        for i in range(50):
            tick(r,i,0);tick(r,i,60,102 if i==0 else 99)
        self.assertAlmostEqual(r.snapshot(NOW+timedelta(seconds=60))['horizons']['1']['weighted_return_bps'],53)

    def test_partial_and_stale_do_not_invent_direction(self):
        r=radar()
        for i in range(49):tick(r,i,0);tick(r,i,60,101)
        s=r.snapshot(NOW+timedelta(seconds=60))
        self.assertEqual(s['status'],'degraded')
        self.assertIsNone(s['horizons']['1']['direction'])
        self.assertEqual(r.snapshot(NOW+timedelta(seconds=66))['fresh_count'],0)

    def test_bad_ticks_and_volume_reset(self):
        r=radar();tick(r,0,0);tick(r,0,1,volume=900)
        self.assertEqual(r.states['0']['volume'],1000)
        for patch in ({'exchange_type':2},{'ltp':float('nan')},{'exchange_ts':NOW-timedelta(seconds=30)},{'exchange_ts':None}):
            t=dict(token='1',ts=NOW,exchange_ts=NOW,exchange_type=1,ltp=100,volume=1000);t.update(patch);r.on_tick(t)
        self.assertNotIn('1',r.states)

    def test_daily_reset_and_expiry(self):
        r=radar();tick(r,0,0);tick(r,1,86400,volume=2)
        self.assertNotIn('0',r.states)
        self.assertEqual(r.snapshot(NOW+timedelta(days=2))['reason'],'expired_weights')

    def test_bounded_samples_and_gap(self):
        r=radar()
        for n in range(4000):tick(r,0,n)
        self.assertLessEqual(len(r.states['0']['samples']),62)
        r=radar()
        for i in range(50):tick(r,i,0);tick(r,i,120)
        self.assertFalse(r.snapshot(NOW+timedelta(seconds=120))['horizons']['1']['ready'])

    def test_feed_skips_radar_disk_writes_and_deduplicates(self):
        # Execute actual feed methods without importing broker SDK or starting threads.
        tree=ast.parse(Path('kadima/feed.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef))
        methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('subscribe','_handle_tick')]
        class Audit:
            def log(self,*args,**kwargs):pass
        import math
        ns=dict(datetime=datetime,IST=IST,time=time,math=math,queue=queue,audit=Audit())
        exec(compile(ast.Module(body=methods,type_ignores=[]),'feed-methods','exec'),ns)
        class Feed:pass
        f=Feed();f._subs={};f.transient_tokens={'42'};f._writer_q=queue.Queue();seen=[];f.on_tick=seen.append
        ns['subscribe'](f,1,['42','42']);ns['subscribe'](f,1,['42','43'])
        self.assertEqual(f._subs['1'],['42','43'])
        msg=dict(token='42',last_traded_price=10000,volume_trade_for_the_day=100,exchange_type=1,exchange_timestamp=NOW.timestamp()*1000)
        ns['_handle_tick'](f,None,msg)
        self.assertEqual(f._writer_q.qsize(),0);self.assertEqual(len(seen),1)
        msg['token']='43';ns['_handle_tick'](f,None,msg)
        self.assertEqual(f._writer_q.qsize(),1)

if __name__=='__main__':unittest.main()
