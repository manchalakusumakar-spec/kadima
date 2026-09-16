"""Resolve active option roots/expiries from contract data, never weekdays."""
from datetime import datetime
from . import config


def active_contracts(rows, on):
    result = {}
    for row in rows:
        try:
            if row.get('exch_seg') not in ('NFO','BFO') or row.get('instrumenttype') not in ('OPTIDX','OPTSTK'):
                continue
            expiry = datetime.strptime(row['expiry'],'%d%b%Y').date()
            lot = int(row['lotsize'])
            if expiry < on or lot <= 0 or not row['token'] or not row['symbol'].endswith(('CE','PE')):
                continue
            if row.get('permitted_to_trade') is False:
                continue
            result[(row['exch_seg'],str(row['token']))] = dict(row,expiry_date=expiry,lot_size=lot)
        except (TypeError,ValueError,KeyError):
            continue
    return result


def discover_indices(scrips, on):
    roots = {(r['exch_seg'],r['name']) for r in active_contracts(scrips.rows,on).values()
             if r['instrumenttype']=='OPTIDX'}
    resolved, missing = {}, []
    for exchange,name in sorted(roots):
        reference = config.GAMMA_INDICES.get(name)
        if reference is not None and reference.opt_exchange==exchange:
            resolved[name] = reference
            continue
        cash = 'NSE' if exchange=='NFO' else 'BSE'
        spots = [r for r in scrips.rows if r.get('exch_seg')==cash
                 and r.get('instrumenttype') in ('AMXIDX','INDEX')
                 and (r.get('name')==name or r.get('symbol')==name)]
        step = scrips.infer_strike_step(name,exchange)
        if len(spots)!=1 or not step:
            missing.append(dict(name=name,exchange=exchange,reason='spot_mapping_unverified'))
            continue
        resolved[name] = config.Index(name,cash,str(spots[0]['token']),exchange,name,step,.1)
    return resolved, missing
