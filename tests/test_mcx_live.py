from datetime import datetime, timedelta
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

from kadima import commodity_watch as watch
from kadima import instrument_master as im
from kadima import mcx_live
from kadima.celestial_scanner import PremiumWindow
from kadima.celestial_universe import active_contracts
from kadima.config import IST

NOW=datetime(2026,9,21,18,0,tzinfo=IST)


def rows():
    out=[]
    token=1000
    for name, base in [('CRUDEOIL',7000),('GOLD',120000),('NATURALGAS',400),
                       ('SILVER',150000),('COPPER',1200)]:
        token += 1
        out.append(dict(name=name,exch_seg='MCX',instrumenttype='FUTCOM',
            token=str(token),symbol=name+'29SEP26FUT',expiry='29SEP2026',
            lotsize='10',strike='0',permitted_to_trade=True))
        for side in ('CE','PE'):
            token += 1
            out.append(dict(name=name,exch_seg='MCX',instrumenttype='OPTFUT',
                token=str(token),symbol=name+'28SEP26'+str(base)+side,
                expiry='28SEP2026',lotsize='10',strike=str(base*100),
                permitted_to_trade=True))
    return out


class MCXLiveTests(unittest.TestCase):
    def test_all_five_roots_are_catalogued(self):
        catalog=watch.catalog(rows(),NOW.date())
        self.assertEqual(set(r['name'] for r in catalog),set(mcx_live.ROOTS))
        self.assertIn('COPPER',watch.ROOTS)

    def test_instrument_master_keeps_mcx_lot_but_not_stock_universe(self):
        lot,sym,universe,spot={},{},{},{}
        row=rows()[0]
        im._absorb_row(row,lot,sym,universe,spot)
        self.assertEqual(lot[row['token']],10)
        self.assertEqual(sym[row['token']],row['symbol'])
        self.assertEqual(universe,{})
        self.assertEqual(spot,{})

    def test_active_contracts_recognises_mcx_options_for_reconciliation(self):
        source=rows()
        contracts=active_contracts(source,NOW.date())
        ce=next(r for r in source if r['name']=='CRUDEOIL'
                and r['instrumenttype']=='OPTFUT' and r['symbol'].endswith('CE'))
        fut=next(r for r in source if r['name']=='CRUDEOIL'
                 and r['instrumenttype']=='FUTCOM')
        self.assertIn(('MCX',ce['token']),contracts)
        self.assertEqual(contracts[('MCX',ce['token'])]['lot_size'],10)
        self.assertNotIn(('MCX',fut['token']),contracts)

    def test_option_mapping_requires_matching_side_and_not_after_future_expiry(self):
        catalog=watch.catalog(rows(),NOW.date())
        future=dict(next(r for r in catalog if r['name']=='CRUDEOIL' and r['instrumenttype']=='FUTCOM'),
                    ltp=7005,fresh=True)
        ce=mcx_live._option_for(catalog,future,'CE')
        pe=mcx_live._option_for(catalog,future,'PE')
        self.assertEqual((ce['type'],pe['type']),('CE','PE'))
        self.assertEqual((ce['strike'],pe['strike']),(7000,7000))

    def test_live_requires_explicit_flag_cap_and_global_auto(self):
        executor=types.SimpleNamespace(
            _reconcile_blocked=False,
            state=types.SimpleNamespace(positions={}))
        fake_cfg=dict(auto_trade_enabled=True,kill_latched=False)
        with patch.dict(os.environ,{'KADIMA_MCX_LIVE':'1','KADIMA_MCX_CAPITAL_INR':'15000'}), \
             patch.object(mcx_live,'macro_clear',return_value=True), \
             patch.object(mcx_live.celestial,'enabled',return_value=True), \
             patch.object(mcx_live.celestial,'read_config',return_value=fake_cfg), \
             patch.object(mcx_live.killswitch,'engaged',return_value=False), \
             patch.object(mcx_live.exit_manager,'realized_pnl_today',return_value=0), \
             patch.object(mcx_live.execution_ledger,'pending',return_value=[]):
            self.assertEqual(mcx_live.global_blocks(executor,NOW),[])
        with patch.dict(os.environ,{},clear=True):
            self.assertIn('mcx_live_disabled',mcx_live.global_blocks(executor,NOW))

    def test_macro_gate_fails_closed(self):
        with patch.object(mcx_live,'MACRO_SNAPSHOT_PATH','/definitely/missing'):
            self.assertFalse(mcx_live.macro_clear(NOW))

    def test_one_lot_entry_fails_closed_when_cost_exceeds_cap(self):
        executor=types.SimpleNamespace(
            _execution_lock=__import__('threading').RLock(),
            _reconcile_blocked=False,_last_reconcile=1,
            state=types.SimpleNamespace(positions={}))
        worker=mcx_live.Worker(executor,types.SimpleNamespace(rows=[]))
        row=dict(root='GOLD',direction='CE',token='T',symbol='GOLDCE',
            score=90,ts=NOW.isoformat(),baseline_at=NOW.isoformat(),ask=1000,
            stop=900,lot_size=100)
        with patch.object(mcx_live,'global_blocks',return_value=[]), \
             patch.object(mcx_live,'capital_limit',return_value=15000), \
             patch.object(mcx_live.capital_guard,'get_lot_size',return_value=100), \
             patch.object(mcx_live.capital_guard,'available_equity',return_value=100000):
            result=worker._enter(row,NOW)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'mcx_one_lot_cost_exceeds_capital')
        self.assertEqual(executor.state.positions,{})

    def test_underlying_structure_breach_requests_shared_exit(self):
        pos=types.SimpleNamespace(kind='mcx_momentum',status='open',swing_level=7000,
                                  index='CRUDEOIL',direction='CE',signal_id=99)
        executor=types.SimpleNamespace(
            _lock=__import__('threading').RLock(),
            state=types.SimpleNamespace(positions={'99':pos}),
            manual_exit=Mock())
        worker=mcx_live.Worker(executor,types.SimpleNamespace(rows=[]))
        worker._protect([dict(name='CRUDEOIL',fresh=True,ltp=6999)])
        executor.manual_exit.assert_called_once_with(99,'mcx_underlying_structure_failed')

    def test_signal_requires_full_five_minute_future_and_option_confirmation(self):
        executor=types.SimpleNamespace(state=types.SimpleNamespace(positions={}))
        worker=mcx_live.Worker(executor,types.SimpleNamespace(rows=[]))
        contracts=watch.catalog(rows(),NOW.date())
        future=dict(next(r for r in contracts if r['name']=='CRUDEOIL' and r['instrumenttype']=='FUTCOM'),
                    ltp=7000,volume=1000,oi=10000,bid=6999,ask=7001,
                    spread_pct=.03,fresh=True)
        # A fresh quote without a five-minute window never becomes a trade.
        with patch.object(mcx_live,'_fetch',return_value={'status':True,'data':{'fetched':[]}}):
            self.assertIsNone(worker._candidate(contracts,future,{}, {}, NOW))




class MCXInstallerTests(unittest.TestCase):
    @staticmethod
    def git_blob(raw):
        return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()

    def test_mcx_worker_has_no_direct_order_transport(self):
        tree=ast.parse(Path('kadima/mcx_live.py').read_text())
        imports=set()
        calls=set()
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                imports.add((node.module or '').split('.')[-1])
            elif isinstance(node,ast.Import):
                imports.update(a.name.split('.')[-1] for a in node.names)
            elif isinstance(node,ast.Call):
                fn=node.func
                name=fn.attr if isinstance(fn,ast.Attribute) else fn.id if isinstance(fn,ast.Name) else None
                if name=='place_limit_order':
                    calls.add(name)
        self.assertNotIn('angel_orders',imports)
        self.assertFalse(calls)

    def test_installer_composes_on_current_gap_executor(self):
        spec=importlib.util.spec_from_file_location('mcx_installer',Path('mcx_live_update/install.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.DEPLOYED_GAP_EXECUTOR,
                         'a163c2077ccd304dcd5c274333e4f8a32648b34e')
        self.assertEqual(module.COMPOSED_EXECUTOR,
                         '7cfe6cf21a520ba967d6a672b2f54a99bbe81321')
        self.assertIn(module.DEPLOYED_GAP_EXECUTOR,
                      module.base.FILES['kadima/auto_executor.py'])
        manifest=json.loads(Path('tools/activation_source.json').read_text())
        self.assertEqual(set(module.base.FILES),set(manifest))
        raw=module.base.read_file(module.ROOT/'kadima/auto_executor.py')
        self.assertEqual(module.base.digest(raw),module.COMPOSED_EXECUTOR)

    def test_installer_never_mutates_target_activation_helper(self):
        spec=importlib.util.spec_from_file_location('mcx_installer_boundary',
            Path('mcx_live_update/install.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertNotIn('tools/activate_execution.py',module.base.FILES)
        # go_live executes the helper from the reviewed release worktree,
        # so an older helper in /home/ubuntu/kadima-py cannot block install.
        go_spec=importlib.util.spec_from_file_location('mcx_go_live_boundary',
            Path('mcx_live_update/go_live.py'))
        go=importlib.util.module_from_spec(go_spec);go_spec.loader.exec_module(go)
        self.assertEqual((go.PACKAGE/'tools/activate_execution.py').resolve(),
                         Path('tools/activate_execution.py').resolve())

    def test_one_command_runner_stops_before_activation_if_install_fails(self):
        spec=importlib.util.spec_from_file_location('mcx_go_live',
            Path('mcx_live_update/go_live.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        failed=types.SimpleNamespace(returncode=7)
        with patch.object(module.subprocess,'run',return_value=failed) as run, \
             self.assertRaises(SystemExit):
            module.run('source_install',['python','installer'])
        run.assert_called_once()

    def test_activation_manifest_pins_mcx_runtime(self):
        manifest=json.loads(Path('tools/activation_source.json').read_text())
        self.assertEqual(manifest['kadima/auto_executor.py'],
                         '7cfe6cf21a520ba967d6a672b2f54a99bbe81321')
        self.assertEqual(manifest['kadima/mcx_live.py'],
                         '86928e87cca0fe6c085466f9c0b5c715d4ebb415')
        self.assertEqual(manifest['kadima/instrument_master.py'],
                         'a04a6c3636d49081fb49d48ac745aea0adc73f1f')


if __name__=='__main__':
    unittest.main()
