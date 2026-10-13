"""Offline rules for scheduled and market-shock-triggered forecast requests."""
from __future__ import annotations
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from precision_lab.core import EvidenceError,instant
from precision_lab.reforecast_events import EventGate,EventJournal,TriggerPolicy

NOW='2026-10-13T13:37:04+05:30'
ORIGINAL='F-ORIGINAL-93'
ACTIVE='F-ACTIVE-94'
PUBLISHED='2026-10-13T13:30:02+05:30'


def sig(value, *, observed=NOW,available=NOW,sessions=20,source='research-producer'):
    return {'status':'AVAILABLE','value':value,'observed_at':observed,
            'available_at':available,'source':source,'baseline_id':'time-of-day-20d-v1',
            'baseline_sessions':sessions}


def shock(gate, now=NOW, signals=None, confirmations=None, **kw):
    return gate.shock(now=now,signals=signals or {},forecast_id=kw.get('forecast_id',ACTIVE),
                      original_id=ORIGINAL,published_at=PUBLISHED,
                      next_target_at=kw.get('next_target_at','2026-10-13T13:40:00+05:30'),
                      confirmations=confirmations)


def scheduled(gate, error=5, now='2026-10-13T13:45:02+05:30', **kw):
    target='2026-10-13T13:45:00+05:30'
    return gate.scheduled(now=now,target_at=kw.get('target_at',target),
      actual_at=kw.get('actual_at',target),actual_available_at=kw.get('available_at',now),
      actual=24000+error,projected=24000,forecast_id=ACTIVE,
      original_id=ORIGINAL,published_at=PUBLISHED,
      next_target_at='2026-10-13T13:50:00+05:30')


class ScheduledTests(unittest.TestCase):
    def test_default_disabled(self):
        self.assertEqual(scheduled(EventGate())['state'],'DISABLED')
    def test_strict_five_trigger(self):
        r=scheduled(EventGate(TriggerPolicy(enabled=True)),5)
        self.assertEqual(r['state'],'REQUEST_REFORECAST')
        self.assertEqual(r['event']['deviation_points'],5)
    def test_negative_five_trigger(self):
        self.assertEqual(scheduled(EventGate(TriggerPolicy(enabled=True)),-5)['state'],'REQUEST_REFORECAST')
    def test_under_five_does_not_trigger(self):
        self.assertEqual(scheduled(EventGate(TriggerPolicy(enabled=True)),4.9)['state'],'CONTINUE')
    def test_dedupe_review(self):
        gate=EventGate(TriggerPolicy(enabled=True))
        self.assertEqual(scheduled(gate,6)['state'],'REQUEST_REFORECAST')
        self.assertEqual(scheduled(gate,6)['state'],'ALREADY_REVIEWED')
    def test_exact_actual_match(self):
        gate=EventGate(TriggerPolicy(enabled=True))
        self.assertEqual(scheduled(gate,6,actual_at='2026-10-13T13:44:59+05:30')['state'],'MISSING_MATCHED_ACTUAL')
        self.assertEqual(scheduled(gate,6)['state'],'REQUEST_REFORECAST')
    def test_actual_cannot_be_future(self):
        r=scheduled(EventGate(TriggerPolicy(enabled=True)),6,
            available_at='2026-10-13T13:45:20+05:30')
        self.assertEqual(r['state'],'MISSING_MATCHED_ACTUAL')
    def test_late_review_fails(self):
        r=scheduled(EventGate(TriggerPolicy(enabled=True)),6,
            now='2026-10-13T13:48:00+05:30')
        self.assertEqual(r['state'],'LATE_OR_EARLY_REVIEW')
    def test_non_quarter_checkpoint(self):
        r=scheduled(EventGate(TriggerPolicy(enabled=True)),6,
                    target_at='2026-10-13T13:40:00+05:30')
        self.assertEqual(r['state'],'NOT_REVIEW_CHECKPOINT')
    def test_target_before_publication_refused(self):
        target='2026-10-13T13:30:00+05:30'
        with self.assertRaises(EvidenceError):
            scheduled(EventGate(TriggerPolicy(enabled=True)),6,now='2026-10-13T13:30:05+05:30',
                      target_at=target,actual_at=target)
    def test_no_future_targets_refused(self):
        r=EventGate(TriggerPolicy(enabled=True))
        with self.assertRaises(EvidenceError): r.shock(now=NOW,signals={},
         forecast_id=ACTIVE,original_id=ORIGINAL,published_at=PUBLISHED,
         next_target_at='2026-10-13T13:35:00+05:30')
    def test_next_target_must_be_future(self):
        with self.assertRaises(EvidenceError):
            shock(EventGate(TriggerPolicy(enabled=True)),next_target_at='2026-10-13T13:35:00+05:30',
                  signals={'price_z':sig(3),'volume_ratio':sig(3)})
    def test_publication_must_precede_next_target(self):
        with tempfile.TemporaryDirectory() as d:
            j=EventJournal(Path(d)/'test.reforecast-events.sqlite3')
            ev=shock(EventGate(TriggerPolicy(enabled=True)),signals={
                'price_z':sig(3),'volume_ratio':sig(3)})['event']
            j.record(ev)
            with self.assertRaises(EvidenceError):
                j.record_receipt(ev['id'],recorded_at='2026-10-13T13:41:00+05:30',
                 publication_at='2026-10-13T13:40:00+05:30',revised_forecast_id='V2',outcome='PUBLISHED')
            j.close()
    def test_market_close_no_future(self):
        self.assertEqual(scheduled(EventGate(TriggerPolicy(enabled=True)),6,
                         now='2026-10-13T15:30:02+05:30',target_at='2026-10-13T15:30:00+05:30',
                         actual_at='2026-10-13T15:30:00+05:30')['state'],'NOT_REVIEW_CHECKPOINT')
    def test_negative_price_rejected(self):
        g=EventGate(TriggerPolicy(enabled=True))
        with self.assertRaises(EvidenceError):
            g.scheduled(now='2026-10-13T13:45:02+05:30',
                target_at='2026-10-13T13:45:00+05:30',actual_at='2026-10-13T13:45:00+05:30',
                actual_available_at='2026-10-13T13:45:02+05:30',actual=-1,projected=24000,
                forecast_id=ACTIVE,original_id=ORIGINAL,published_at=PUBLISHED,
                next_target_at='2026-10-13T13:50:00+05:30')


class ShockTests(unittest.TestCase):
    def setUp(self): self.g=EventGate(TriggerPolicy(enabled=True))
    def test_disabled_by_default(self):
        self.assertEqual(shock(EventGate(),signals={'price_z':sig(8)})['state'],'DISABLED')
    def test_mid_candle_immediate_shock(self):
        r=shock(self.g,signals={'price_z':sig(3),'volume_ratio':sig(2.8)})
        self.assertEqual(r['state'],'REQUEST_REFORECAST')
        self.assertEqual(r['event']['kind'],'MARKET_SHOCK')
        self.assertIn('PRICE_AND_VOLUME',r['event']['reasons'])
    def test_negative_price_shock(self):
        r=shock(self.g,signals={'price_z':sig(-3),'volume_ratio':sig(3)})
        self.assertEqual(r['state'],'REQUEST_REFORECAST')
    def test_shock_requires_corrob(self):
        self.assertEqual(shock(self.g,signals={'price_z':sig(3)})['state'],'NO_CONFIRMED_SHOCK')
    def test_opposite_breadth_not_confirmation(self):
        self.assertEqual(shock(self.g,signals={'price_z':sig(3),'breadth_z':sig(-3)})['state'],'NO_CONFIRMED_SHOCK')
    def test_aligned_breadth_confirms(self):
        r=shock(self.g,signals={'price_z':sig(-3),'breadth_z':sig(-2)})
        self.assertIn('PRICE_AND_WEIGHTED_BREADTH',r['event']['reasons'])
    def test_oi_and_price_confirm(self):
        r=shock(self.g,signals={'price_z':sig(3),'oi_velocity_z':sig(4)})
        self.assertIn('PRICE_AND_OI_VELOCITY',r['event']['reasons'])
    def test_gamma_pressure_not_entry_signal(self):
        r=shock(self.g,signals={'price_z':sig(-2.6),'gamma_pressure_z':sig(3)})
        self.assertIn('PRICE_AND_GAMMA_PRESSURE',r['event']['reasons'])
        self.assertNotIn('trade_signal',r['event'])
    def test_standalone_volume_and_options(self):
        r=shock(self.g,signals={'volume_ratio':sig(4.1),'oi_velocity_z':sig(3)})
        self.assertIn('VOLUME_AND_OPTIONS_PRESSURE',r['event']['reasons'])
    def test_no_baseline_fails(self):
        self.assertEqual(shock(self.g,signals={'price_z':sig(6,sessions=2),'volume_ratio':sig(5)})['state'],
                         'NO_CONFIRMED_SHOCK')
    def test_stale_volume_does_not_confirm(self):
        older='2026-10-13T13:36:00+05:30'
        r=shock(self.g,signals={'price_z':sig(3),'volume_ratio':sig(3,observed=older)})
        self.assertEqual(r['state'],'NO_CONFIRMED_SHOCK')
    def test_future_oi_does_not_confirm(self):
        after='2026-10-13T13:37:20+05:30'
        r=shock(self.g,signals={'price_z':sig(3),'oi_velocity_z':sig(3,available=after)})
        self.assertEqual(r['state'],'NO_CONFIRMED_SHOCK')
    def test_price_only_extreme_requires_verified_ticks(self):
        r=shock(self.g,signals={'price_z':sig(4.2)})
        self.assertEqual(r['state'],'NO_CONFIRMED_SHOCK')
        r=shock(self.g,signals={'price_z':sig(4.2)},confirmations={
            'distinct_ticks':3,'span_seconds':2.5,'verified_exchange_sequence':True})
        self.assertEqual(r['state'],'REQUEST_REFORECAST')
    def test_fake_tick_proof_rejected(self):
        r=shock(self.g,signals={'price_z':sig(5)},confirmations={
            'distinct_ticks':3,'span_seconds':3,'verified_exchange_sequence':False})
        self.assertEqual(r['state'],'NO_CONFIRMED_SHOCK')
    def test_dedupe_cooldown_not_15_min(self):
        self.assertEqual(shock(self.g,signals={'price_z':sig(3),'volume_ratio':sig(3)})['state'],'REQUEST_REFORECAST')
        self.assertEqual(shock(self.g,signals={'price_z':sig(3),'volume_ratio':sig(3)})['state'],'SHOCK_COOLDOWN')
        shifted='2026-10-13T13:38:00+05:30'
        self.assertEqual(shock(self.g,now=shifted,signals={'price_z':sig(3,observed=shifted,available=shifted),
            'volume_ratio':sig(3,observed=shifted,available=shifted)})['state'],'REQUEST_REFORECAST')
    def test_no_trade_outside_market_hours(self):
        self.assertEqual(shock(self.g,now='2026-10-13T15:31:02+05:30',signals={})['state'],'OUTSIDE_SESSION')
    def test_unknown_signal_refused(self):
        with self.assertRaises(EvidenceError): shock(self.g,signals={'spoofed_gamma_signal':sig(10)})
    def test_producer_cannot_claim_future_values(self):
        r=shock(self.g,signals={'price_z':sig(3,observed='2026-10-13T13:38:00+05:30'),
                                'volume_ratio':sig(4)})
        self.assertEqual(r['state'],'NO_CONFIRMED_SHOCK')
    def test_invalid_policy_threshold_refused(self):
        with self.assertRaises(EvidenceError): TriggerPolicy(enabled=True,price_z=-1)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.j=EventJournal(Path(self.t.name)/'audit.reforecast-events.sqlite3')
        self.event=shock(EventGate(TriggerPolicy(enabled=True)),signals={
            'price_z':sig(3),'volume_ratio':sig(3)})['event']
    def tearDown(self): self.j.close(); self.t.cleanup()
    def test_immutable_idempotent(self):
        self.assertTrue(self.j.record(self.event));self.assertFalse(self.j.record(self.event))
        self.assertEqual(len(self.j.recent()),1)
    def test_hash_mismatch_refused(self):
        altered=dict(self.event);altered['kind']='IMAGINARY'
        with self.assertRaises(EvidenceError): self.j.record(altered)
    def test_sql_mutation_refused(self):
        self.j.record(self.event)
        with self.assertRaises(sqlite3.IntegrityError): self.j.db.execute('DELETE FROM triggers')
    def test_published_requires_publication_receipt(self):
        self.j.record(self.event)
        with self.assertRaises(EvidenceError):
            self.j.record_receipt(self.event['id'],recorded_at='2026-10-13T13:38:00+05:30',
                                  outcome='PUBLISHED')
    def test_future_publication_refused(self):
        self.j.record(self.event)
        with self.assertRaises(EvidenceError):
            self.j.record_receipt(self.event['id'],recorded_at='2026-10-13T13:38:00+05:30',
                                  publication_at='2026-10-13T13:38:30+05:30',
                                  revised_forecast_id='V2',outcome='PUBLISHED')
    def test_nonpublication_receipt(self):
        self.j.record(self.event)
        self.j.record_receipt(self.event['id'],recorded_at='2026-10-13T13:38:00+05:30',
                              outcome='NO_FITTED_MODEL')
        self.assertEqual(self.j.recent()[0]['receipt']['outcome'],'NO_FITTED_MODEL')
    def test_valid_published_receipt(self):
        self.j.record(self.event)
        self.j.record_receipt(self.event['id'],recorded_at='2026-10-13T13:38:00+05:30',
                              publication_at='2026-10-13T13:37:06+05:30',
                              revised_forecast_id='F-REV-V2',outcome='PUBLISHED')
        self.assertEqual(self.j.recent()[0]['receipt']['revised_forecast_id'],'F-REV-V2')
    def test_receipt_not_mutable(self):
        self.j.record(self.event)
        self.j.record_receipt(self.event['id'],recorded_at='2026-10-13T13:38:00+05:30',
                              outcome='NO_FITTED_MODEL')
        with self.assertRaises(sqlite3.IntegrityError):
            self.j.db.execute('UPDATE receipts SET body="changed"')
    def test_research_db_naming(self):
        with self.assertRaises(EvidenceError): EventJournal(Path(self.t.name)/'forecast.db')
    def test_limit_bounded(self):
        with self.assertRaises(EvidenceError): self.j.recent(200)

if __name__=='__main__': unittest.main()
