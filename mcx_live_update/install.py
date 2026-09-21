"""Guarded MCX live source installer.

Extends the reviewed Gap/live KADIMA source with the MCX evening worker.
Plan-only by default. Broker check is read-only. Source installation never arms
KADIMA_MCX_LIVE; activation remains a separate reviewed step.
"""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
BASE_PATH=ROOT/'stock_fast_update/install.py'

spec=importlib.util.spec_from_file_location('_mcx_base_installer',BASE_PATH)
base=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=base
spec.loader.exec_module(base)


def _paused_status(status):
    if not isinstance(status,dict) or not isinstance(status.get('config'),dict):
        raise base.Blocked('control_schema_unknown')
    cfg=status['config']
    if type(cfg.get('rollout_enabled')) is not bool:
        raise base.Blocked('celestial_rollout_state_unknown')
    if cfg.get('auto_trade_enabled') is not False:
        raise base.Blocked('global_auto_must_be_paused_for_mcx_install')
    if cfg.get('kill_latched') is not False:
        raise base.Blocked('kill_state_requires_operator_review')
    if not isinstance(status.get('positions'),list) or status['positions']:
        raise base.Blocked('local_api_exposure_or_unknown')
    try:
        timestamp=base.datetime.fromisoformat(status['ts'])
        age=(base.datetime.now(base.timezone.utc)-timestamp).total_seconds()
        if timestamp.tzinfo is None or not 0 <= age <= 30:
            raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise base.Blocked('stale_or_unknown_control_timestamp') from None

base.paused_status=_paused_status


def _carryforward_preflight_code(code):
    contract_anchor="    SESSION.ensure()\n    for name in ('position','orderBook'):\n"
    position_anchor="""                if quantity != 0:
                    reason = 'broker_positions_not_flat'
                    raise RuntimeError()
"""
    if code.count(contract_anchor)!=1 or code.count(position_anchor)!=1:
        raise base.Blocked('broker_preflight_shape_changed')
    contract_block="""    SESSION.ensure()
    from kadima.scrip_master import ScripMaster
    from kadima.celestial_universe import active_contracts
    from kadima.utils import now_ist
    contracts = active_contracts(ScripMaster.load().rows, now_ist().date())
    for name in ('position','orderBook'):
"""
    position_block="""                if quantity != 0:
                    product = str(row.get('producttype') or '')
                    exchange = str(row.get('exchange') or '')
                    token = str(row.get('symboltoken') or '')
                    symbol = str(row.get('tradingsymbol') or '')
                    contract = contracts.get((exchange, token))
                    carry_ok = (
                        product == 'CARRYFORWARD' and quantity > 0
                        and quantity == int(quantity) and contract is not None
                        and int(quantity) % int(contract['lot_size']) == 0
                        and symbol == str(contract['symbol']))
                    if not carry_ok:
                        reason = 'broker_positions_not_flat'
                        raise RuntimeError()
"""
    result=code.replace(contract_anchor,contract_block,1).replace(position_anchor,position_block,1)
    compile(result,'mcx_carryforward_broker_preflight','exec')
    return result

base.broker_preflight_code=_carryforward_preflight_code

# Exact current reviewed source -> MCX release. Unknown target bytes refuse.
base.FILES={
    'run.py':'86f8232e1efee90e5eed20cd507f360f3ab969bf',
    'kadima/engine.py':'a1875f1d73889c850403320cd30e583cbf5ca469',
    'kadima/auto_executor.py':'502744a63d0b5a5d5aa9c74c39e7bd7cf5334c88',
    'kadima/commodity_watch.py':'7103168af0cbe9aec4f622dd41627da7854697a9',
    'kadima/instrument_master.py':'7d1338047157f1ec91cf19f3ec6099ca5b658c2e',
    'kadima/celestial_universe.py':'26f2433d9d3ca09389c5f9485105a4008f99b9de',
    'kadima/mcx_live.py':None,
    'tools/activate_execution.py':'055e05bc2f20b2faf0a4fb4de4aae579249a06ac',
}


def main():
    base.main()


if __name__=='__main__':
    try:
        fd=os.open('/run/lock/kadima-mcx-live-install.lock',
                   os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            main()
    except base.Blocked as exc:
        print('BLOCKED:',str(exc))
        raise SystemExit(1)
    except Exception as exc:
        print('BLOCKED:',type(exc).__name__,'(details suppressed; no credential output)')
        raise SystemExit(1)
