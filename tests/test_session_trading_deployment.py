"""Offline deployment admission, restart sequencing and scheduler rollback."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_session_trading_deploy_test', ROOT/'momentum_recovery/enable_session_trading.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
gate = m.load('session_auto/gate.py')
POLICY = json.loads((ROOT/'session_auto/policy.json').read_bytes())
NOW = datetime(2026, 9, 28, 12, 35, tzinfo=timezone.utc)


class Boundaries(unittest.TestCase):
    def test_after_hours_allows_evening_but_not_market_or_near_start(self):
        for clock, allowed in (('18:05', True), ('23:59', True), ('00:01', True), ('08:29', True),
                ('08:30', False), ('09:00', False), ('09:15', False), ('15:35', False), ('15:59', False), ('16:00', True)):
            at = datetime.fromisoformat('2026-09-28T'+clock+':00+05:30')
            with self.subTest(clock=clock):
                if allowed:
                    m.after_hours(gate, POLICY, at)
                else:
                    with self.assertRaisesRegex(m.Blocked, 'after_hours'):
                        m.after_hours(gate, POLICY, at)

    def test_no_extension_beyond_reviewed_calendar(self):
        with self.assertRaisesRegex(m.Blocked, 'calendar_update_required'):
            m.after_hours(gate, POLICY, NOW.replace(year=2027))

    def test_invalid_calendar_not_used_for_arming(self):
        with self.assertRaises(ValueError):
            m.after_hours(gate, dict(POLICY, stop_entries_at='20:00'), NOW)

    def test_weekend_and_holiday_are_still_calendar_exclusions(self):
        for day in ('2026-10-02', '2026-10-03'):
            at = datetime.fromisoformat(day+'T18:00:00+05:30')
            m.after_hours(gate, POLICY, at)
            self.assertEqual('non_trading_day', gate.calendar_reason(POLICY, at))

    def test_existing_gates_and_nonselected_lanes_are_preserved(self):
        env = dict.fromkeys(m.ENABLED, '1')
        m.live_environment(env)
        for field in m.ENABLED:
            for bad in (None, '0'):
                changed = dict(env)
                if bad is None: changed.pop(field)
                else: changed[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(m.Blocked):
                    m.live_environment(changed)
        for field in m.HELD:
            with self.subTest(field=field), self.assertRaises(m.Blocked):
                m.live_environment(dict(env, **{field:'1'}))

    def test_only_flags_can_change_not_credentials_or_thresholds(self):
        old = dict(ANGEL_API_KEY='private', KADIMA_CELESTIAL_ENABLED='1', KADIMA_SESSION_AUTOTRADE='1')
        new = dict(old, **dict.fromkeys(m.ENABLED, '1'))
        m.expected_environment(old, new)
        for key in ('ANGEL_API_KEY', 'KADIMA_MIN_SCORE', 'PYTHONPATH'):
            with self.subTest(key=key), self.assertRaisesRegex(m.Blocked, 'unexpected_environment'):
                m.expected_environment(old, dict(new, **{key:'changed'}))

    def test_live_flags_use_last_environment_file_not_inline_override(self):
        cm = m.load('momentum_recovery/start_paused_peripherals.py').load_start().load_repair().load_readiness().load_reviewed('stopped_context.py')
        self.assertEqual(dict.fromkeys(m.ENABLED, '1'), cm.parse_environment_file(m.LIVE_ENV_BYTES))
        self.assertIn(('EnvironmentFile='+str(m.LIVE_ENV)).encode(), m.DROPIN_BYTES)
        self.assertNotIn(b'UnsetEnvironment=\n', m.DROPIN_BYTES)
        for name in m.HELD:
            self.assertIn(name.encode(), m.DROPIN_BYTES)

    def test_all_frozen_helpers_match_without_importing_runtime(self):
        for relative, pin in m.PINS.items():
            self.assertEqual(pin, m.digest((ROOT/relative).read_bytes()), relative)
        self.assertEqual(m.STOCK_API_BLOB, m.digest((ROOT/'kadima/stock_fast_api.py').read_bytes()))
        p = m.load('momentum_recovery/start_paused_peripherals.py')
        self.assertEqual(p.START_BLOB, m.digest((ROOT/'momentum_recovery/start_repaired_paused.py').read_bytes()))


class Health(unittest.TestCase):
    def setUp(self):
        self.policies = dict(global_=dict(auto_trade_enabled=False, kill_latched=False, score_threshold=65, revision=22),
                            stock=dict(live_enabled=False, revision=14))
        self.policies['global'] = self.policies.pop('global_')
        cfg = dict(self.policies['global'], rollout_enabled=True, broker_orders_enabled=True)
        self.values = {
            (8080, '/health'):dict(engine_attached=True, ok=True),
            (8081, '/health'):dict(service='kadima-control-api', ok=True),
            (8081, '/api/kadima/config'):cfg,
            (8081, '/api/kadima/top20'):dict(ts=NOW.isoformat(), positions=[], config=cfg),
            (8081, '/api/kadima/stock-fast'):dict(ts=NOW.isoformat(), controls=deepcopy(self.policies['stock']),
                fast_master_permission=True, celestial_rollout=True, broker_order_permission=True,
                global_auto_requested=False, kill_latched=False),
        }

    def check(self, fast=True):
        return m.activation_health(lambda port, path:self.values[(port,path)], self.policies, NOW, fast=fast)

    def test_paused_api_proves_loaded_flags_but_not_market_readiness(self):
        result = self.check()
        self.assertTrue(result['api_health_verified'])
        self.assertTrue(result['market_readiness_pending'])
        self.assertFalse(result['global_auto_requested'])
        self.assertFalse(result['stock_live_requested'])

    def test_snapshot_freshness_uses_receipt_time_after_http_calls(self):
        received = NOW+timedelta(seconds=1)
        for route in ('/api/kadima/top20', '/api/kadima/stock-fast'):
            self.values[(8081, route)]['ts'] = received.isoformat()
        clock = Mock(return_value=received)
        m.activation_health(lambda port, path:self.values[(port,path)], self.policies, clock, fast=True)
        clock.assert_called_once()

    def test_before_restart_requires_fast_master_false(self):
        self.values[(8081,'/api/kadima/stock-fast')]['fast_master_permission'] = False
        self.check(fast=False)
        with self.assertRaises(m.Blocked): self.check()

    def test_global_auto_kill_threshold_revision_changes_block(self):
        value = self.values[(8081,'/api/kadima/config')]
        for key, bad in (('auto_trade_enabled', True), ('kill_latched', True), ('score_threshold', 60), ('revision',23)):
            old = value[key]; value[key] = bad
            with self.subTest(key=key), self.assertRaises(m.Blocked): self.check()
            value[key] = old

    def test_live_stock_or_changed_revision_blocks(self):
        value = self.values[(8081,'/api/kadima/stock-fast')]['controls']
        for key, bad in (('live_enabled', True), ('revision',15)):
            old=value[key];value[key]=bad
            with self.subTest(key=key), self.assertRaises(m.Blocked):self.check()
            value[key]=old

    def test_new_managed_position_blocks_engine_restart_deployment(self):
        self.values[(8081,'/api/kadima/top20')]['positions']=[dict(signal_id=96)]
        with self.assertRaisesRegex(m.Blocked,'managed_exposure'):self.check()

    def test_both_snapshots_must_be_fresh_and_timezone_aware(self):
        for route in ('/api/kadima/top20','/api/kadima/stock-fast'):
            for stamp in ((NOW-timedelta(seconds=31)).isoformat(), (NOW+timedelta(seconds=1)).isoformat(),
                          NOW.replace(tzinfo=None).isoformat(), 'bad', None):
                value=self.values[(8081,route)];old=value['ts'];value['ts']=stamp
                with self.subTest(route=route,stamp=stamp),self.assertRaisesRegex(m.Blocked,'snapshot_stale'):self.check()
                value['ts']=old

    def test_missing_real_broker_permission_or_rollout_blocks(self):
        value=self.values[(8081,'/api/kadima/stock-fast')]
        for key in ('celestial_rollout','broker_order_permission'):
            value[key]=False
            with self.subTest(key=key),self.assertRaises(m.Blocked):self.check()
            value[key]=True


class Operations:
    def __init__(self, fail=None):
        self.calls=[];self.fail=fail;self.last_broker={'flat':False,'pending_orders':0}
        self.policy=POLICY;self.records=[]
    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            if name==self.fail:raise m.Blocked('injected_'+name)
            if name=='wait_health':return {'market_readiness_pending':True}
            if name=='verify_schedule':return {'status':'outside_scheduled_window'}
            if name=='fail_closed':return {'timer_disabled':True,'permit_revoked':True}
            if name=='record':self.records.append(deepcopy(args[0]))
        return call


class Sequencing(unittest.TestCase):
    def test_plan_does_not_claim_restart_or_enable(self):
        ops=Operations();report=m.transaction(ops,apply=False)
        self.assertEqual([x[0] for x in ops.calls],['guard','broker_check','guard'])
        self.assertEqual(report['state'],'auto_trading_plan')
        self.assertFalse(report['live_entries_enabled_now'])

    def test_success_enables_timer_only_after_restart_and_verified_health(self):
        ops=Operations();report=m.transaction(ops,apply=True)
        names=[x[0] for x in ops.calls]
        ordered=['claim','backup','revoke','stop_engine','install_flags','start_engine','wait_health',
                 'clear_schedule_hold','release_scheduler_lock','enable_schedule','verify_schedule','record']
        self.assertEqual([n for n in names if n in ordered],ordered)
        self.assertEqual(report['state'],'auto_trading_deployed_waiting_for_session')
        self.assertTrue(report['scheduler_enabled'])
        self.assertFalse(report['live_entries_enabled_now'])
        self.assertFalse(report['broker_order_called'])
        self.assertNotIn('fail_closed',names)
        self.assertEqual(ops.calls[names.index('stop_engine')+2],('guard',dict(fast=False,stopped=True)))
        self.assertEqual(ops.calls[-2],('guard',dict(fast=True,scheduled=True)))

    def test_admission_failure_never_claims_or_changes_service(self):
        for failure in ('guard','broker_check'):
            ops=Operations(failure)
            with self.subTest(failure=failure),self.assertRaises(m.Blocked):m.transaction(ops,apply=True)
            self.assertNotIn('claim',[x[0] for x in ops.calls])
            self.assertNotIn('fail_closed',[x[0] for x in ops.calls])

    def test_every_mutating_stage_failure_revokes_permission_and_never_retries_engine(self):
        for failure in ('backup','revoke','stop_engine','install_flags','start_engine','wait_health',
                        'clear_schedule_hold','release_scheduler_lock','enable_schedule','verify_schedule'):
            ops=Operations(failure)
            with self.subTest(failure=failure),self.assertRaises(m.Blocked) as caught:m.transaction(ops,apply=True)
            names=[x[0] for x in ops.calls]
            self.assertEqual(names.count('fail_closed'),1)
            self.assertLessEqual(names.count('start_engine'),1)
            self.assertEqual(caught.exception.report['state'],'auto_trading_setup_incomplete')
            if names.index(failure)<names.index('fail_closed') and failure not in ('enable_schedule','verify_schedule'):
                self.assertNotIn('enable_schedule',names)

    def test_repeat_claim_does_not_touch_timer_or_original_receipts(self):
        ops=Operations('claim')
        with self.assertRaises(m.Blocked):m.transaction(ops,apply=True)
        self.assertNotIn('backup',[x[0] for x in ops.calls])
        self.assertNotIn('enable_schedule',[x[0] for x in ops.calls])

    def test_unknown_failure_after_start_disables_timer_and_preserves_running_engine(self):
        ops=m.Deployment.__new__(m.Deployment)
        ops.command=Mock(side_effect=[None,RuntimeError('unavailable')]);ops.revoke=Mock()
        ops.helper=SimpleNamespace(observe_unit=lambda unit:{'ActiveState':'active'})
        ops.flags_written=True
        result=ops.fail_closed()
        self.assertTrue(result['timer_disabled']);self.assertFalse(result['scheduler_stopped'])
        self.assertTrue(result['permit_revoked']);self.assertEqual(result['engine_state'],'active')
        self.assertEqual(ops.command.call_args_list[0].args,('disable','--now',m.TIMER))
        self.assertEqual(ops.command.call_args_list[1].args,('stop',m.JOB))

    def test_post_enable_guard_failure_cannot_report_success(self):
        ops=Operations();original=ops.guard
        def guard(**kwargs):
            original(**kwargs)
            if kwargs.get('scheduled'):raise m.Blocked('late_change')
        ops.guard=guard
        with self.assertRaises(m.Blocked) as caught:m.transaction(ops,apply=True)
        self.assertEqual(caught.exception.report['state'],'auto_trading_setup_incomplete')
        self.assertEqual(ops.records[-1]['recovery']['permit_revoked'],True)


if __name__=='__main__':unittest.main()
