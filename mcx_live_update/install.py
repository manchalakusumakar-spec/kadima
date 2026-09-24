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
COMPOSED_EXECUTOR='7cfe6cf21a520ba967d6a672b2f54a99bbe81321'
DEPLOYED_GAP_EXECUTOR='a163c2077ccd304dcd5c274333e4f8a32648b34e'

spec=importlib.util.spec_from_file_location('_mcx_base_installer',BASE_PATH)
base=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=base
spec.loader.exec_module(base)

_original_read_file=base.read_file
_composed_path=ROOT/'mcx_live_update/auto_executor.py'
if base.digest(_original_read_file(_composed_path)) != COMPOSED_EXECUTOR:
    raise base.Blocked('composed_mcx_executor_hash_mismatch')

def _mcx_read_file(path, missing=False):
    path=Path(path)
    if path == ROOT/'kadima/auto_executor.py':
        return _original_read_file(_composed_path, missing=missing)
    return _original_read_file(path, missing=missing)

base.read_file=_mcx_read_file


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
    # Complete reviewed Gap-era runtime lineage -> MCX runtime.
    # Unknown/local modifications still fail closed; only these reviewed
    # predecessors (or the exact incoming MCX source) are accepted.
    'kadima/angel_orders.py':'6e379765f37044c255e5ca61b2a3b05abeec77dd',
    'kadima/auto_executor.py':(
        '362923e19b763975d1a6cf8c18a23459585e0259',
        '502744a63d0b5a5d5aa9c74c39e7bd7cf5334c88',
        '6161c04be1266e5e9b010f67a9466c22c0fc8ef0',
        DEPLOYED_GAP_EXECUTOR),
    'kadima/celestial.py':'dfac18b9d76f11428357cc7d6b4d8f88d15278fa',
    'kadima/celestial_adoption.py':(
        '2273bb837257a09ba9d9e5a7bae52560f595e3bf',
        '68e66fca7baca0b42cc29b80cd41cff301ae4f57'),
    'kadima/control_api.py':'6734f4e822b2c6cf14fae88539a4bfd87b7292fb',
    'kadima/engine.py':'a1875f1d73889c850403320cd30e583cbf5ca469',
    'kadima/execution_ledger.py':'1fde45163927f6fceff4a6ae8573e4242ac4fb18',
    'kadima/external_reconciliation.py':'c1eb8c7bb0e0b7f6842ff2aacbc7401f60f21ec9',
    'kadima/fast_momentum.py':'7eb165d7ae89be70133b0a8a4d4efeed964a7d70',
    'kadima/full_universe_scanner.py':'530c41a3fe4fe1e706dcfde3437f4c16e1a19b69',
    'kadima/news_catalysts.py':'2cdbcdd91616c75b8c57a9dc6192b6b6aa2c09e9',
    'kadima/news_collector.py':(
        '8a6a2375924b7551effb88e48204c7564ee4ae48',
        '98c0e36ef614da41096df686fa49d6333d62114b'),
    'kadima/shield_confirmation.py':'88f8485c1f8845147db8074fb82b1dcda0bc90ef',
    'kadima/stock_universe_scanner.py':'1117df7df0825e918a5ae119e9abb00d0627df62',
    'run.py':'86f8232e1efee90e5eed20cd507f360f3ab969bf',
    'kadima/stock_fast.py':'e84932b645d584787df21e1bb94ce927d80b6cc3',
    'kadima/stock_fast_api.py':'0d9b50418d6cc12752ebf96114e98504eaca455d',
    'kadima/feed.py':'8a9f8ea0aa5a708739c9297583badabc85fc2c2a',
    'kadima/rate_limiter.py':'9886d951be6997e0ddf8fdda640529f5f751a25c',
    'kadima/gap_and_go.py':'b49527a16984c306293b5b9874fb17ed88c6ee31',
    'kadima/gap_baseline.py':'97e2851b4c599ca7b04fedba2bacc39169074455',
    'kadima/gap_live.py':'9f282800528fd942ff5da8014da93c2ac3602b34',
    'kadima/portfolio_capacity.py':'c5d92290e608d22241afbad88cf5eb6dcd4bf22b',
    'kadima/market_session_gate.py':'5633eba4177f6ade457d6bdd32ec4e154f27e68a',
    'kadima/commodity_watch.py':'7103168af0cbe9aec4f622dd41627da7854697a9',
    'kadima/instrument_master.py':'7d1338047157f1ec91cf19f3ec6099ca5b658c2e',
    'kadima/celestial_universe.py':'26f2433d9d3ca09389c5f9485105a4008f99b9de',
    'kadima/mcx_live.py':None,
    # Ledger fill reconciliation imports these only for persisted stop bindings.
    'kadima/stop_runtime.py':None,
    'kadima/regime_stops.py':None,
    'kadima/structure_guard.py':None,
    'kadima/structure_levels.py':None,
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
