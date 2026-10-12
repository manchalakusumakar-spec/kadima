import copy
from datetime import timedelta
from threading import Lock
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from precision_lab.cache_adapter import capture, technical_features, radar_quotes, ema, rsi
from precision_lab.core import EvidenceError, InsufficientEvidence, instant, encode

NOW = instant('2026-10-12T10:30:02+05:30')
ORIGIN = NOW.replace(second=0)


def fixture():
    # Complete previous-session ending + present-session starting, no gap fill.
    starts = [instant('2026-10-09T12:30:00+05:30')+timedelta(minutes=5*i) for i in range(36)]
    starts += [instant('2026-10-12T09:15:00+05:30')+timedelta(minutes=5*i) for i in range(15)]
    bars = [{'interval':5,'closed':1,'start_ts':t.isoformat(),'open':24000+i,
             'high':24002+i,'low':23999+i,'close':24001+i} for i,t in enumerate(starts)]
    m = {'version':'SYNTHETIC-ONLY','source':'SYNTHETIC','effective_date':'2026-10-01',
         'valid_through':'2026-10-31','available_at':'2026-10-01T08:00:00+05:30',
         'weights':{f'S{i}':.02 for i in range(50)}}
    members={str(i):dict(symbol=f'S{i}',weight=.02) for i in range(50)}
    ts=NOW.timestamp()
    states={str(i):dict(price=101.,ts=ts, samples=[(0,ts-300,100.),(1,ts,101.)]) for i in range(50)}
    radar=NS(lock=Lock(),reason=None,source='SYNTHETIC',valid_from=instant(m['available_at']).date(),
             valid_until=instant('2026-10-31T00:00:00+05:30').date(),session=NOW.date(),members=members,states=states)
    index=lambda token:NS(is_live_tick=True, spot=24052.,last_updated=NOW,chain=NS(index=NS(token=token)))
    e=NS(states={'NIFTY':index('N'),'BANKNIFTY':index('B'),'SENSEX':index('S')},constituent_radar=radar)
    return e,m,bars


class CacheAdapter(unittest.TestCase):
    def setUp(self): self.e,self.m,self.b=fixture()
    def run_capture(self, **kwargs):
        return capture(self.e,lambda token,limit,until:copy.deepcopy(self.b),self.m,
                       source_id='SYNTHETIC-SOURCE',config_id='SYNTHETIC-CONFIG',clock=lambda:NOW,**kwargs)
    def test_native_cache_packet(self):
        s=self.run_capture(); _,q=encode(s)
        self.assertEqual(s['capture_metadata']['breadth']['fresh_count'],50)
        self.assertEqual(q['weighted_ret_5m_bps'],'AVAILABLE')
        self.assertEqual(q['pcr_change_5m'],'MISSING')
        self.assertEqual(q['banknifty_ret_5m_bps'],'AVAILABLE')
    def test_no_trading_fields_needed(self):
        s=self.run_capture(); self.assertFalse(s['capture_metadata']['trading_authority'])
    def test_socket_calls_impossible(self):
        with patch('socket.socket',side_effect=AssertionError('network forbidden')): self.run_capture()
    def test_radar_not_mutated(self):
        before=copy.deepcopy(self.e.constituent_radar.states); self.run_capture()
        self.assertEqual(self.e.constituent_radar.states,before)
    def test_index_receipt_label_not_exchange_claim(self):
        self.assertIn('EXCHANGE_EVENT_TIME_NOT_CAPTURED',self.run_capture()['capture_metadata']['index_timestamp_kind'])
    def test_absent_radar_blocks(self):
        self.e.constituent_radar=None
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_missing_one_company_blocks(self):
        del self.e.constituent_radar.states['0']
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_partial_research_explicit(self):
        del self.e.constituent_radar.states['0']; s=self.run_capture(require_all50=False)
        self.assertAlmostEqual(s['features']['constituent_weight_coverage']['value'],.98)
    def test_manifest_mismatch(self):
        self.e.constituent_radar.members['0']['weight']=.03
        with self.assertRaises(EvidenceError): self.run_capture()
    def test_expired_manifest(self):
        self.m['valid_through']='2026-10-11'
        with self.assertRaises(EvidenceError): self.run_capture()
    def test_wrong_session_radar(self):
        self.e.constituent_radar.session=instant('2026-10-09T10:30:00+05:30').date()
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_short_warmup(self):
        self.b=self.b[-20:]
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_latest_bar_missing(self):
        self.b.pop()
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_intraday_gap(self):
        del self.b[-3]
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_forming_bar_not_used(self):
        s=self.run_capture(); b=copy.deepcopy(self.b[-1]); b.update(closed=0,close=1e9)
        self.b.append(b); self.assertEqual(self.run_capture(),s)
    def test_bad_ohlc(self):
        self.b[-1]['low']=1e9
        with self.assertRaises(EvidenceError): self.run_capture()
    def test_duplicate_bar(self):
        self.b.append(copy.deepcopy(self.b[-1]))
        with self.assertRaises(EvidenceError): self.run_capture()
    def test_anchor_stale(self):
        self.e.states['NIFTY'].last_updated=NOW-timedelta(seconds=31)
        with self.assertRaises(EvidenceError): self.run_capture()
    def test_anchor_non_live(self):
        self.e.states['NIFTY'].is_live_tick=False
        with self.assertRaises(InsufficientEvidence): self.run_capture()
    def test_enrichment_cannot_override_price_features(self):
        with self.assertRaises(EvidenceError): self.run_capture(enrichments={'ret_5m_bps':{}})
    def test_greeks_need_contract_scope(self):
        with self.assertRaises(EvidenceError): self.run_capture(enrichments={'atm_iv_pct':{'status':'AVAILABLE'}})
    def test_scoped_greeks_available(self):
        x={'value':12.,'status':'AVAILABLE','observed_at':NOW.isoformat(),'available_at':NOW.isoformat(),
           'source':'SYNTHETIC','contract_scope':'SYNTHETIC-NIFTY-20261013-24050CE'}
        s=self.run_capture(enrichments={'atm_iv_pct':x}); self.assertEqual(encode(s)[1]['atm_iv_pct'],'AVAILABLE')
    def test_stale_greeks_excluded(self):
        x={'value':12.,'status':'AVAILABLE','observed_at':(NOW-timedelta(seconds=91)).isoformat(),
           'available_at':NOW.isoformat(),'source':'SYNTHETIC','contract_scope':'SYNTHETIC'}
        s=self.run_capture(enrichments={'atm_iv_pct':x}); self.assertEqual(encode(s)[1]['atm_iv_pct'],'STALE')
    def test_vwap_requires_volume_source(self):
        with self.assertRaises(EvidenceError): self.run_capture(enrichments={'vwap_proxy_gap_bps':{'status':'AVAILABLE'}})
    def test_capture_deadline(self):
        clock=iter([NOW,NOW+timedelta(seconds=6)])
        with self.assertRaises(EvidenceError):
            capture(self.e,lambda *args:self.b,self.m,source_id='s',config_id='c',clock=lambda:next(clock))
    def test_availability_after_computation(self):
        clock=iter([NOW, NOW+timedelta(seconds=1), NOW+timedelta(seconds=2)])
        s=capture(self.e,lambda *args:self.b,self.m,source_id='s',config_id='c',clock=lambda:next(clock))
        self.assertEqual(s['issued_at'],(NOW+timedelta(seconds=2)).isoformat())
        self.assertEqual(s['features']['weighted_ret_5m_bps']['available_at'],s['issued_at'])
    def test_computation_deadline(self):
        clock=iter([NOW, NOW+timedelta(seconds=1), NOW+timedelta(seconds=6)])
        with self.assertRaises(EvidenceError):
            capture(self.e,lambda *args:self.b,self.m,source_id='s',config_id='c',clock=lambda:next(clock))
    def test_indicator_values(self):
        self.assertEqual(ema([100.]*50,20)[-1],100.)
        self.assertEqual(rsi([100.]*50),50.)
        self.assertEqual(rsi(list(range(1,51))),100.)


if __name__=='__main__': unittest.main()
