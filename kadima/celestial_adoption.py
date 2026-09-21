"""Verified long-INTRADAY option adoption; unsupported exposure stays visible.

External pending orders and changes to managed quantities are never assumed fills.
"""
import json
import math
from . import celestial, storage
from .celestial_universe import active_contracts
from .utils import now_ist


def inspect(executor, broker_rows, book, owned_ids, owned_tags, master):
    contracts = active_contracts(master.rows,now_ist().date())
    expected = {}
    for p in executor.state.positions.values():
        if p.remaining_qty and p.status!='closed':
            key = p.exchange,p.token,'INTRADAY'
            expected[key] = expected.get(key,0)+p.remaining_qty
    from .execution_ledger import TERMINAL
    external_pending = [o for o in book if o.status not in TERMINAL and
                        o.order_id not in owned_ids and o.raw.get('ordertag') not in owned_tags]
    allow = celestial.read_config()['adopt_external_positions'] and not external_pending
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
                adopted = any(p.kind=='MANUAL_ADOPTED' and (p.exchange,p.token)==key[:2]
                              for p in executor.state.positions.values())
                view.update(source='MANUAL_ADOPTED' if adopted else 'KADIMA_AUTO',managed=True,reason='reconciled')
            elif key in expected:
                view['reason']='managed_quantity_changed_externally'
            elif not allow:
                view['reason']='adoption_disabled_or_external_order_pending'
            elif qty>0 and key[2]=='CARRYFORWARD' and contract is not None:
                if qty % contract['lot_size'] or view['symbol']!=contract['symbol']:
                    view['reason']='contract_or_lot_mismatch'
                else:
                    # Verified manual carry-forward exposure is reconciled as
                    # known-but-unmanaged. KADIMA never creates a Position for
                    # it, never exits/trails it, and only blocks new entries in
                    # the same underlying while it remains at the broker.
                    view.update(source='MANUAL_CARRYFORWARD',managed=True,
                        auto_managed=False,reason='carryforward_observed_not_managed',
                        underlying=str(contract.get('name') or ''))
            elif qty<=0 or key[2]!='INTRADAY' or contract is None:
                view['reason']='unsupported_position_type_or_contract'
            elif qty % contract['lot_size'] or view['symbol']!=contract['symbol']:
                view['reason']='contract_or_lot_mismatch'
            else:
                price = float(row.get('buyavgprice') or 0)
                if not math.isfinite(price) or price<=0:
                    view['reason']='entry_basis_unavailable'
                else:
                    new.append(dict(key=key,quantity=int(qty),entry_price=price,contract=contract))
                    view.update(source='MANUAL_ADOPTED',managed=True,reason='adoption_verified')
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
    return live, expected, observations, new


def adopt(executor, proposals):
    from .auto_executor import Position
    for item in proposals:
        r,qty,price = item['contract'],item['quantity'],item['entry_price']
        exchange,token,_ = item['key']
        sid = storage.insert_signal(r['name'],'ADOPTED',0,r['symbol'][-2:],
            json.dumps(dict(source='MANUAL_ADOPTED',exchange=exchange,token=token,quantity=qty)),
            kind='MANUAL_ADOPTED')
        p = Position(sid,'MANUAL_ADOPTED',r['name'],r['symbol'][-2:],r['symbol'],token,exchange,
            price,now_ist(),qty//r['lot_size'],r['lot_size'],qty,peak_price=price,
            structural_stop=price*.95,effective_stop=price*.95,allocated_capital=qty*price)
        storage.upsert_position(p)
        executor.state.positions[str(sid)] = p
