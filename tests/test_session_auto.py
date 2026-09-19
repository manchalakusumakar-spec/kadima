import ast
import copy
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module
runner=load('session_auto_runner_test',BASE/'session_auto/runner.py')
gate=runner.gate
install=load('session_auto_install_test',BASE/'session_auto/install.py')
POLICY=json.loads((BASE/'session_auto/policy.json').read_text())
MON=datetime(2026,9,21,9,20,tzinfo=gate.IST)

class Clock(datetime):
    current=MON
    @classmethod
    def now(cls,tz=None):return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

def snapshots():
    health=dict(ok=True,ts=MON.isoformat())
    return (dict(ts=MON.isoformat(),config=dict(revision=0,auto_trade_enabled=False,kill_latched=False),
        system=dict(news=health.copy(),reconciliation=health.copy())),
        dict(ts=MON.isoformat(),controls=dict(revision=0,live_enabled=False,holding_minutes=15),
        worker_alive=True,worker=health.copy(),kill_latched=False,celestial_rollout=True,
        fast_master_permission=True,broker_order_permission=True))

class Client:
    def __init__(self):self.gd,self.sd=snapshots();self.calls=[];self.fail=None
    def call(self,action,payload=None):
        self.calls.append((action,payload))
        if self.fail==action:raise runner.Blocked(action+'_temporarily_unavailable')
        if action=='global':return copy.deepcopy(self.gd)
        if action=='stock':return copy.deepcopy(self.sd)
        if action=='resume':
            cfg=self.gd['config'];cfg['revision']+=1;cfg['auto_trade_enabled']=True
            return cfg.copy()
        if action=='pause':
            cfg=self.gd['config'];cfg['revision']+=1;cfg['auto_trade_enabled']=False
            return cfg.copy()
        if action=='stock_config':
            cfg=self.sd['controls']
            if payload['expected_revision']!=cfg['revision']:raise runner.Blocked('stock_config_http_409')
            cfg.update(payload['patch']);cfg['revision']+=1;return cfg.copy()
        raise AssertionError(action)

class CalendarTests(unittest.TestCase):
    def test_normal_working_day(self):self.assertIsNone(gate.calendar_reason(POLICY,MON))
    def test_saturday_and_sunday(self):
        for day in (19,20):self.assertEqual(gate.calendar_reason(POLICY,MON.replace(day=day)),'non_trading_day')
    def test_all_remaining_holidays_block(self):
        for day in ('2026-10-02','2026-10-20','2026-11-08','2026-11-10','2026-11-24','2026-12-25'):
            dt=datetime.fromisoformat(day+'T10:00:00+05:30')
            self.assertEqual(gate.calendar_reason(POLICY,dt),'non_trading_day')
    def test_future_year_fails_closed(self):self.assertEqual(gate.calendar_reason(POLICY,MON.replace(year=2027)),'calendar_update_required')
    def test_before_calendar_coverage_fails_closed(self):self.assertEqual(gate.calendar_reason(POLICY,MON.replace(day=18)),'calendar_update_required')
    def test_preparation_can_begin_nine_but_entries_cannot(self):
        now=MON.replace(hour=9,minute=0)
        self.assertIsNone(gate.calendar_reason(POLICY,now))
        self.assertEqual(gate.calendar_reason(POLICY,now,entries=True),'outside_scheduled_window')
    def test_cutoff_strict(self):self.assertEqual(gate.calendar_reason(POLICY,MON.replace(hour=15,minute=35)),'outside_scheduled_window')
    def test_after_cutoff(self):self.assertEqual(gate.calendar_reason(POLICY,MON.replace(hour=16)),'outside_scheduled_window')
    def test_utc_converted_to_ist(self):
        utc=datetime.fromisoformat('2026-09-21T03:50:00+00:00')
        self.assertIsNone(gate.calendar_reason(POLICY,utc,entries=True))
    def test_naive_clock_blocks(self):self.assertEqual(gate.calendar_reason(POLICY,MON.replace(tzinfo=None)),'schedule_clock_timezone_missing')
    def test_unknown_calendar_schema(self):
        with self.assertRaises(ValueError):gate.validate_policy(dict(POLICY,unknown=1))
    def test_holiday_duplicates(self):
        with self.assertRaises(ValueError):gate.validate_policy(dict(POLICY,holidays=POLICY['holidays']*2))
    def test_unsafe_extended_cutoff(self):
        with self.assertRaises(ValueError):gate.validate_policy(dict(POLICY,stop_entries_at='17:00'))

class PermitTests(unittest.TestCase):
    def permit(self):return dict(allowed=True,day=MON.date().isoformat(),boot_id='boot',issued_mono=1000,expires_mono=1150,issued_at=MON.isoformat())
    def test_valid(self):self.assertIsNone(gate.permit_reason(self.permit(),MON,1001,'boot'))
    def test_expired(self):self.assertEqual(gate.permit_reason(self.permit(),MON+timedelta(seconds=150),1150,'boot'),'session_permit_expired')
    def test_wrong_boot(self):self.assertEqual(gate.permit_reason(self.permit(),MON,1001,'other'),'session_permit_wrong_boot')
    def test_yesterdays_lease(self):self.assertEqual(gate.permit_reason(self.permit(),MON+timedelta(days=1),1001,'boot'),'session_permit_not_granted')
    def test_no_permission(self):self.assertEqual(gate.permit_reason(dict(allowed=False),MON,1001,'boot'),'session_permit_not_granted')
    def test_no_nan(self):self.assertEqual(gate.permit_reason(dict(self.permit(),expires_mono=float('nan')),MON,1001,'boot'),'session_permit_invalid')
    def test_no_overlong_lease(self):self.assertEqual(gate.permit_reason(dict(self.permit(),expires_mono=2000),MON,1001,'boot'),'session_permit_expired')
    def test_monotonic_clock_rollback(self):self.assertEqual(gate.permit_reason(self.permit(),MON,999,'boot'),'session_permit_expired')
    def test_wall_clock_rollback(self):self.assertEqual(gate.permit_reason(self.permit(),MON-timedelta(seconds=1),1001,'boot'),'session_permit_clock_invalid')
    def test_non_equity_scopes_unchanged(self):
        with patch.dict(os.environ,{'KADIMA_SESSION_AUTOTRADE':'1'}):self.assertIsNone(gate.entry_block('MCX',MON))
    def test_disabled_rollout_unchanged(self):
        with patch.dict(os.environ,{'KADIMA_SESSION_AUTOTRADE':'0'}):self.assertIsNone(gate.entry_block('NFO',MON))
    def test_enabled_missing_calendar_blocks(self):
        with patch.dict(os.environ,{'KADIMA_SESSION_AUTOTRADE':'1'}),patch.object(gate,'read_json',side_effect=FileNotFoundError):
            self.assertEqual(gate.entry_block('NFO',MON),'session_schedule_unavailable')
    def test_calendar_blocks_even_with_prior_permission(self):
        with patch.dict(os.environ,{'KADIMA_SESSION_AUTOTRADE':'1'}),patch.object(gate,'read_json',return_value=POLICY):
            self.assertEqual(gate.entry_block('BFO',MON.replace(day=19)),'non_trading_day')

class RunnerTests(unittest.TestCase):
    def setUp(self):
        Clock.current=MON
        self.client=Client();self.state={};self.saved=[]
        self.clock=patch.object(runner,'datetime',Clock);self.clock.start();self.addCleanup(self.clock.stop)
        self.atomic_p=patch.object(runner,'atomic');self.atomic=self.atomic_p.start();self.addCleanup(self.atomic_p.stop)
        self.revoke_p=patch.object(runner,'revoke');self.revoke=self.revoke_p.start();self.addCleanup(self.revoke_p.stop)
        self.boot_p=patch.object(gate,'BOOT',Mock(read_text=lambda:'boot'));self.boot_p.start();self.addCleanup(self.boot_p.stop)
    def run_once(self,now=MON):return runner.iteration(self.client,POLICY,self.state,now,lambda s:self.saved.append(copy.deepcopy(s)))
    def test_enables_both_no_stock_click(self):
        self.assertEqual(self.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')
        self.assertEqual(self.state['phase'],'ARMED')
        self.assertTrue(self.client.gd['config']['auto_trade_enabled'])
        self.assertTrue(self.client.sd['controls']['live_enabled'])
        self.assertEqual(self.client.sd['controls']['holding_minutes'],15)
    def test_no_direct_order_route_exists(self):
        self.run_once();self.assertTrue(all(a in runner.URLS for a,_ in self.client.calls))
        self.assertFalse(any('order' in path for path in runner.URLS.values()))
    def test_worker_not_ready_retries_without_write(self):
        self.client.sd['worker_alive']=False
        self.assertEqual(self.run_once(),'worker_not_started_yet')
        self.assertEqual(self.state['phase'],'WAITING')
        self.assertFalse(any(a=='resume' for a,_ in self.client.calls))
        self.client.sd['worker_alive']=True
        self.assertEqual(self.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')
    def test_news_missing_waits(self):
        self.client.gd['system'].pop('news')
        self.assertEqual(self.run_once(),'news_not_ready');self.atomic.assert_not_called()
    def test_news_stale_waits(self):
        self.client.gd['system']['news']['ts']=(MON-timedelta(seconds=301)).isoformat()
        self.assertEqual(self.run_once(),'news_not_ready')
    def test_reconciliation_missing_waits(self):
        self.client.gd['system'].pop('reconciliation')
        self.assertEqual(self.run_once(),'reconciliation_not_ready')
    def test_kill_never_reset(self):
        self.client.gd['config']['kill_latched']=True
        self.assertEqual(self.run_once(),'kill_latched_or_unknown')
        self.assertFalse(any(a in ('resume','stock_config') for a,_ in self.client.calls))
    def test_weekend_no_arming(self):
        self.assertEqual(self.run_once(MON.replace(day=19)),'non_trading_day')
        self.assertFalse(any(a in ('resume','stock_config') for a,_ in self.client.calls))
    def test_outside_window_disables_both_without_exits(self):
        self.run_once();self.client.calls.clear()
        self.assertEqual(self.run_once(MON.replace(hour=16)),'outside_scheduled_window')
        self.assertFalse(self.client.gd['config']['auto_trade_enabled'])
        self.assertFalse(self.client.sd['controls']['live_enabled'])
        self.assertFalse(any('order' in a for a,_ in self.client.calls))
    def test_manual_global_pause_is_not_undone(self):
        self.run_once();self.client.gd['config'].update(auto_trade_enabled=False,revision=2)
        self.assertEqual(self.run_once(),'operator_pause_today')
        self.assertEqual(self.run_once(),'operator_pause_today')
        self.assertFalse(self.client.gd['config']['auto_trade_enabled'])
    def test_manual_stock_pause_is_not_undone(self):
        self.run_once();self.client.sd['controls'].update(live_enabled=False,revision=2)
        self.assertEqual(self.run_once(),'operator_pause_today')
        self.assertFalse(self.client.sd['controls']['live_enabled'])
    def test_pause_while_waiting_blocks_autoresume(self):
        self.client.sd['worker_alive']=False;self.run_once()
        self.client.gd['config']['revision']+=1;self.client.sd['worker_alive']=True
        self.assertEqual(self.run_once(),'operator_control_changed_while_waiting')
    def test_later_health_recovery_renews_not_rearms(self):
        self.run_once();self.client.calls.clear()
        self.client.gd['system']['news']['ok']=False
        self.assertEqual(self.run_once(),'news_not_ready')
        self.client.gd['system']['news']['ok']=True
        self.assertEqual(self.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')
        self.assertFalse(any(a=='resume' for a,_ in self.client.calls))
    def test_next_day_can_auto_arm_again(self):
        self.run_once();self.client.gd['config']['auto_trade_enabled']=False
        self.run_once();Clock.current=MON+timedelta(days=1)
        self.client.gd['ts']=self.client.sd['ts']=Clock.current.isoformat()
        self.client.sd['worker']['ts']=Clock.current.isoformat()
        for h in self.client.gd['system'].values():h['ts']=Clock.current.isoformat()
        self.assertEqual(self.run_once(Clock.current),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')
    def test_pending_revision_persisted_before_resume(self):
        old=self.client.call
        def call(action,payload=None):
            if action=='resume':self.assertEqual(self.saved[-1]['phase'],'ARMING')
            return old(action,payload)
        self.client.call=call;self.run_once()
    def test_lost_resume_response_reconciles_without_duplicate_resume(self):
        old=self.client.call;once=[True]
        def call(action,payload=None):
            result=old(action,payload)
            if action=='resume' and once[0]:once[0]=False;raise runner.Blocked('resume_temporarily_unavailable')
            return result
        self.client.call=call
        with self.assertRaises(runner.Blocked):self.run_once()
        self.assertEqual(self.state['phase'],'ARMING')
        self.assertEqual(self.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')
        self.assertEqual(sum(a=='resume' for a,_ in self.client.calls),1)
    def test_lost_stock_response_reconciles(self):
        old=self.client.call;once=[True]
        def call(action,payload=None):
            result=old(action,payload)
            if action=='stock_config' and once[0]:once[0]=False;raise runner.Blocked('stock_config_temporarily_unavailable')
            return result
        self.client.call=call
        with self.assertRaises(runner.Blocked):self.run_once()
        self.assertEqual(self.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')
        self.assertEqual(sum(a=='stock_config' for a,_ in self.client.calls),1)
    def test_mismatched_resume_revision_never_grants(self):
        old=self.client.call
        def call(action,payload=None):
            if action=='resume':self.client.gd['config']['revision']+=1
            return old(action,payload)
        self.client.call=call
        self.assertEqual(self.run_once(),'resume_revision_or_confirmation_changed');self.atomic.assert_not_called()
    def test_missing_control_revision_rejected(self):
        self.client.gd['config'].pop('revision')
        with self.assertRaises(runner.Blocked):self.run_once()
    def test_control_flags_are_not_truthy_strings(self):
        self.client.sd['controls']['live_enabled']='false'
        with self.assertRaises(runner.Blocked):self.run_once()
    def test_network_delay_across_cutoff_cannot_grant_lease(self):
        old=self.client.call
        def call(action,payload=None):
            result=old(action,payload)
            if action=='stock_config':Clock.current=MON.replace(hour=15,minute=36)
            return result
        self.client.call=call
        self.assertNotEqual(self.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES');self.atomic.assert_not_called()

class PatchTests(unittest.TestCase):
    fixture=b'''class Executor:
    def evaluate_and_enter(self,index_name,direction,chain,source_signal_id,kind):
        if not getattr(chain, '_entry_guard', lambda: True)():
            return False
        selected = None
        if not getattr(chain, '_entry_guard', lambda: True)():
            return False
        pos = Position(
            signal_id=source_signal_id, kind=kind, index=index_name, direction=direction,
        )
        return pos
    def check_exits(self):
        return 'untouched'
'''
    def test_patch_only_two_entry_checks_no_exits(self):
        with patch.object(install,'BASE_EXECUTOR',install.digest(self.fixture)):
            result=install.patch_executor(self.fixture).decode()
        self.assertEqual(result.count('market_session_gate.entry_block'),2)
        tree=ast.parse(result);exits=tree.body[0].body[-1]
        self.assertEqual(ast.dump(exits),ast.dump(ast.parse(self.fixture).body[0].body[-1]))
    def test_unknown_executor_never_patched(self):
        with self.assertRaisesRegex(ValueError,'reviewed_executor_required'):install.patch_executor(self.fixture)
    def test_real_checkout_source_patch_compiles(self):
        file=BASE/'kadima/auto_executor.py'
        if not file.exists():self.skipTest('Complete repository check runs in GitHub CI')
        raw=file.read_bytes();patched=install.patch_executor(raw)
        before=ast.parse(raw);after=ast.parse(patched)
        def methods(tree):
            return {n.name:ast.dump(n,include_attributes=False) for c in tree.body if isinstance(c,ast.ClassDef) and c.name=='AutoExecutor' for n in c.body if isinstance(n,ast.FunctionDef)}
        a,b=methods(before),methods(after)
        self.assertEqual(set(a),set(b))
        self.assertEqual([k for k in a if a[k]!=b[k]],['evaluate_and_enter'])
    def test_timer_retries_and_boot(self):
        self.assertIn('OnBootSec=30s',install.timer_text());self.assertIn('OnUnitInactiveSec=60s',install.timer_text())
    def test_lease_directory_survives_oneshot_end(self):self.assertIn('RuntimeDirectoryPreserve=yes',install.service_text())
    def test_no_force_start_or_risk_changes_in_env(self):
        self.assertEqual(set(install.ENV_CONTENT.splitlines()),{'KADIMA_CELESTIAL_ENABLED=1','KADIMA_FAST_MOMENTUM_LIVE=1','KADIMA_SESSION_AUTOTRADE=1'})

if __name__=='__main__':unittest.main()
