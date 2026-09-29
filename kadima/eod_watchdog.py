"""Legacy intraday watchdog plus CAS ownership-scoped exit requests.

CAS never uses this process as a second broker-order writer. Unconfirmed CAS
exposure returns nonzero and escalates; only the engine/ledger can sell it.
The legacy non-CAS path is unchanged and must not be confused with the newer
priority_trading owned-watchdog overlay in a separately deployed release.
"""

from __future__ import annotations

import sys

from kadima import alerts, audit
from kadima.session import SESSION
from kadima.utils import now_ist

# 2026-09-29: KADIMA's CAS trades are CARRYFORWARD. The watchdog now closes each
# position with its OWN producttype (an INTRADAY SELL against a CARRYFORWARD
# long opens a new short instead of closing it), never touches carry-forward
# positions KADIMA does not own (the trader's manual carries), and leaves
# KADIMA's CAS carry-forward positions to the engine's 15:38 EOD close unless
# it runs at/after CAS_WATCHDOG_FROM -- schedule a second run at 15:39.
CAS_WATCHDOG_FROM = (15, 39)


def _owned_cas_contracts():
    try:
        from kadima.cas_watchdog import owned_positions
        return owned_positions()
    except Exception as exc:
        audit.log('eod_watchdog_cas_ownership_unreadable',error_type=type(exc).__name__)
        return None

AGGRESSIVE_PRICE_OFFSET_PCT = 3.0   # price the exit this far from LTP, in
                                    # the direction that favours a fast
                                    # fill — this is a last-resort exit,
                                    # not a normal one; certainty beats
                                    # price optimisation here


def _fetch_positions() -> list[dict] | None:
    """Ground truth from the broker. None means unreadable — caller must
    NOT treat that as 'nothing to close', same fail-closed convention
    used throughout this codebase (capital_guard's margin check,
    auto_executor.reconcile_on_startup)."""
    try:
        SESSION.ensure()
        resp = SESSION.api.position()
    except Exception as exc:                        # noqa: BLE001
        audit.log("eod_watchdog_broker_unreadable", error=str(exc))
        return None
    if not resp or not resp.get("status"):
        audit.log("eod_watchdog_broker_bad_response", response=str(resp)[:300])
        return None
    return resp.get("data") or []


def _close_position(pos: dict) -> None:
    symbol = pos.get("tradingsymbol")
    token = pos.get("symboltoken")
    exchange = pos.get("exchange")

    try:
        netqty = int(float(pos.get("netqty", 0) or 0))
    except (TypeError, ValueError):
        netqty = 0
    if netqty == 0:
        return

    transaction_type = "SELL" if netqty > 0 else "BUY"
    quantity = abs(netqty)

    try:
        ltp_f = float(pos.get("ltp"))
    except (TypeError, ValueError):
        ltp_f = None

    if not ltp_f or ltp_f <= 0:
        # No usable LTP to price a limit order safely — alert and stop
        # rather than guess a price. A human needs to act right now;
        # a wrong guessed price could fail to fill OR fill very badly.
        audit.log("eod_watchdog_no_ltp_cannot_price", symbol=symbol, token=token)
        alerts.send(
            f"\U0001F6A8 EOD WATCHDOG: found open position {symbol} with no "
            f"usable LTP to price an exit safely. MANUAL ACTION NEEDED NOW.",
            bypass=True)
        return

    offset = AGGRESSIVE_PRICE_OFFSET_PCT / 100.0
    price = round(ltp_f * (1 - offset if transaction_type == "SELL" else 1 + offset), 1)

    orderparams = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": "LIMIT",           # hard-coded, no exception even here
        "producttype": str(pos.get("producttype") or "INTRADAY"),
        "duration": "DAY",
        "price": str(price),
        "quantity": str(quantity),
    }

    try:
        resp = SESSION.api.placeOrder(orderparams)
        audit.log("eod_watchdog_exit_order_placed", response=str(resp)[:300],
                  **orderparams)
    except Exception as exc:                        # noqa: BLE001
        audit.log("eod_watchdog_exit_order_failed", error=str(exc), **orderparams)
        alerts.send(
            f"\U0001F6A8 EOD WATCHDOG: tried to close {symbol} but the order "
            f"call FAILED ({exc}). MANUAL ACTION NEEDED NOW.", bypass=True)
        return

    alerts.send(
        f"\U0001F6A8 EOD WATCHDOG fired: closed {symbol} (qty {quantity}) still "
        f"open at {now_ist().strftime('%H:%M:%S')} IST. This means "
        f"auto_executor's own EOD close did NOT fire as expected — "
        f"investigate why before the next session.", bypass=True)


def main() -> int:
    audit.log("eod_watchdog_run_start", ts=now_ist().isoformat())
    positions = _fetch_positions()

    if positions is None:
        alerts.send(
            "\U0001F6A8 EOD WATCHDOG: could not read broker positions at all. "
            "CANNOT CONFIRM there is no overnight exposure. Check manually NOW.",
            bypass=True)
        return 1

    open_positions = [p for p in positions
                      if int(float(p.get("netqty", 0) or 0)) != 0]

    if not open_positions:
        audit.log("eod_watchdog_clean", checked=len(positions))
        return 0

    audit.log("eod_watchdog_found_open_positions", count=len(open_positions),
              symbols=[p.get("tradingsymbol") for p in open_positions])
    owned_cas = _owned_cas_contracts()
    now = now_ist()
    late = (now.hour, now.minute) >= CAS_WATCHDOG_FROM
    unresolved_cas = False
    for pos in open_positions:
        if str(pos.get("producttype") or "").upper() == "CARRYFORWARD":
            key = (str(pos.get("exchange")), str(pos.get("symboltoken")))
            if owned_cas is None:
                unresolved_cas = True
                audit.log('cas_watchdog_ownership_unknown',symbol=pos.get('tradingsymbol'))
                alerts.send('CAS watchdog ownership unavailable; check broker exposure manually.',bypass=True)
                continue
            if key not in owned_cas:
                audit.log("eod_watchdog_skip_manual_carryforward",symbol=pos.get("tradingsymbol"))
                continue
            if not late:
                audit.log("eod_watchdog_cas_left_for_engine_eod",
                          symbol=pos.get("tradingsymbol"))
                continue
            owned = owned_cas[key]
            from kadima.cas_safety import integer
            from kadima.cas_watchdog import request_exit
            if (integer(pos.get('netqty'),positive=True) != owned['quantity']
                    or pos.get('tradingsymbol') != owned['symbol']):
                audit.log('cas_watchdog_manual_quantity_collision',signal_id=owned['signal_id'])
                unresolved_cas = True
                continue
            accepted = request_exit(owned['signal_id'])
            audit.log('cas_watchdog_exit_requested',signal_id=owned['signal_id'],accepted=accepted)
            alerts.send(f"CAS watchdog: {owned['symbol']} exit request "
                        f"{'accepted; fill unconfirmed' if accepted else 'UNCONFIRMED; check engine/broker now'}.",
                        bypass=True)
            unresolved_cas = True  # An accepted request is still not a reconciled flat position.
            continue
        _close_position(pos)

    return 1 if unresolved_cas else 0


if __name__ == "__main__":
    sys.exit(main())

