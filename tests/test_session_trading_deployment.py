"""Offline deployment admission, restart sequencing and scheduler rollback."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import stat
import tempfile
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


class LockDirectoryTests(unittest.TestCase):
    def info(self, mode, uid=0, kind=stat.S_IFDIR):
        return SimpleNamespace(st_mode=kind | mode, st_uid=uid)

    def test_standard_sticky_lock_directory_and_nonwritable_variants(self):
        for mode in (0o1777, 0o755, 0o700, 0o750, 0o1755):
            with self.subTest(mode=oct(mode)):
                self.assertTrue(m.lock_directory_admitted(self.info(mode)))

    def test_nonsticky_writable_and_nonstandard_writable_modes_are_rejected(self):
        for mode in (0o777, 0o775, 0o757, 0o770, 0o1775, 0o3777):
            with self.subTest(mode=oct(mode)):
                self.assertFalse(m.lock_directory_admitted(self.info(mode)))

    def test_sticky_does_not_admit_wrong_owner_or_nondirectory(self):
        for uid, kind in ((1000, stat.S_IFDIR), (0, stat.S_IFREG), (0, stat.S_IFLNK), (0, stat.S_IFIFO)):
            with self.subTest(uid=uid, kind=kind):
                self.assertFalse(m.lock_directory_admitted(self.info(0o1777, uid, kind)))

    def test_exception_is_limited_to_exact_lock_directory(self):
        helper=SimpleNamespace(root_parents=Mock())
        with ExitStack() as stack, self.assertRaisesRegex(m.Blocked, 'lock_path'):
            m.maintenance_lock(stack, helper, Path('/tmp/unreviewed.lock'))
        helper.root_parents.assert_not_called()

    def test_nonlock_ancestor_rule_is_unchanged(self):
        helper=SimpleNamespace(root_parents=Mock(side_effect=m.Blocked('unsafe_parent_directory')))
        with ExitStack() as stack, patch.object(m.os, 'open') as opened:
            with self.assertRaisesRegex(m.Blocked, 'unsafe_parent_directory'):
                m.maintenance_lock(stack, helper, m.LOCK_DIRECTORY/'kadima-test.lock')
        helper.root_parents.assert_called_once_with(m.LOCK_DIRECTORY)
        opened.assert_not_called()


@unittest.skipUnless(os.geteuid() == 0, 'Root file ownership fixtures run in privileged CI')
class RealLockFiles(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)/'lock'
        self.directory.mkdir();self.directory.chmod(0o1777)
        self.original_mode=stat.S_IMODE(self.directory.stat().st_mode)
        self.path=self.directory/'kadima-test.lock'
        self.helper=SimpleNamespace(root_parents=Mock())
        self.override=patch.object(m,'LOCK_DIRECTORY',self.directory)
        self.override.start();self.addCleanup(self.override.stop)

    def test_existing_root_lock_inode_contents_and_mode_are_preserved(self):
        self.path.write_bytes(b'existing lock contents')
        self.path.chmod(0o644)
        before=self.path.stat()
        with ExitStack() as locks:
            stream=m.maintenance_lock(locks,self.helper,self.path)
            self.assertEqual(os.fstat(stream.fileno()).st_ino,before.st_ino)
            self.assertEqual(stream.read(),b'existing lock contents')
            with ExitStack() as competitor, self.assertRaises(BlockingIOError):
                m.maintenance_lock(competitor,self.helper,self.path)
        after=self.path.stat()
        self.assertEqual((before.st_ino,before.st_mode,before.st_size),(after.st_ino,after.st_mode,after.st_size))
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode),self.original_mode)
        with ExitStack() as subsequent:
            m.maintenance_lock(subsequent,self.helper,self.path)
        self.helper.root_parents.assert_called_with(self.directory)

    def test_new_lock_is_private_and_reusable(self):
        with ExitStack() as locks:
            m.maintenance_lock(locks,self.helper,self.path)
            info=self.path.stat()
            self.assertEqual(info.st_uid,0)
            self.assertEqual(stat.S_IMODE(info.st_mode),0o600)
            self.assertEqual(info.st_nlink,1)
        with ExitStack() as locks:
            m.maintenance_lock(locks,self.helper,self.path)

    def test_group_or_world_writable_file_is_rejected(self):
        for mode in (0o666,0o660,0o622):
            self.path.write_bytes(b'unchanged');self.path.chmod(mode)
            with self.subTest(mode=mode),ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'private_lock'):
                m.maintenance_lock(locks,self.helper,self.path)
            self.assertEqual(self.path.read_bytes(),b'unchanged')
            self.path.unlink()

    def test_symlink_and_hardlink_are_rejected_without_modifying_target(self):
        target=self.directory/'target';target.write_bytes(b'protected')
        self.path.symlink_to(target)
        with ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'lock_path'):
            m.maintenance_lock(locks,self.helper,self.path)
        self.path.unlink();os.link(target,self.path)
        with ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'private_lock'):
            m.maintenance_lock(locks,self.helper,self.path)
        self.assertEqual(target.read_bytes(),b'protected')

    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.path,0o600)
        with ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'private_lock'):
            m.maintenance_lock(locks,self.helper,self.path)

    def test_nonsticky_writable_parent_is_rejected_before_creating_file(self):
        self.directory.chmod(0o777)
        with ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'lock_directory_owner_or_mode'):
            m.maintenance_lock(locks,self.helper,self.path)
        self.assertFalse(self.path.exists())

    def test_directory_symlink_is_rejected(self):
        linked=Path(self.tmp.name)/'linked';linked.symlink_to(self.directory,target_is_directory=True)
        with patch.object(m,'LOCK_DIRECTORY',linked),ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'lock_path'):
            m.maintenance_lock(locks,self.helper,linked/'kadima-test.lock')
        self.assertFalse(self.path.exists())

    def test_replaced_inode_is_rejected_and_descriptor_released(self):
        original_flock=m.fcntl.flock
        replacement=self.directory/'replacement';replacement.write_bytes(b'new')
        def replacing(fd,flags):
            original_flock(fd,flags)
            os.replace(replacement,self.path)
        with patch.object(m.fcntl,'flock',side_effect=replacing),ExitStack() as locks,self.assertRaisesRegex(m.Blocked,'lock_inode_changed'):
            m.maintenance_lock(locks,self.helper,self.path)
        with ExitStack() as locks:
            m.maintenance_lock(locks,self.helper,self.path)


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


class ObserverHealth(unittest.TestCase):
    def setUp(self):
        self.started = NOW.replace(hour=11, minute=8)  # 16:38 IST, successful paused startup.
        self.startup = dict(dashboard_gateway_health_verified=True,
                            scanner_snapshot_at=self.started.isoformat(),
                            macro_snapshot_at=self.started.isoformat())
        self.values = {
            (8082, '/health'): dict(ok=True),
            (8081, '/universe_snapshot'): dict(ts=self.started.isoformat(), tracked=[], prices={},
                                              execution='central_executor_gated'),
            (8081, '/macro_snapshot'): dict(ts=NOW.isoformat(), news=[], economic_calendar=[]),
        }

    def get(self, port, path):
        return self.values[(port, path)]

    def check(self, current=NOW):
        return m.deployment_observer_health(self.get, self.startup, gate, POLICY, current)

    def test_same_day_idle_snapshot_passes_without_claiming_fresh_market_data(self):
        before = deepcopy(self.values)
        result = self.check()
        self.assertEqual(result['scanner_snapshot_age_seconds'], 5220)
        self.assertFalse(result['scanner_snapshot_recent'])
        self.assertFalse(result['scanner_progress_verified'])
        self.assertFalse(result['live_feed_verified'])
        self.assertTrue(result['market_data_verification_pending'])
        self.assertEqual(self.values, before)  # No timestamp rewriting.

    def test_recent_snapshot_still_does_not_prove_market_readiness(self):
        self.values[(8081, '/universe_snapshot')]['ts'] = NOW.isoformat()
        result = self.check()
        self.assertTrue(result['scanner_snapshot_recent'])
        self.assertFalse(result['live_feed_verified'])
        self.assertTrue(result['market_data_verification_pending'])

    def test_snapshot_regression_and_future_timestamp_block(self):
        for at in (self.started-timedelta(seconds=1), NOW+timedelta(seconds=1)):
            self.values[(8081, '/universe_snapshot')]['ts'] = at.isoformat()
            with self.subTest(at=at), self.assertRaisesRegex(m.Blocked, 'before_startup_or_future'):
                self.check()

    def test_retained_snapshot_from_previous_local_day_blocks(self):
        at = datetime.fromisoformat('2026-09-29T00:01:00+05:30')
        self.values[(8081, '/macro_snapshot')]['ts'] = at.isoformat()
        with self.assertRaisesRegex(m.Blocked, 'not_current_local_day'):
            self.check(at)

    def test_new_local_day_snapshot_from_daily_reinitialization_is_admitted(self):
        at = datetime.fromisoformat('2026-09-29T00:01:00+05:30')
        for route in ('/universe_snapshot', '/macro_snapshot'):
            self.values[(8081, route)]['ts'] = at.isoformat()
        self.assertTrue(self.check(at)['market_data_verification_pending'])

    def test_macro_still_requires_continuous_fresh_publication(self):
        for at in (NOW-timedelta(seconds=181), NOW+timedelta(seconds=1), self.started-timedelta(seconds=1)):
            self.values[(8081, '/macro_snapshot')]['ts'] = at.isoformat()
            with self.subTest(at=at), self.assertRaisesRegex(m.Blocked, 'macro_snapshot_not_fresh'):
                self.check()

    def test_missing_naive_or_invalid_timestamps_block_for_both_observers(self):
        for route in ('/universe_snapshot', '/macro_snapshot'):
            original = self.values[(8081, route)]['ts']
            for stamp in (None, 'bad', NOW.replace(tzinfo=None).isoformat(), 123, {}):
                self.values[(8081, route)]['ts'] = stamp
                with self.subTest(route=route, stamp=stamp), self.assertRaisesRegex(m.Blocked, 'timestamp_invalid'):
                    self.check()
            del self.values[(8081, route)]['ts']
            with self.assertRaisesRegex(m.Blocked, 'timestamp_invalid'):
                self.check()
            self.values[(8081, route)]['ts'] = original

    def test_no_after_hours_exception_during_daytime_even_with_fresh_snapshots(self):
        for clock in ('08:30', '09:15', '15:35', '15:59'):
            at = datetime.fromisoformat('2026-09-28T'+clock+':00+05:30')
            for route in ('/universe_snapshot', '/macro_snapshot'):
                self.values[(8081, route)]['ts'] = at.isoformat()
            with self.subTest(clock=clock), self.assertRaisesRegex(m.Blocked, 'after_hours'):
                self.check(at)

    def test_unhealthy_gateway_blocks(self):
        for value in (dict(ok=False), {}, [], None):
            self.values[(8082, '/health')] = value
            with self.subTest(value=value), self.assertRaisesRegex(m.Blocked, 'gateway_health'):
                self.check()

    def test_observer_schemas_remain_required(self):
        for route, key, bad in (('/universe_snapshot', 'tracked', {}),
                ('/universe_snapshot', 'prices', []), ('/universe_snapshot', 'execution', 'direct'),
                ('/macro_snapshot', 'news', {}), ('/macro_snapshot', 'economic_calendar', {})):
            value = self.values[(8081, route)]; old = value[key]; value[key] = bad
            with self.subTest(key=key), self.assertRaisesRegex(m.Blocked, 'schema_invalid'):
                self.check()
            value[key] = old

    def test_successful_startup_health_receipt_is_required(self):
        for value in (None, {}, dict(dashboard_gateway_health_verified=False)):
            self.startup = value
            with self.subTest(value=value), self.assertRaisesRegex(m.Blocked, 'startup_health_receipt'):
                self.check()

    def test_startup_timestamp_must_be_valid_and_cannot_be_newer_than_observation(self):
        for key in ('scanner_snapshot_at', 'macro_snapshot_at'):
            original = self.startup[key]
            for value in (None, 'bad', NOW.replace(tzinfo=None).isoformat(),
                          (NOW+timedelta(seconds=1)).isoformat()):
                self.startup[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(m.Blocked):
                    self.check()
            self.startup[key] = original

    def test_receipt_clock_is_sampled_after_all_http_reads(self):
        received = NOW+timedelta(seconds=1)
        self.values[(8081, '/macro_snapshot')]['ts'] = received.isoformat()
        calls = []
        def get(port, path):
            calls.append(path)
            return self.get(port, path)
        def clock():
            self.assertEqual(calls, ['/health', '/universe_snapshot', '/macro_snapshot'])
            return received
        result = m.deployment_observer_health(get, self.startup, gate, POLICY, clock)
        self.assertEqual(result['macro_snapshot_age_seconds'], 0)

    def test_post_restart_health_reports_the_retained_snapshot_and_does_not_refresh_it(self):
        ops = m.Deployment.__new__(m.Deployment)
        ops.get = self.get; ops.policies = {}; ops.observer_startup_health = self.startup
        ops.gate = gate; ops.policy = POLICY
        ops.p = SimpleNamespace(Blocked=RuntimeError)
        with patch.object(m, 'activation_health', return_value={'market_readiness_pending': True}), \
                patch.object(m, 'now', return_value=NOW):
            result = ops.wait_health()
        self.assertEqual(result['observers']['scanner_snapshot_at'], self.started.isoformat())
        self.assertFalse(result['observers']['scanner_snapshot_recent'])


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
