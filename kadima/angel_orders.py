"""SmartAPI order transport; acceptance is distinct from confirmed execution.

LIMIT/DAY/INTRADAY defaults retain the deployment's existing order format.
The execution ledger owns fill reconciliation and uncertain submission recovery.
Broker reference: https://smartapi.angelone.in/docs/Instruments
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from . import audit
from .config import CAS_PRODUCT_TYPE, ORDER_PLACEMENT_ENABLED
from .rate_limiter import ORDER_STATUS, ORDER_WRITE
from .session import SESSION

CAS_KIND = "cas_blast"


def product_for(pos) -> str:
    """Broker producttype a position's orders must use. Every order, fill
    check and reconciliation for a position goes through this, so a
    CARRYFORWARD CAS entry is never exited with an INTRADAY SELL (which
    Angel would treat as a separate, new short position)."""
    # Existing CAS positions retain their original product even if config drifts.
    return "CARRYFORWARD" if getattr(pos, "kind", None) == CAS_KIND else "INTRADAY"


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str]
    reason: str
    dry_run: bool = False
    uncertain: bool = False


@dataclass
class OrderStatus:
    order_id: str
    status: str              # broker's own status string, passed through
    filled_qty: int
    avg_price: Optional[float]
    raw: dict


# ---------------------------------------------------------------- place
def place_limit_order(*, tradingsymbol: str, symboltoken: str, exchange: str,
                      transactiontype: str, quantity: int, price: float,
                      producttype: str = "INTRADAY", ordertag: str | None = None,
                      signal_id: int | None = None) -> OrderResult:
    """
    transactiontype: "BUY" (entry) or "SELL" (exit, squaring off a long).
    price: the limit price. Caller decides this — this function does not
    adjust it (no chasing logic here; that belongs in the caller, which
    has the context of why it's placing the order).

    producttype: defaults to INTRADAY, matching the live trading path
    (auto_executor.py never overrides this — see module docstring for
    the full history of NRML being tried and reverted). The override
    exists for diagnostic use (test_order_pathway.py isolating exactly
    this variable, 2026-08-23, which is how the NRML rejection was
    actually found) — nothing in the real trading pipeline should pass
    a different value here without the same kind of deliberate testing.

    When ORDER_PLACEMENT_ENABLED is False, does everything EXCEPT the
    actual broker call: validates inputs, logs exactly what would have
    been sent, and returns a synthetic dry-run order id. This lets the
    rest of the system (position tracking, exit monitoring) run for
    real against a fake fill, so the whole pipeline can be validated
    before a single real order is ever placed.
    """
    if quantity <= 0:
        return OrderResult(False, None, "invalid_quantity")
    if price is None or not math.isfinite(price) or price <= 0:
        return OrderResult(False, None, "invalid_price")
    if transactiontype not in ("BUY", "SELL"):
        return OrderResult(False, None, "invalid_transactiontype")

    orderparams = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transactiontype,
        "exchange": exchange,
        "ordertype": "LIMIT",          # hard-coded — see module docstring
        "producttype": producttype,    # defaults to INTRADAY — see docstrings above
        "duration": "DAY",             # hard-coded — IOC prohibited
        "price": str(round(price, 2 if producttype == "CARRYFORWARD" else 1)),
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }

    if ordertag:
        orderparams["ordertag"] = ordertag

    if not ORDER_PLACEMENT_ENABLED:
        dry_id = f"DRYRUN-{tradingsymbol}-{transactiontype}-{int(price*100)}"
        audit.log('ANGELONE_BLOCKED', signal_id=signal_id, intent=ordertag, reason='dry_run')
        audit.log("order_dry_run", dry_run_id=dry_id, **orderparams)
        return OrderResult(True, dry_id, "dry_run_not_sent", dry_run=True)

    if not ORDER_WRITE.allow():
        audit.log('ANGELONE_BLOCKED', signal_id=signal_id, intent=ordertag, reason='rate_limited')
        audit.log("order_rejected_rate_limited", **orderparams)
        return OrderResult(False, None, "rate_limited")

    request_started = False
    try:
        SESSION.ensure()
        audit.log('ANGELONE_REQUEST', signal_id=signal_id, intent=ordertag,
                  symbol=tradingsymbol, side=transactiontype, quantity=quantity, price=price)
        request_started = True
        resp = SESSION.api.placeOrder(orderparams)
        order_id = _extract_order_id(resp)
        code = str(resp.get('errorcode') or '') if isinstance(resp, dict) else ''
        code = code if code.isalnum() and len(code) <= 16 else None
        audit.log('ANGELONE_RESPONSE', signal_id=signal_id, intent=ordertag,
                  order_id=order_id, accepted=order_id is not None, broker_code=code,
                  reason='accepted_awaiting_fill' if order_id else 'unparseable_or_rejected')
        if order_id is None:
            audit.log("order_place_unparseable_response", response_type=type(resp).__name__,
                      **orderparams)
            return OrderResult(False, None, "unparseable_broker_response", uncertain=True)
        audit.log("order_placed", order_id=order_id, **orderparams)
        return OrderResult(True, order_id, "ok")
    except Exception as exc:                       # noqa: BLE001
        audit.log('ANGELONE_RESPONSE' if request_started else 'ANGELONE_BLOCKED',
                  signal_id=signal_id, intent=ordertag, accepted=False,
                  uncertain=request_started, error_type=type(exc).__name__)
        audit.log("order_place_exception", error=type(exc).__name__, **orderparams)
        return OrderResult(False, None, "submission_exception" if request_started else "session_unavailable",
                           uncertain=request_started)


def _extract_order_id(resp) -> Optional[str]:
    """Defensive parse — smartapi-python has returned order id as a bare
    string in some versions and as resp['data']['orderid'] in others.
    Handle both; anything else is treated as unparseable, not guessed at."""
    if isinstance(resp, str) and resp:
        return resp
    if isinstance(resp, dict):
        if resp.get("status") is False:
            return None
        data = resp.get("data")
        if isinstance(data, dict):
            for key in ("orderid", "orderId", "order_id"):
                if data.get(key):
                    return str(data[key])
        if resp.get("status") is False:
            return None
    return None


def cancel_order(order_id: str, variety: str = "NORMAL") -> OrderResult:
    if not ORDER_PLACEMENT_ENABLED:
        audit.log("order_cancel_dry_run", order_id=order_id)
        return OrderResult(True, order_id, "dry_run_not_sent", dry_run=True)

    if not ORDER_WRITE.allow():
        return OrderResult(False, order_id, "rate_limited")

    try:
        SESSION.ensure()
        resp = SESSION.api.cancelOrder(order_id, variety)
        if not isinstance(resp, dict) or resp.get("status") is not True:
            return OrderResult(False, order_id, "cancel_not_confirmed", uncertain=True)
        audit.log("order_cancelled", order_id=order_id, response=str(resp)[:300])
        return OrderResult(True, order_id, "ok")
    except Exception as exc:                        # noqa: BLE001
        audit.log("order_cancel_exception", order_id=order_id, error=str(exc))
        return OrderResult(False, order_id, f"exception:{exc}")


# ---------------------------------------------------------------- status
def modify_order(order_id: str, *, price: float | None = None,
                  quantity: int | None = None, variety: str = "NORMAL",
                  contract: dict | None = None) -> OrderResult:
    """Modify price and/or quantity on a pending LIMIT/DAY order. Does NOT
    accept an order-type or duration override — Market/IOC orders remain
    prohibited for algo trading under SEBI's framework (see module docstring).

    contract: optional tradingsymbol/symboltoken/exchange/producttype for the
    order. SmartAPI's modify endpoint documents these as part of the request;
    when given they are sent with ordertype LIMIT and duration DAY forced."""
    if price is None and quantity is None:
        return OrderResult(False, order_id, "invalid_modification_no_fields")
    if price is not None and price <= 0:
        return OrderResult(False, order_id, "invalid_price")
    if quantity is not None and quantity <= 0:
        return OrderResult(False, order_id, "invalid_quantity")

    if not ORDER_PLACEMENT_ENABLED:
        audit.log("order_modify_dry_run", order_id=order_id, price=price, quantity=quantity)
        return OrderResult(True, order_id, "dry_run_not_sent", dry_run=True)

    if not ORDER_WRITE.allow():
        audit.log("order_modify_rejected_rate_limited", order_id=order_id)
        return OrderResult(False, order_id, "rate_limited")

    modparams = {"variety": variety, "orderid": order_id}
    if contract:
        for key in ("tradingsymbol", "symboltoken", "exchange", "producttype"):
            if contract.get(key) is not None:
                modparams[key] = str(contract[key])
        modparams["ordertype"] = "LIMIT"
        modparams["duration"] = "DAY"
    if price is not None:
        modparams["price"] = str(round(price, 2 if (contract or {}).get("producttype") == "CARRYFORWARD" else 1))
    if quantity is not None:
        modparams["quantity"] = str(quantity)

    try:
        SESSION.ensure()
        resp = SESSION.api.modifyOrder(modparams)
        if not isinstance(resp, dict) or resp.get("status") is not True:
            return OrderResult(False, order_id, "modify_not_confirmed", uncertain=True)
        audit.log("order_modified", order_id=order_id, response=str(resp)[:300], **modparams)
        return OrderResult(True, order_id, "ok")
    except Exception as exc:                       # noqa: BLE001
        audit.log("order_modify_exception", order_id=order_id, error=str(exc))
        return OrderResult(False, order_id, f"exception:{exc}")


def fetch_order_book() -> Optional[list[OrderStatus]]:
    """None means unavailable, distinct from a confirmed empty order book."""
    if not ORDER_STATUS.wait_and_allow(timeout_sec=3.0):
        return None
    try:
        SESSION.ensure()
        resp = SESSION.api.orderBook()
        if not isinstance(resp, dict) or resp.get("status") is not True:
            return None
        rows = resp.get("data")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            return None
        result = []
        for row in rows:
            oid = row.get("orderid") or row.get("orderId")
            if not oid:
                return None
            qty = float(row.get("filledshares") or row.get("filledQty") or 0)
            avg = float(row.get("averageprice") or 0)
            if not math.isfinite(qty) or qty < 0 or not qty.is_integer():
                return None
            if not math.isfinite(avg) or (qty > 0 and avg <= 0):
                return None
            result.append(OrderStatus(str(oid), str(row.get("orderstatus") or
                row.get("status") or "unknown").lower(), int(qty), avg or None, row))
        return result
    except Exception as exc:
        audit.log("order_book_unavailable", error=type(exc).__name__)
        return None


def get_order_book() -> list[OrderStatus]:
    # Compatibility for the existing read-only dashboard.
    return fetch_order_book() or []


def get_order_status(order_id: str) -> Optional[OrderStatus]:
    return next((o for o in (fetch_order_book() or [])
                 if o.order_id == str(order_id)), None)
