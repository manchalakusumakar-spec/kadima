import copy
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from kadima import priority_sessions as sessions, market_session_gate as entry

spec = importlib.util.spec_from_file_location('priority_runner_tests', Path('scheduler/runner.py'))
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
POLICY = dict(schema=1, valid_from='2026-09-19', valid_through='2026-12-31',
              holidays=['2026-10-02', '2026-10-20', '2026-11-10', '2026-11-24', '2026-12-25'],
              sources=['fixture'], arm_at='09:00', entries_at='09:15', stop_entries_at='15:35')
NOW = datetime(2026, 9, 28, 19, 10, tzinfo=entry.IST)


class Clock(datetime):
    current = NOW
    @classmethod
    def now(cls, tz=None): return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class Client:
    def __init__(self, at):
        self.gd = dict(config=dict(revision=0, auto_trade_enabled=False, kill_latched=False), system={})
        self.sd = dict(controls=dict(revision=0, live_enabled=False), kill_latched=False,
                       celestial_rollout=True, fast_master_permission=True,
                       broker_order_permission=True, worker_alive=True)
        self.calls = []; self.lost = None
        self.stamp(at)

    def stamp(self, at):
        h = dict(ok=True, ts=at.isoformat())
        self.gd.update(ts=at.isoformat(), system=dict(news=h.copy(), reconciliation=h.copy(),
            mcx_live=dict(h, live_requested=True, fresh_futures=2, fresh_options=2)))
        self.sd.update(ts=at.isoformat(), worker=h.copy())

    def call(self, action, payload=None):
        self.calls.append(action)
        if action == 'global': return copy.deepcopy(self.gd)
        if action == 'stock': return copy.deepcopy(self.sd)
        if action in ('resume', 'pause'):
            cfg = self.gd['config']; cfg['revision'] += 1
            cfg['auto_trade_enabled'] = action == 'resume'
        elif action == 'stock_config':
            cfg = self.sd['controls']
            assert cfg['revision'] == payload['expected_revision']
            cfg.update(payload['patch']); cfg['revision'] += 1
        else: raise AssertionError(action)
        if self.lost == action: raise runner.Blocked('lost_response')
        return cfg.copy()


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.state = {}; self.client = Client(NOW); self.lease = None
        self.patches = [patch.object(runner, 'datetime', Clock),
                        patch.object(runner, 'revoke', side_effect=lambda reason: self.save_lease(None, dict(allowed=False))),
                        patch.object(runner, 'atomic', side_effect=self.save_lease)]
        Clock.current = NOW
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def save_lease(self, path, value, *args): self.lease = value
    def run_at(self, at):
        Clock.current = at; self.client.stamp(at)
        return runner.iteration(self.client, POLICY, self.state, at, lambda v: None)

    def test_evening_mcx_does_not_arm_equity_or_stock(self):
        out = self.run_at(NOW)
        self.assertEqual(out['mcx'], 'armed_subject_to_entry_gates')
        self.assertEqual(self.lease['lanes'], dict(mcx=True, equity=False))
        self.assertNotIn('stock_config', self.client.calls)

    def test_equity_close_leaves_mcx_running_and_pauses_stock(self):
        self.run_at(NOW.replace(hour=14))
        self.client.calls.clear()
        self.run_at(NOW)
        self.assertTrue(self.client.gd['config']['auto_trade_enabled'])
        self.assertFalse(self.client.sd['controls']['live_enabled'])
        self.assertNotIn('pause', self.client.calls)
        self.assertEqual(self.state['phase'], 'ACTIVE')

    def test_bad_mcx_health_does_not_block_healthy_equity(self):
        at = NOW.replace(hour=14); Clock.current = at; self.client.stamp(at)
        self.client.gd['system']['mcx_live']['fresh_options'] = 0
        runner.iteration(self.client, POLICY, self.state, at, lambda v: None)
        self.assertEqual(self.lease['lanes'], dict(equity=True, mcx=False))

    def test_manual_global_pause_is_binding_for_both_lanes(self):
        self.run_at(NOW)
        self.client.gd['config'].update(auto_trade_enabled=False, revision=2)
        self.client.calls.clear()
        self.run_at(NOW+timedelta(minutes=1))
        self.assertEqual(self.state['phase'], 'OPERATOR_PAUSED')
        self.assertFalse(self.lease['allowed']); self.assertNotIn('resume', self.client.calls)

    def test_manual_stock_pause_never_gets_overwritten(self):
        at = NOW.replace(hour=14); self.run_at(at)
        self.client.sd['controls'].update(live_enabled=False, revision=2)
        self.run_at(at+timedelta(minutes=1))
        self.assertEqual(self.state['phase'], 'OPERATOR_PAUSED')
        self.assertFalse(self.lease['allowed'])

    def test_lost_success_response_recovers_without_repeating_resume(self):
        self.client.lost = 'resume'
        with self.assertRaises(runner.Blocked): self.run_at(NOW)
        self.assertIn('pending', self.state)
        self.assertFalse(self.lease['allowed'])
        self.client.lost = None
        self.run_at(NOW+timedelta(seconds=30))
        self.assertEqual(self.client.calls.count('resume'), 1)
        self.assertTrue(self.lease['lanes']['mcx'])

    def test_ambiguous_unapplied_request_never_retries(self):
        self.run_at(NOW)
        before = copy.deepcopy(self.state['expected']); after = dict(before, global_revision=2, global_enabled=False)
        self.state['pending'] = dict(before=before, after=after)
        self.client.calls.clear()
        result = self.run_at(NOW+timedelta(minutes=1))
        self.assertEqual(result['mcx'], 'control_write_confirmation_pending')
        self.assertFalse(self.lease['allowed']); self.assertNotIn('pause', self.client.calls)

    def test_mcx_evening_holiday_is_independent_of_equity_holiday(self):
        at = NOW.replace(month=10, day=20)
        self.run_at(at)
        self.assertEqual(self.lease['lanes'], dict(mcx=True, equity=False))

    def test_both_closed_pauses_intents(self):
        self.run_at(NOW); self.run_at(NOW.replace(hour=22, minute=35))
        self.assertFalse(self.lease['allowed'])
        self.assertFalse(self.client.gd['config']['auto_trade_enabled'])

    def test_calendar_expiry_cannot_issue_lease(self):
        self.run_at(NOW.replace(year=2027))
        self.assertFalse(self.lease['allowed'])
        self.assertNotIn('resume', self.client.calls)


class EntryGate(unittest.TestCase):
    def setUp(self):
        self.permit = dict(allowed=True, lanes=dict(equity=False, mcx=True),
            day=NOW.date().isoformat(), boot_id='boot', issued_mono=100, expires_mono=250,
            issued_at=NOW.isoformat())
        self.patches = [patch.dict(os.environ, KADIMA_PRIORITY_TRADING='1'),
            patch.object(entry, 'read_json', side_effect=lambda path: POLICY if path == entry.POLICY else self.permit),
            patch.object(entry.time, 'monotonic', return_value=101),
            patch.object(Path, 'read_text', return_value='boot')]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_evening_mcx_permit_never_authorizes_nfo(self):
        self.assertIsNone(entry.entry_block('MCX', NOW))
        self.assertIsNotNone(entry.entry_block('NFO', NOW))

    def test_unrecognized_exchange_blocks(self):
        self.assertIsNotNone(entry.entry_block('NSE', NOW))

    def test_cutoff_and_expired_lease_fail_closed(self):
        self.assertIsNotNone(entry.entry_block('MCX', NOW.replace(hour=22, minute=35)))
        self.assertIsNotNone(entry.entry_block('MCX', NOW+timedelta(minutes=3)))

    def test_missing_lane_does_not_inherit_global_permission(self):
        self.permit.pop('lanes')
        self.assertEqual(entry.entry_block('MCX', NOW), 'lane_permit_not_granted')

    def test_broker_cutoff_and_mcx_holiday_bounds(self):
        self.assertIsNone(sessions.calendar_reason('NFO', NOW.replace(hour=15, minute=14)))
        self.assertIsNotNone(sessions.calendar_reason('NFO', NOW.replace(hour=15, minute=15)))
        holiday = NOW.replace(month=10, day=20)
        self.assertIsNotNone(sessions.calendar_reason('MCX', holiday.replace(hour=16)))
        self.assertIsNone(sessions.calendar_reason('MCX', holiday.replace(hour=17)))
        self.assertIsNotNone(sessions.calendar_reason('MCX', NOW.replace(month=10, day=2)))
        self.assertTrue(sessions.exit_due('MCX', NOW.replace(hour=22, minute=40)))


if __name__ == '__main__': unittest.main()
