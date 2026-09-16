"""
control_api.py — KADIMA order-control bridge (manual dashboard actions).

Deliberately kept SEPARATE from kadima/api.py, whose docstring states it is
built as a read-only bridge for LLM access ("an LLM reads this, and an LLM
should not be able to reach anything consequential"). This file is the
opposite: it exposes routes that DO have real, immediate consequences
(closing a live position, cancelling/modifying a live order), and it is
meant to be called by the dashboard's Live Trade panel — not by an LLM.

No new order logic or risk logic lives here. Every route is a thin wrapper
around functions that already exist and are already tested:
    - EXECUTOR.manual_exit()      -> auto_executor.py (reuses _close_position())
    - angel_orders.cancel_order() -> angel_orders.py
    - angel_orders.modify_order() -> angel_orders.py
    - get_order_book()            -> auto_executor.py (already deployed)

Runs as its own uvicorn instance on port 8081, in its own thread, started
from run.py alongside the existing port-8080 state_api thread. Reuses the
existing KADIMA_API_TOKEN env var for auth, per Kusumakar's explicit
decision (2026-08-29) despite api.py's docstring warning that a prior
token value was flagged as exposed in an audit — rotate the token before
relying on this in production if that hasn't been done yet.

Confirmed against the live EC2 source on 2026-09-02:
    - angel_orders.cancel_order(order_id: str, variety: str = "NORMAL") -> OrderResult
    - angel_orders.modify_order(order_id: str, *, price: float | None = None, ...)
    - auto_executor.EXECUTOR = AutoExecutor()   (module-level singleton, line 695)
"""

from __future__ import annotations

import os
import hmac
from typing import Optional
from .forecast_observer import read_report as read_forecast_report

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from kadima import audit

app = FastAPI(title="KADIMA Control API", version="1.0")

# Set via register() at startup from run.py, same pattern as kadima/api.py's
# register(engine) for ENGINE.
EXECUTOR = None
ANGEL_ORDERS = None


def register(executor, angel_orders_module) -> None:
    """Wire in the live EXECUTOR singleton and angel_orders module.

    Called once from run.py at startup, after auto_executor.EXECUTOR and
    angel_orders are constructed — mirrors kadima/api.py's register(engine).
    """
    global EXECUTOR, ANGEL_ORDERS
    EXECUTOR = executor
    ANGEL_ORDERS = angel_orders_module


def _check_auth(authorization: Optional[str]) -> None:
    expected = os.environ.get("KADIMA_API_TOKEN")
    if not expected:
        # Fail closed: if the token isn't configured, refuse everything
        # rather than silently allowing unauthenticated control actions.
        raise HTTPException(status_code=503, detail="control_api_token_not_configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token,expected):
        raise HTTPException(status_code=401, detail="invalid_token")


class ManualExitBody(BaseModel):
    reason: str = "manual_exit_dashboard"


class ModifyOrderBody(BaseModel):
    price: Optional[float] = None
    quantity: Optional[int] = None


class CancelOrderBody(BaseModel):
    variety: str = "NORMAL"


@app.get("/health")
def health():
    # Unauthenticated, matches api.py's convention for /health.
    return {"ok": True, "service": "kadima-control-api"}


@app.post("/manual_exit/{signal_id}")
def manual_exit(signal_id: int, body: ManualExitBody, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if EXECUTOR is None:
        raise HTTPException(status_code=503, detail="executor_not_registered")
    audit.log("control_api_manual_exit_requested", signal_id=signal_id, reason=body.reason)
    result = EXECUTOR.manual_exit(signal_id, reason=body.reason)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.get("/order_book")
def order_book(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if EXECUTOR is None:
        raise HTTPException(status_code=503, detail="executor_not_registered")
    # get_order_book() confirmed as a module-level function in
    # auto_executor.py (def get_order_book() -> list[OrderStatus]:).
    from kadima.angel_orders import get_order_book
    return get_order_book()


@app.post("/cancel_order/{order_id}")
def cancel_order(order_id: str, body: CancelOrderBody, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if ANGEL_ORDERS is None:
        raise HTTPException(status_code=503, detail="angel_orders_not_registered")
    audit.log("control_api_cancel_order_requested", order_id=order_id, variety=body.variety)
    # cancel_order(order_id, variety="NORMAL") -> OrderResult, confirmed
    # live in angel_orders.py.
    result = ANGEL_ORDERS.cancel_order(order_id, variety=body.variety)
    if not getattr(result, "ok", False):
        raise HTTPException(status_code=409, detail=str(result))
    return result


@app.post("/modify_order/{order_id}")
def modify_order(order_id: str, body: ModifyOrderBody, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if ANGEL_ORDERS is None:
        raise HTTPException(status_code=503, detail="angel_orders_not_registered")
    audit.log(
        "control_api_modify_order_requested",
        order_id=order_id,
        price=body.price,
        quantity=body.quantity,
    )
    # modifyOrder() is confirmed live in angel_orders.py from the
    # deployed screenshots.
    from . import execution_ledger
    tracked = [r for r in execution_ledger.pending() if r['order_id'] == order_id]
    if tracked and body.quantity is not None and body.quantity != tracked[0]['quantity']:
        raise HTTPException(status_code=409, detail='tracked_order_quantity_change_not_supported')
    result = ANGEL_ORDERS.modify_order(order_id, price=body.price, quantity=body.quantity)
    if not getattr(result, "ok", False):
        raise HTTPException(status_code=409, detail=str(result))
    return result


# ============================================================================
# /dashboard_data -- added to feed the live dashboard's Session Ledger,
# Live Trade card, and Entry/SL/Target panel. Read-only (like api.py's
# routes), but lives here rather than in api.py because it reads EXECUTOR
# state (positions), which api.py's docstring deliberately keeps out of
# reach ("an LLM reads this, and an LLM should not be able to reach
# anything consequential"). Every number here is either read directly off
# an existing live object (Position, capital_guard, exit_manager) or
# computed with the same formulas already used elsewhere in the codebase
# (FIXED_STOPLOSS_PCT / GIVEBACK_TRAIL_PCT for the effective stop, 1R/2R/3R
# for targets, expected-move-from-IV + normal CDF for probability of touch
# -- the same design already described in the dashboard mockup's own note
# text, just not wired to real data before now).
# ============================================================================

import json
import math
import re

from kadima import capital_guard, config, exit_manager
from kadima import greeks as _greeks
from kadima.auto_executor import (
    FIXED_STOPLOSS_PCT,
    GIVEBACK_TRAIL_PCT,
    _poll_ltp,
)
from kadima.utils import now_ist

_STRIKE_RE = re.compile(r"(\d+(?:\.\d+)?)(CE|PE)$")


def _resolve_strike(pos) -> float | None:
    """Best-effort strike lookup: prefer the live option chain (has the
    real OptionQuote.strike for this token if it's still in the ATM+/-N
    ladder), else parse it out of the trading symbol as a fallback."""
    try:
        from kadima import api as _kadima_api
        if _kadima_api.ENGINE is not None:
            st = _kadima_api.ENGINE.states.get(pos.index)
            chain = getattr(st, "chain", None) if st else None
            q = chain.quotes.get(pos.token) if chain else None
            if q is not None and getattr(q, "strike", None):
                return float(q.strike)
    except Exception:  # noqa: BLE001
        pass
    m = _STRIKE_RE.search(pos.symbol or "")
    return float(m.group(1)) if m else None


def _spot_and_dte(pos):
    """(spot, days_to_expiry) for pos.index, reusing Engine._dte() rather
    than re-deriving expiry logic here."""
    try:
        from kadima import api as _kadima_api
        if _kadima_api.ENGINE is None:
            return None, None
        st = _kadima_api.ENGINE.states.get(pos.index)
        if st is None:
            return None, None
        return st.spot, _kadima_api.ENGINE._dte(st)
    except Exception:  # noqa: BLE001
        return None, None


def _prob_of_touch_pct(spot, iv_pct, days_to_expiry, distance) -> float | None:
    """Same formula as the dashboard mockup's probOfTouch(): expected move
    from IV, reflected normal-CDF approximation for probability of
    touching a barrier before expiry. Not a guarantee; assumes no
    directional drift -- see the note already printed under the panel."""
    if not spot or not iv_pct or distance is None:
        return None
    expected_move = spot * (iv_pct / 100.0) * math.sqrt(max(days_to_expiry or 0.02, 0.02) / 365.0)
    if expected_move <= 0:
        return None
    z = abs(distance) / expected_move
    p = 2 * (1 - _greeks._norm_cdf(z))
    return max(0.0, min(1.0, p)) * 100.0


def _position_view(pos) -> dict:
    ltp_map = _poll_ltp(pos.exchange, [pos.token])
    ltp = ltp_map.get(pos.token, pos.peak_price or pos.entry_price)

    fixed_stop = getattr(pos, 'structural_stop', None) or pos.entry_price * (1 - FIXED_STOPLOSS_PCT / 100.0)
    peak = pos.peak_price or pos.entry_price
    if pos.trail_active:
        trail_stop = peak - (GIVEBACK_TRAIL_PCT / 100.0) * (peak - pos.entry_price)
        effective_stop = max(fixed_stop, trail_stop)
    else:
        effective_stop = fixed_stop

    effective_stop = max(effective_stop, getattr(pos, 'effective_stop', None) or 0)
    risk = max(0, pos.entry_price - fixed_stop)
    targets = {
        "T1": pos.entry_price + risk * 1,
        "T2": pos.entry_price + risk * 2,
        "T3": pos.entry_price + risk * 3,
    }

    pnl_rupees = (ltp - pos.entry_price) * pos.remaining_qty
    pnl_pct = ((ltp - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price else 0.0

    spot, dte = _spot_and_dte(pos)
    strike = _resolve_strike(pos)
    iv_pct = None
    if spot and strike and dte is not None and ltp:
        try:
            iv_pct, _ = _greeks.implied_vol_and_greeks(spot, strike, ltp, dte, pos.direction)
        except Exception:  # noqa: BLE001
            iv_pct = None

    def _level(price, distance):
        prob = None if iv_pct is None else round(_prob_of_touch_pct(spot, iv_pct, dte, distance), 1)
        return {"price": round(price, 2), "probability_touch_pct": prob}

    levels = {
        "SL": _level(effective_stop, ltp - effective_stop),
    }
    for tag, price in targets.items():
        levels[tag] = _level(price, price - ltp)

    return {
        "signal_id": pos.signal_id,
        "status": pos.status,
        "entry_order_id": pos.order_id,
        "exit_order_id": pos.exit_order_id,
        "index": pos.index,
        "direction": pos.direction,
        "symbol": pos.symbol,
        "entry_price": pos.entry_price,
        "ltp": round(ltp, 2),
        "lots": pos.lots,
        "remaining_qty": pos.remaining_qty,
        "trail_active": pos.trail_active,
        "effective_stop": round(effective_stop, 2),
        "unrealized_pnl_inr": round(pnl_rupees, 0),
        "unrealized_pnl_pct": round(pnl_pct, 1),
        "levels": levels,
        "probability_note": (
            "Implied vol unavailable for this strike right now; probabilities omitted."
            if iv_pct is None else
            "Expected-move approximation from live IV; not a guarantee, assumes no directional drift."
        ),
    }


@app.get("/dashboard_data")
def dashboard_data(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if EXECUTOR is None:
        raise HTTPException(status_code=503, detail="executor_not_registered")

    open_positions = [p for p in EXECUTOR.state.positions.values() if p.status != "closed"]
    position_views = [_position_view(p) for p in open_positions]

    deployed = capital_guard.deployed_capital(EXECUTOR.state.positions)
    ceiling = capital_guard._buffered_ceiling()
    equity = capital_guard.available_equity()
    realized_today = exit_manager.realized_pnl_today()
    daily_loss_used = max(0.0, -realized_today)

    return {
        "ts": now_ist().isoformat(),
        "positions": position_views,
        "forecast_connection": getattr(EXECUTOR, "forecast_connection_status", {"status": "not_checked"}),
        "forecaster": read_forecast_report(),
        "stock_execution": {"mode": "central_executor_gated", "shared_slot": True},
        "session": {
            "realized_gain_today_inr": round(realized_today, 0),
            "capital_deployed_inr": None if deployed == float("inf") else round(deployed, 0),
            "capital_equity_inr": round(equity, 0),
            "capital_ceiling_inr": round(ceiling, 0),
            "daily_loss_used_inr": round(daily_loss_used, 0),
            "daily_loss_limit_inr": config.DAILY_LOSS_LIMIT_INR,
            "position_slots_used": len(open_positions),
            "position_slots_max": min(1, config.MAX_CONCURRENT_POSITIONS),
        },
    }


# ---------------------------------------------------------------------
# /universe_snapshot -- serves stock_universe_scanner.py's real, currently
# -tracked-stock scores to the dashboard. That scanner runs as a SEPARATE
# process/systemd service (kadima-universe-scanner), deliberately isolated
# from this engine process for memory-safety reasons -- so this route does
# NOT talk to it directly. It reads the small JSON snapshot file the
# scanner writes atomically after each scoring cycle (~30s cadence),
# same hand-off pattern as last_known.py. Only ever contains up to
# MAX_SHORTLIST (20) currently-tracked stocks -- the scanner only ever
# scores a bounded rotating subset of the full F&O universe, by design.
UNIVERSE_SNAPSHOT_PATH = os.path.expanduser("~/kadima-py/state/universe_snapshot.json")


@app.get("/universe_snapshot")
def universe_snapshot(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    try:
        with open(UNIVERSE_SNAPSHOT_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # Scanner hasn't written a snapshot yet (fresh deploy, or it's
        # not running) -- not an error, just nothing tracked yet.
        return {"ts": None, "tracked": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=f"universe_snapshot_unavailable: {exc}")


# Same hand-off pattern as UNIVERSE_SNAPSHOT_PATH above, written by the
# separate, isolated macro_watch.py process (India VIX, FII/DII flows,
# real news headlines, economic calendar). See that module's docstring
# for exactly what each field is and how it degrades when a fetch fails
# -- this route just passes the file through as-is, no interpretation.
MACRO_SNAPSHOT_PATH = os.path.expanduser("~/kadima-py/state/macro_snapshot.json")


@app.get("/macro_snapshot")
def macro_snapshot(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    try:
        with open(MACRO_SNAPSHOT_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # macro_watch.py hasn't written a snapshot yet (fresh deploy, or
        # it's not running) -- not an error, just nothing fetched yet.
        return {"ts": None, "india_vix": None, "fii_dii": None, "news": [],
                "economic_calendar": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=f"macro_snapshot_unavailable: {exc}")


# Celestial control routes: only the authenticated same-origin gateway exposes
# these to a human dashboard. The read-only LLM bridge is unchanged.
from fastapi import Body
from . import celestial, execution_ledger


def _celestial_executor():
    if EXECUTOR is None:
        raise HTTPException(503,'executor_not_registered')
    if not celestial.enabled():
        raise HTTPException(409,'celestial_rollout_not_enabled')
    return EXECUTOR


def _control_result(result):
    if not result or result.get('ok') is not True:
        raise HTTPException(409,result or {'reason':'no_execution_result'})
    return result


@app.get('/api/kadima/config')
def celestial_config(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return dict(celestial.read_config(),rollout_enabled=celestial.enabled(),
                broker_orders_enabled=config.ORDER_PLACEMENT_ENABLED)


@app.post('/api/kadima/config')
def set_celestial_config(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    data = dict(body)
    revision = data.pop('revision',None)
    try:
        with ex._execution_lock:
            result = celestial.update_config(data,expected_revision=revision)
    except ValueError as exc:
        raise HTTPException(409,str(exc))
    audit.log('celestial_config_changed',revision=result['revision'])
    return result


@app.get('/api/kadima/top20')
def celestial_top20(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    rows = celestial.top20()
    # UI reads cached monitor quotes, never manufactures a fresh quote from peak.
    positions = []
    if EXECUTOR is not None:
        with EXECUTOR._execution_lock:
            for p in EXECUTOR.state.positions.values():
                if p.status=='closed': continue
                quote = EXECUTOR._celestial_quotes.get(p.exchange+':'+p.token) or {}
                ltp = quote.get('ltp') if celestial.fresh(quote.get('ts')) else None
                positions.append(dict(signal_id=p.signal_id,symbol=p.symbol,exchange=p.exchange,token=p.token,
                    ltp=ltp,quote_at=quote.get('ts'),unrealized_pnl_inr=(ltp-p.entry_price)*p.remaining_qty if ltp is not None else None,
                    direction=p.direction,status=p.status,remaining_qty=p.remaining_qty,entry_price=p.entry_price,
                    source='MANUAL_ADOPTED' if p.kind=='MANUAL_ADOPTED' else 'KADIMA_AUTO',
                    effective_stop=p.effective_stop,trail_active=p.trail_active,
                    entry_order_id=p.order_id,exit_order_id=p.exit_order_id,exit_reason=p.exit_reason,
                    protection='engine_managed_not_broker_hosted'))
    by_contract = {p['exchange']+':'+p['token']:p for p in positions}
    for row in rows:
        p = by_contract.get(row['id'])
        if EXECUTOR is None or EXECUTOR._reconcile_blocked:
            row['eligible']=False
            row['blocked_reasons'].append('broker_reconciliation_required')
            row['action']='RADAR'
        if p:
            row['action']='EXIT' if p['exit_reason'] or p['status']=='exit_pending' else 'TRAIL' if p['trail_active'] else 'ENTERED'
    return dict(ts=now_ist().isoformat(),candidates=rows,positions=positions,
        config=celestial_config(authorization),system={name:celestial.health(name) for name in
            ('index_universe','stock_universe','reconciliation','news')},
        readiness_note='Candidate prechecks only; execution rechecks alignment, structure, sizing, slots and broker state.',
        score_note='Heuristic scores, not win probabilities. No guaranteed 5% capture or fills.')


@app.post('/api/order/buy')
def celestial_buy(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if set(body)!={'candidate_id'} or not isinstance(body['candidate_id'],str):
        raise HTTPException(422,'candidate_id_required_no_raw_orders')
    return _control_result(_celestial_executor().buy_candidate(body['candidate_id']))


@app.post('/api/order/sell')
@app.post('/api/order/exit')
def celestial_exit(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if set(body)!={'signal_id'} or type(body['signal_id']) is not int or body['signal_id']<=0:
        raise HTTPException(422,'positive_signal_id_required_sell_is_reduce_only')
    return _control_result(_celestial_executor().manual_exit(body['signal_id']))


@app.post('/api/order/sl')
@app.post('/api/order/modify-sl')
def celestial_stop(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if set(body)!={'signal_id','price'} or type(body['signal_id']) is not int:
        raise HTTPException(422,'signal_id_and_price_required')
    return _control_result(_celestial_executor().tighten_stop(body['signal_id'],body['price']))


@app.post('/api/order/trailing-sl')
def celestial_trail(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    if set(body)!={'signal_id','retain'} or type(body['signal_id']) is not int:
        raise HTTPException(422,'signal_id_and_retain_required')
    with ex._execution_lock:
        p = ex.state.positions.get(str(body['signal_id']))
        if not p or p.remaining_qty<=0:
            raise HTTPException(409,'owned_position_not_found')
        try:
            celestial.set_retain(body['signal_id'],body['retain'])
        except ValueError as exc:
            raise HTTPException(422,str(exc))
    return {'ok':True,'retain':celestial.retain_fraction(body['signal_id'])}


@app.post('/api/order/cancel')
def celestial_cancel(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    if set(body)!={'order_id'} or not isinstance(body['order_id'],str):
        raise HTTPException(422,'order_id_required')
    if ANGEL_ORDERS is None:
        raise HTTPException(503,'broker_transport_unavailable')
    with ex._execution_lock:
        rows = [r for r in execution_ledger.pending() if r['order_id']==body['order_id']]
        if len(rows)!=1:
            raise HTTPException(409,'only_tracked_pending_orders_can_be_cancelled')
        result = ANGEL_ORDERS.cancel_order(body['order_id'])
        if not result.ok:
            raise HTTPException(409,'cancellation_not_confirmed')
        return {'ok':True,'reason':'cancel_requested_awaiting_terminal_broker_state'}


@app.post('/api/order/squareoff-all')
def celestial_squareoff(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return _celestial_executor().squareoff()


@app.post('/api/kadima/pause')
def celestial_pause(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    with ex._execution_lock:
        return celestial.update_config({'auto_trade_enabled':False})


@app.post('/api/kadima/resume')
def celestial_resume(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    with ex._execution_lock:
        ex.reconcile_on_startup()
        if ex._reconcile_blocked:
            raise HTTPException(409,'broker_reconciliation_required')
        h = celestial.health('news')
        if not h or h.get('ok') is not True or not celestial.fresh(h['ts'],seconds=300):
            raise HTTPException(409,'fresh_news_coverage_required')
        try:
            return celestial.update_config({'auto_trade_enabled':True})
        except ValueError as exc:
            raise HTTPException(409,str(exc))


@app.post('/api/kadima/kill')
def celestial_kill(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    with ex._execution_lock:
        celestial.latch_kill()
        return ex.squareoff('emergency_kill_exit')


@app.post('/api/kadima/reset-kill')
def celestial_reset_kill(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    ex = _celestial_executor()
    with ex._execution_lock:
        ex.reconcile_on_startup()
        if ex._reconcile_blocked or ex.state.positions or execution_ledger.pending():
            raise HTTPException(409,'verified_flat_account_required')
        return celestial.update_config({'auto_trade_enabled':False},reset_kill=True)


@app.post('/api/kadima/news')
def celestial_news(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from .news_catalysts import ingest
    if set(body)!={'events','source','observed_at','covered_symbols'}:
        raise HTTPException(422,'events_source_observed_at_covered_symbols_required')
    try:
        return ingest(body['events'],source=body['source'],observed_at=body['observed_at'],
                      covered_symbols=body['covered_symbols'])
    except (ValueError,TypeError,KeyError) as exc:
        raise HTTPException(422,str(exc))


@app.get('/api/operations/state')
def operations_state(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from . import memory_guard, storage
    from datetime import timedelta
    rows = storage.conn().execute(
        "SELECT id,ts,index_name,tier,score,direction,payload,kind FROM signals "
        "WHERE kind IN ('gamma_blast','gamma_strike','cas_blast') AND ts>=? ORDER BY id DESC LIMIT 40",
        ((now_ist()-timedelta(days=1)).isoformat(),)).fetchall()
    detections=[]
    for r in rows:
        d=dict(r)
        try:
            import json
            payload=json.loads(d.pop('payload'))
            d['symbol']=payload.get('symbol') if isinstance(payload,dict) else None
        except (ValueError,TypeError):
            d['symbol']=None
        detections.append(d)
    return dict(ts=now_ist().isoformat(),memory_guard=memory_guard.status(),
        cas=celestial.health('cas_detection'),detections=detections,
        reconciliation_blocked=EXECUTOR is None or EXECUTOR._reconcile_blocked)


@app.post('/api/operations/entries')
def operations_entries(body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from . import memory_guard
    import re
    if set(body)!={'action','token'} or body.get('action') not in ('hold','release') or not isinstance(body.get('token'),str) or not re.fullmatch('[a-f0-9]{32}',body['token']):
        raise HTTPException(422,'invalid_operations_request')
    if EXECUTOR is None:
        raise HTTPException(503,'executor_unavailable')
    # The acknowledgement waits for any in-flight serialized entry to finish.
    # Subsequent entry checks, including the broker boundary, read this hold.
    if not EXECUTOR._execution_lock.acquire(timeout=2):
        raise HTTPException(409,'entry_operation_in_progress')
    try:
        if body['action']=='hold':
            memory_guard.hold_entries(body['token'])
            ok=True
        else:
            ok=memory_guard.release_entries(body['token'])
        audit.log('operations_entry_hold',action=body['action'],ok=ok)
        return {'ok':ok}
    finally:
        EXECUTOR._execution_lock.release()
