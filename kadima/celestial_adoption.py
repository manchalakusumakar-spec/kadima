"""Verified manual long-option protection; unsupported exposure stays visible.

External pending orders and changes to managed quantities are never assumed fills.
"""
import json
import math
from . import celestial, config, manual_protection, storage
from .angel_orders import product_for
from .celestial_universe import active_contracts
from .utils import now_ist


def inspect(executor, broker_rows, book, owned_ids, owned_tags, master):
    contracts = active_contracts(master.rows,now_ist().date())
    expected = {}
    for p in executor.state.positions.values():
        if p.remaining_qty and p.status!='closed':
            key = p.exchange,p.token,product_for(p)
            expected[key] = expected.get(key,0)+p.remaining_qty
    from .execution_ledger import TERMINAL
    external_pending = [o for o in book if o.status not in TERMINAL and
                        o.order_id not in owned_ids and o.raw.get('ordertag') not in owned_tags]
    pending_contracts = {(str(o.raw.get('exchange')),str(o.raw.get('symboltoken')))
                         for o in external_pending}
    allow = celestial.read_config()['adopt_external_positions'] and config.MANUAL_PROTECT_ENABLED
    live, observations, new = {}, [], []
    for row in broker_rows:
        try:
            qty = float(row.get('netqty'))
            if not math.isfinite(qty) or qty != int(qty): raise ValueError()
            if qty==0: continue
            key = str(row['exchange']),str(row['symboltoken']),str(row['producttype'])
            if key in live: raise ValueError('Duplicate position')
            live[key] = int(qty)
            view = dict(exchange=key[0],token=key[1],product=key[2],quantity=int(qty),
                        symbol=str(row.get('tradingsymbol','')),source='BROKER_EXTERNAL',managed=False)
            contract = contracts.get(key[:2])
            if expected.get(key)==qty:
                adopted = next((p.kind for p in executor.state.positions.values()
                               if p.kind in ('MANUAL_ADOPTED','MANUAL_PROTECTED')
                               and (p.exchange,p.token,product_for(p))==key), None)
                view.update(source=adopted or 'KADIMA_AUTO',managed=True,reason='reconciled')
            elif key in expected:
                view['reason']='managed_quantity_changed_externally'
            elif not allow or key[:2] in pending_contracts:
                view['reason']='adoption_disabled_or_external_order_pending'
            elif (qty<=0 or key[2] not in ('INTRADAY','CARRYFORWARD','NRML')
                  or contract is None or contract['instrumenttype'] not in ('OPTIDX','OPTSTK')):
                view['reason']='unsupported_position_type_or_contract'
            elif qty % contract['lot_size'] or view['symbol']!=contract['symbol']:
                view['reason']='contract_or_lot_mismatch'
            else:
                try:
                    price = float(__import__(__package__ + '.protection', fromlist=['entry_basis']).entry_basis(row) or 0)  # 2026-10-19 CF fix
                except (TypeError,ValueError):
                    price = 0
                if not math.isfinite(price) or price<=0:
                    view['reason']='entry_basis_unavailable'
                else:
                    new.append(dict(key=key,quantity=int(qty),entry_price=price,contract=contract))
                    view.update(source='MANUAL_PROTECTED',managed=True,reason='adoption_verified')
            observations.append(view)
        except (KeyError,ValueError,TypeError,OverflowError):
            observations.append(dict(source='UNKNOWN',managed=False,reason='invalid_broker_position'))
    for key,qty in expected.items():
        if key not in live:
            observations.append(dict(exchange=key[0],token=key[1],product=key[2],quantity=qty,
                                     source='UNKNOWN',managed=False,reason='managed_position_missing_at_broker'))
    if external_pending:
        observations.append(dict(source='BROKER_EXTERNAL',managed=False,reason='external_pending_orders',
                                 count=len(external_pending)))
    present = set(live)
    if not any(view.get('reason')=='invalid_broker_position' for view in observations):
        manual_protection.forget_absent_contracts(present | set(expected))
    for view in observations:
        key = view.get('exchange'),view.get('token'),view.get('product')
        if (allow and view.get('quantity',0)>0 and not view.get('managed')
                and key in present):
            manual_protection.warn_once(manual_protection.warning_key(*key),
                                        view.get('symbol') or key[1],view['reason'])
    return live, expected, observations, new


def adopt(executor, proposals):
    from .auto_executor import Position
    for item in proposals:
        r,qty,price = item['contract'],item['quantity'],item['entry_price']
        exchange,token,product = item['key']
        stop = price*(1-config.MANUAL_HARD_STOP_PCT)
        c = storage.conn()
        with c:
            sid = storage.insert_signal(r['name'],'ADOPTED',0,r['symbol'][-2:],
                json.dumps(dict(source='MANUAL_PROTECTED',exchange=exchange,token=token,
                                quantity=qty,product=product)),kind='MANUAL_PROTECTED',_connection=c)
            p = Position(sid,'MANUAL_PROTECTED',r['name'],r['symbol'][-2:],r['symbol'],token,exchange,
                price,now_ist(),qty//r['lot_size'],r['lot_size'],qty,peak_price=price,
                structural_stop=stop,effective_stop=stop,allocated_capital=qty*price,product=product)
            storage.upsert_position(p,_connection=c)
        executor.state.positions[str(sid)] = p
        manual_protection.record_event('manual_position_adopted',signal_id=sid,symbol=p.symbol,
                  product=product,quantity=qty,entry_price=price,
                  effective_stop=stop,reason='adoption_verified')
