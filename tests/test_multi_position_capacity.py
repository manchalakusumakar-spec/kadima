import ast
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from kadima import portfolio_capacity as pc


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

patcher = load('multi_patch_test', BASE/'multi_position_update/patch.py')
installer = load('multi_install_test', BASE/'multi_position_update/install.py')


def pos(token='1', qty=100, entry=100., stop=95., status='open'):
    return NS(exchange='NFO', token=token, symbol='TEST'+token,
              status=status, remaining_qty=qty, entry_price=entry, structural_stop=stop)


def sizing(positions=None, pending=None, **change):
    inputs = dict(exchange='NFO', token='NEW', limit_price=100., stop=95., lot_size=50,
                  requested_lots=2, broker_funds=50000., capital_buffer_pct=5.,
                  daily_limit=10000., realized_pnl=0., cost_reserve=.01, exit_buffer=.025)
    inputs.update(change)
    return pc.affordable_lots(positions or {}, pending if pending is not None else [], **inputs)


class CapacityTests(unittest.TestCase):
    def test_second_open_position_allowed(self):
        result = sizing({'1':pos()})
        self.assertEqual((result.lots,result.reason),(2,'ok'))
        self.assertEqual(result.open_positions,1)
    def test_third_open_position_allowed(self):
        result = sizing({'1':pos(),'2':pos('2')})
        self.assertEqual(result.lots,2); self.assertEqual(result.open_positions,2)
    def test_no_numeric_position_count_limit(self):
        held = {str(i):pos(str(i),qty=1,entry=1.,stop=.9) for i in range(101)}
        self.assertEqual(sizing(held).lots,2)
    def test_pending_buy_blocks_new_entry(self):
        self.assertEqual(sizing({'1':pos()},[dict(side='BUY',state='pending')]).reason,'portfolio_order_pending_wait')
    def test_pending_sell_does_not_release_cash(self):
        self.assertEqual(sizing({'1':pos()},[dict(side='SELL',state='pending')]).lots,0)
    def test_ambiguous_response_blocks_new_entry(self):
        self.assertEqual(sizing({},[dict(state='unknown')]).lots,0)
    def test_partial_entry_blocks_other_entries(self):
        self.assertEqual(sizing({'1':pos(status='entry_pending')}).reason,'portfolio_order_pending_wait')
    def test_exit_pending_without_ledger_is_not_assumed_closed(self):
        self.assertEqual(sizing({'1':pos(status='exit_pending')}).lots,0)
    def test_unfilled_pending_zero_qty_is_not_free_slot(self):
        self.assertEqual(sizing({'1':pos(qty=0,status='entry_pending')}).lots,0)
    def test_confirmed_closed_position_releases_exposure(self):
        self.assertEqual(sizing({'1':pos(qty=0,status='closed')}).open_positions,0)
    def test_closed_nonzero_qty_requires_reconciliation(self):
        self.assertEqual(sizing({'1':pos(status='closed')}).lots,0)
    def test_duplicate_contract_is_rejected(self):
        self.assertEqual(sizing({'1':pos('NEW')}).reason,'contract_already_open')
    def test_same_token_different_exchange_not_conflated(self):
        position=pos('NEW'); position.exchange='BFO'
        self.assertEqual(sizing({'1':position}).lots,2)
    def test_duplicate_existing_contract_book_rejected(self):
        self.assertEqual(sizing({'1':pos(),'2':pos()}).reason,'portfolio_duplicate_contract_unresolved')
    def test_cash_sized_down_in_whole_lots(self):
        result=sizing({'1':pos()},broker_funds=18000.)
        self.assertEqual((result.lots,result.reason),(1,'portfolio_sized_down'))
    def test_aggregate_cash_not_individual_only(self):
        self.assertEqual(sizing({'1':pos(),'2':pos('2')},broker_funds=22000.).lots,0)
    def test_cash_cost_reserve_counted(self):
        self.assertEqual(sizing({'1':pos()}).cash_committed,10100.)
    def test_remaining_daily_loss_is_shared(self):
        result=sizing({'1':pos()},realized_pnl=-9000.)
        self.assertEqual(result.lots,0)
        self.assertAlmostEqual(result.stop_risk_committed,837.5)
    def test_negative_risk_room_rejected(self):
        self.assertEqual(sizing({'1':pos()},realized_pnl=-9900.).reason,'portfolio_daily_risk_headroom_exhausted')
    def test_risk_downsizes_lots(self):
        self.assertEqual(sizing({'1':pos()},daily_limit=1500.).lots,1)
    def test_realized_profit_does_not_raise_daily_risk_budget(self):
        self.assertEqual(sizing({'1':pos()},daily_limit=1500.).lots,
                         sizing({'1':pos()},daily_limit=1500.,realized_pnl=20000.).lots)
    def test_profitable_trail_not_counted_as_guaranteed_cash(self):
        p=pos(stop=120.)
        result=sizing({'1':p})
        self.assertEqual(result.cash_committed,10100.)
        self.assertAlmostEqual(result.stop_risk_committed,350.)
    def test_missing_structural_stop_blocks(self):
        self.assertEqual(sizing({'1':pos(stop=None)}).reason,'portfolio_existing_risk_unverifiable')
    def test_nan_existing_entry_blocks(self):
        self.assertEqual(sizing({'1':pos(entry=float('nan'))}).lots,0)
    def test_unknown_local_position_state_blocks(self):
        self.assertEqual(sizing({'1':pos(status='unknown')}).lots,0)
    def test_quantity_must_be_nonnegative_integer(self):
        for qty in (-1,True,1.5,None):
            with self.subTest(qty=qty): self.assertEqual(sizing({'1':pos(qty=qty)}).lots,0)
    def test_unreadable_broker_funds_block(self):
        for value in (None,True,-1,float('nan'),float('inf')):
            with self.subTest(value=value): self.assertEqual(sizing(broker_funds=value).lots,0)
    def test_invalid_candidate_inputs_block(self):
        for change in ({'stop':100.},{'lot_size':True},{'requested_lots':0},
                       {'daily_limit':0},{'cost_reserve':-1},{'exit_buffer':1},
                       {'realized_pnl':float('nan')},{'capital_buffer_pct':100}):
            with self.subTest(change=change): self.assertEqual(sizing(**change).lots,0)
    def test_no_permission_or_position_mutation(self):
        p=pos(); initial=vars(p).copy(); sizing({'1':p})
        self.assertEqual(vars(p),initial)
    def test_invalid_pending_schema_is_not_flat(self):
        self.assertEqual(pc.entry_block({},None),'portfolio_state_unknown')


FIXTURE = '''from .config import (DAILY_LOSS_LIMIT_INR, IST, MAX_CONCURRENT_POSITIONS,
                     MAX_LOTS_PER_SIGNAL, ORDER_PLACEMENT_ENABLED)
def _poll_ltp(exchange,tokens):
'''+patcher.POLL+'''    return dict.fromkeys(tokens,1)
class AutoExecutor:
    def evaluate_and_enter(self, index_name, direction, chain):
        from . import market_session_gate
        market_session_gate.entry_block('NFO')
'''+patcher.SINGLE+patcher.BEFORE_CLOCK+'''        market_session_gate.entry_block('NFO')
        return True
    def check_exits(self):
        return 'unchanged'
'''


class PatchTests(unittest.TestCase):
    def test_count_removed_calendar_checks_retained(self):
        result=patcher.patch_executor(FIXTURE.encode()).decode()
        self.assertNotIn('MAX_CONCURRENT_POSITIONS',result)
        self.assertEqual(result.count('market_session_gate.entry_block'),2)
        self.assertIn('portfolio_capacity.affordable_lots',result)
    def test_unexpected_source_anchor_rejected(self):
        with self.assertRaises(ValueError): patcher.patch_executor(FIXTURE.replace('min(1,','min(2,').encode())
    def test_missing_session_gate_rejected(self):
        with self.assertRaisesRegex(ValueError,'session_entry_guards_required'):
            patcher.patch_executor(FIXTURE.replace('market_session_gate.entry_block','other').encode())
    def test_patch_does_not_stack_twice(self):
        with self.assertRaises(ValueError): patcher.patch_executor(patcher.patch_executor(FIXTURE.encode()))
    def test_many_position_quotes_are_batched(self):
        tree=ast.parse(patcher.patch_executor(FIXTURE.encode()))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef))
        # Install a recursive wrapper to observe each resulting small batch.
        calls=[]
        class C:
            def enabled(self): calls.append(True); return False
        ns={'celestial':C()}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'batch_test','exec'),ns)
        result=ns['_poll_ltp']('NFO',[str(i) for i in range(121)])
        self.assertEqual(len(calls),3); self.assertEqual(len(result),121)
    def test_real_deployed_patch_preserves_exit_methods(self):
        source=BASE/'kadima/auto_executor.py'
        if not source.is_file(): self.skipTest('Full source fixture available in repository CI')
        session=load('test_multi_session',BASE/'session_auto/install.py')
        original=session.patch_executor(source.read_bytes())
        result=patcher.patch_executor(original)
        before,after=ast.parse(original),ast.parse(result)
        def methods(tree):
            return {n.name:ast.dump(n,include_attributes=False) for c in tree.body if isinstance(c,ast.ClassDef)
                    and c.name=='AutoExecutor' for n in c.body if isinstance(n,ast.FunctionDef)}
        a,b=methods(before),methods(after)
        self.assertEqual([k for k in a if a[k]!=b[k]],['evaluate_and_enter'])


class MaintenanceTests(unittest.TestCase):
    def test_apply_not_allowed_while_scheduler_can_arm(self):
        gate=NS(calendar_reason=lambda *a:None,IST=timezone.utc)
        with self.assertRaisesRegex(ValueError,'outside_scheduled_window'):
            installer.require_outside_session(gate,{})
    def test_unknown_calendar_does_not_authorize_maintenance(self):
        gate=NS(calendar_reason=lambda *a:'calendar_update_required',IST=timezone.utc)
        with self.assertRaises(ValueError): installer.require_outside_session(gate,{})
    def test_weekend_and_offhours_can_be_planned(self):
        for reason in ('non_trading_day','outside_scheduled_window'):
            installer.require_outside_session(NS(calendar_reason=lambda *a:reason,IST=timezone.utc),{})
    def test_installer_changes_only_executor_and_new_helper(self):
        tree=ast.parse((BASE/'multi_position_update/install.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='sources')
        assignment=next(n for n in function.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='replacements' for t in n.targets))
        self.assertEqual({k.value for k in assignment.value.keys},{'kadima/auto_executor.py','kadima/portfolio_capacity.py'})


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.backup=self.root/'backup'; self.backup.mkdir()
        self.original=self.root/'executor.py'; self.original.write_bytes(b'session_guarded_original')
        self.added=self.root/'portfolio.py'
        self.changes=[]
        for path,old,new in ((self.original,b'session_guarded_original',b'session_guarded_multi'),
                              (self.added,None,b'portfolio_helper')):
            self.changes.append(NS(path=path,original=old,replacement=new,uid=0,gid=0,mode=0o644,unchanged=lambda:None))
        self.states={u:'active' for u in (installer.TIMER,installer.JOB,'engine','scanner')}
        self.calls=[]
        def command(args,**kw):
            self.calls.append(args)
            if args[1] in ('stop','start'):
                for u in args[2:]: self.states[u]='inactive' if args[1]=='stop' else 'active'
        self.helper=NS(command=command,unit_state=lambda u:dict(ActiveState=self.states[u],MainPID='0' if self.states[u]=='inactive' else '1'),
                       digest=lambda raw:raw.hex(),check_fingerprints=Mock(),write_atomic=lambda item,data:item.path.write_bytes(data))
        self.flat=Mock();self.check=Mock();self.check.fingerprints={};self.imports=Mock();self.post=Mock()
    def run_update(self):
        installer.perform(self.changes,['scanner','engine'],self.backup,helper=self.helper,
                          flat=self.flat,check=self.check,imports=self.imports,postcheck=self.post)
    def test_success_restores_timer_and_leaves_guarded_multi_source(self):
        self.run_update()
        self.assertEqual(self.original.read_bytes(),b'session_guarded_multi')
        self.assertTrue(self.added.exists());self.assertEqual(self.states[installer.TIMER],'active')
        self.assertEqual(self.calls[0],['systemctl','stop',installer.TIMER,installer.JOB])
    def test_second_broker_check_failure_keeps_old_source_and_restores_services(self):
        self.flat.side_effect=ValueError('broker_unavailable')
        with self.assertRaises(ValueError): self.run_update()
        self.assertEqual(self.original.read_bytes(),b'session_guarded_original');self.assertFalse(self.added.exists())
        self.assertEqual(self.states[installer.TIMER],'active')
    def test_failed_import_rolls_back_both_files_before_restart(self):
        self.imports.side_effect=ValueError('import_failed')
        with self.assertRaises(ValueError): self.run_update()
        self.assertEqual(self.original.read_bytes(),b'session_guarded_original');self.assertFalse(self.added.exists())
    def test_post_restart_failure_does_not_roll_back_under_possible_exposure(self):
        self.post.side_effect=ValueError('health_failed')
        with self.assertRaises(ValueError): self.run_update()
        self.assertEqual(self.original.read_bytes(),b'session_guarded_multi');self.assertTrue(self.added.exists())
        self.assertEqual(self.states[installer.TIMER],'inactive')
    def test_settings_change_after_timer_stop_aborts_before_engine_stop(self):
        self.check.side_effect=ValueError('operator_changed_settings')
        with self.assertRaises(ValueError): self.run_update()
        self.assertNotIn(['systemctl','stop','scanner','engine'],self.calls)
        self.assertEqual(self.original.read_bytes(),b'session_guarded_original')


class RealExecutorTests(unittest.TestCase):
    def setUp(self):
        file=BASE/'kadima/auto_executor.py'
        if not file.exists(): self.skipTest('Full executor-source execution tests run in repository CI')
        session=load('test_multi_real_session',BASE/'session_auto/install.py')
        tree=ast.parse(patcher.patch_executor(session.patch_executor(file.read_bytes())))
        serializer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='serialized')
        klass=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='AutoExecutor')
        method=next(n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name=='evaluate_and_enter')
        self.pending=[]; self.submitted=[]
        self.quote=NS(token='NEW',symbol='NEWCE',opt_type='CE',strike=100,ltp=100.,bid=99.9,ask=100.1,
                      delta=.5,volume=10000,oi=10000,spread_pct=.2)
        self.fast=dict(eligible=True,strategy='fast_momentum',underlying='NEW',direction='CE',ts='now',
                       score=75,token='NEW',symbol='NEWCE',exchange='NFO',spot_stop=99,days=5,entry=100.1,episode='E')
        def submit(position,side,quantity,price):
            self.submitted.append((position,side,quantity,price))
            self.pending.append(dict(signal_id=position.signal_id,side=side,state='pending'))
            return dict(ok=True,reason='awaiting_broker_fill',pending=True)
        self.session_gate=NS(entry_block=Mock(return_value=None))
        import kadima
        # Deterministic synthetic host state; memory guard is tested separately.
        self.memory_guard=NS(entry_block_reason=Mock(return_value=None))
        p=patch.dict(sys.modules,{'kadima.memory_guard':self.memory_guard});p.start();self.addCleanup(p.stop)
        p=patch.object(kadima,'market_session_gate',self.session_gate,create=True);p.start();self.addCleanup(p.stop)
        p=patch.dict(sys.modules,{'kadima.market_session_gate':self.session_gate});p.start();self.addCleanup(p.stop)
        self.ns=dict(__package__='kadima',wraps=wraps,math=math,MAX_CONCURRENT_POSITIONS=1,
            ORDER_PLACEMENT_ENABLED=True,DAILY_LOSS_LIMIT_INR=10000,MAX_LOTS_PER_SIGNAL=2,
            MIN_PROJECTED_RETURN_PCT=10,LIMIT_ORDER_BUFFER_PCT=2.5,SimpleNamespace=NS,
            celestial=NS(fresh=lambda *a:True,enabled=lambda:False),
            fast_momentum=NS(live_blocks=lambda *a:[]),killswitch=NS(engaged=lambda:False),
            index_scalper=NS(KIND='index_scalp', LOTS=2, live_blocks=lambda *a:[]),
            in_market_hours=lambda:True,past_signal_cutoff=lambda:False,
            exit_manager=NS(realized_pnl_today=lambda:0),time=time,
            execution_ledger=NS(seen=lambda sid:False,pending=lambda:list(self.pending),submit=submit),
            config=NS(STRIKES_EACH_SIDE=1),strike_selector=NS(rank=lambda *a:[NS(quote=self.quote,rejected=None)]),
            risk_budget=NS(StrikeCandidate=lambda **kw:NS(**kw),select_within_budget=lambda *a,**k:NS(candidate=NS(token='NEW',stop_price=95.),lots=1)),
            capital_guard=NS(get_capital_for_signal=lambda *a:30000.,enforce=lambda *a:(1,'ok'),
                             broker_available_funds=lambda:50000.,CAPITAL_SAFETY_BUFFER_PCT=5.),
            trade_policy=NS(premium_stop=lambda *a:95.,lots_within_allocation=lambda *a:1,COST_RESERVE=.01,EXIT_BUFFER=.025),
            projected_return_pct=lambda *a:20,entry_quality=NS(EntryContext=lambda **kw:NS(**kw),evaluate_entry=lambda ctx:NS(allowed=True)),
            get_lot_size=lambda *a:50,_poll_ltp=lambda *a:{'NEW':100.},Position=lambda **kw:NS(**kw),
            now_ist=lambda:datetime.now(timezone.utc),audit=NS(log=Mock()),alerts=NS(enqueue=Mock()))
        exec(compile(ast.Module(body=[serializer,method],type_ignores=[]),'actual_executor_test','exec'),self.ns)
        self.ex=NS(_lock=threading.RLock(),_execution_lock=threading.RLock(),_reconcile_blocked=False,
            state=NS(positions={'1':pos()},premium_tracker=NS(record_chain=Mock(),snapshot=lambda *a:(100,100,100))),
            _reject=lambda reason,*a:dict(ok=False,reason=reason),entry_diagnostics=[])
        self.chain=NS(index=NS(opt_exchange='NFO'),_spot_received_at=time.monotonic(),_last_poll=time.monotonic(),
                      scrips=NS(strikes_around=lambda *a:[dict(token='NEW')]),atm=100)
    def enter(self,sid=2):
        return self.ns['evaluate_and_enter'](self.ex,'NEW','CE',self.chain,100,NS(vwap=99),5,
            'fast_momentum',sid,'EXPERIMENTAL',75,fast_context=lambda:self.fast.copy())
    def test_second_position_reaches_real_method_submit(self):
        self.assertTrue(self.enter()['ok']);self.assertEqual(len(self.ex.state.positions),2)
        self.assertEqual(len(self.submitted),1)
    def test_legacy_config_one_does_not_block_second(self):
        self.ns['MAX_CONCURRENT_POSITIONS']=1
        self.assertTrue(self.enter()['ok'])
    def test_open_scalp_keeps_exclusive_slot_under_multi_position_overlay(self):
        self.ex.state.positions['1'].kind = 'index_scalp'
        self.assertEqual(self.enter()['reason'], 'index_scalp_shared_slot_occupied')
        self.assertEqual(self.submitted, [])
    def test_scalp_cannot_enter_alongside_existing_position_under_overlay(self):
        self.fast['strategy'] = 'index_scalp'
        result = self.ns['evaluate_and_enter'](self.ex, 'NEW', 'CE', self.chain, 100, NS(vwap=99), 5,
            'index_scalp', 2, 'EXPERIMENTAL', 75, fast_context=lambda:self.fast.copy())
        self.assertEqual(result['reason'], 'index_scalp_shared_slot_occupied')
        self.assertEqual(self.submitted, [])
    def test_pending_order_blocks_without_second_broker_submission(self):
        self.pending.append(dict(state='unknown'))
        self.assertEqual(self.enter()['reason'],'portfolio_order_pending_wait');self.assertEqual(self.submitted,[])
    def test_duplicate_contract_blocks_in_real_method(self):
        self.ex.state.positions['1']=pos('NEW')
        self.assertEqual(self.enter()['reason'],'contract_already_open')
    def test_cash_guard_blocks_in_real_method(self):
        self.ns['capital_guard'].broker_available_funds=lambda:11000.
        self.assertFalse(self.enter()['ok']);self.assertEqual(self.submitted,[])
    def test_daily_risk_guard_blocks_in_real_method(self):
        self.ns['exit_manager'].realized_pnl_today=lambda:-9500.
        self.assertFalse(self.enter()['ok']);self.assertEqual(self.submitted,[])
    def test_session_gate_still_blocks(self):
        self.session_gate.entry_block.return_value='non_trading_day'
        self.assertEqual(self.enter()['reason'],'non_trading_day');self.assertEqual(self.submitted,[])
    def test_two_threads_cannot_spend_same_pending_cash(self):
        results=[]
        ts=[threading.Thread(target=lambda n=n:results.append(self.enter(n)),daemon=True) for n in (2,3)]
        for t in ts:t.start()
        for t in ts:t.join(3)
        self.assertTrue(all(not t.is_alive() for t in ts))
        self.assertEqual(sum(r['ok'] for r in results),1);self.assertEqual(len(self.submitted),1)
    def test_filled_second_allows_third_distinct_contract(self):
        self.assertTrue(self.enter()['ok'])
        second=self.ex.state.positions['2'];second.status='open';second.entry_price=100.;second.remaining_qty=50
        second.token='FILLED';self.pending.clear()
        self.assertTrue(self.enter(3)['ok']);self.assertEqual(len(self.ex.state.positions),3)

if __name__=='__main__': unittest.main()
