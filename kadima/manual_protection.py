"""Manual-option policy and warning state. No broker transport or new trail math."""
import json
import logging
import math

from . import alerts, audit, celestial, config, storage, trade_policy
from .utils import now_ist


def positive(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def record_event(event, **fields):
    """Observability must not prevent a durable adoption or protective exit."""
    try:
        audit.log(event,**fields)
        return True
    except Exception as exc:
        logging.getLogger(__name__).warning('Manual protection audit unavailable: %s',
                                            type(exc).__name__)
        return False


def warning_key(exchange, token, product):
    return json.dumps([str(exchange),str(token),str(product)],separators=(',',':'))


def warn_once(key, symbol, reason):
    """Persist accepted warning episodes; retry when the alert queue is full."""
    c = storage.conn()
    with c:
        saved = c.execute('SELECT reason FROM manual_protection_warnings WHERE key=?',(key,)).fetchone()
        if saved and saved[0]==reason:
            return False
        accepted = alerts.enqueue(f'Manual position {symbol} is NOT protected: '
                                  f'{reason}. Place an SL in Angel One.',
                                  key='manual-unprotected:'+key)
        if accepted is False:
            return False
        c.execute('INSERT OR REPLACE INTO manual_protection_warnings VALUES (?,?,?)',
                  (key,reason,now_ist().isoformat()))
    return True


def clear_warning(key):
    c = storage.conn()
    with c:
        c.execute('DELETE FROM manual_protection_warnings WHERE key=?',(key,))


def forget_absent_contracts(present):
    """Called only after a complete, valid broker-position observation."""
    keys = {warning_key(*key) for key in present}
    c = storage.conn()
    with c:
        for row in c.execute('SELECT key FROM manual_protection_warnings').fetchall():
            if row[0] not in keys:
                c.execute('DELETE FROM manual_protection_warnings WHERE key=?',(row[0],))


def evaluate(pos, ltp):
    """Pure manual decision, reusing the existing 50%-gain ratchet."""
    entry = pos.entry_price
    if not positive(entry) or not positive(ltp):
        raise ValueError('invalid_protection_inputs')
    stop_fraction = config.MANUAL_HARD_STOP_PCT
    activation = config.MANUAL_TRAIL_ACTIVATE_PCT
    if (type(stop_fraction) not in (int,float) or not 0<stop_fraction<1
            or not positive(activation)):
        raise ValueError('invalid_manual_protection_policy')
    peak = pos.peak_price if pos.peak_price is not None else entry
    previous = pos.effective_stop
    structural = pos.structural_stop
    if not positive(peak) or any(v is not None and not positive(v) for v in (previous,structural)):
        raise ValueError('invalid_protection_inputs')
    peak = max(peak,entry,ltp)
    hard = entry*(1-stop_fraction)
    structural = max(structural or 0,hard)
    active = pos.trail_active or peak>=entry*(1+activation)-1e-12
    if active:
        stop,_ = trade_policy.ratchet(entry,peak,previous,structural)
    else:
        stop = max(previous or 0,structural)
    reason = pos.exit_reason
    if not reason and ltp<=hard:
        reason = 'manual_hard_stop_breached'
    elif not reason and ltp<=stop:
        reason = 'manual_profit_trail_breached' if active else 'manual_tightened_stop_breached'
    return peak,stop,active,reason


def status(executor, pos, ltp):
    if not config.MANUAL_PROTECT_ENABLED:
        return 'opted_out'
    if pos.status!='open' or pos.exit_reason:
        return 'exit_pending'
    if not executor._positions_verified:
        return 'broker_unverified'
    verified = (str(pos.signal_id) in executor._celestial_verified
                if executor._manual_reconciliation or celestial.enabled()
                else executor._positions_verified and str(pos.signal_id) not in executor._exit_paused)
    if not verified:
        return 'broker_unverified'
    if not positive(ltp):
        return 'quote_unavailable'
    if not positive(pos.entry_price) or not positive(pos.effective_stop):
        return 'invalid_protection_inputs'
    if ltp<=pos.effective_stop:
        return 'exit_due'
    return 'monitoring'
