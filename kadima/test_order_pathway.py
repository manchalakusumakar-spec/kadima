"""
test_order_pathway.py — one-time smoke test: place a real, deliberately
unfillable LIMIT order, verify Angel One actually accepted it into the
order book, then cancel it.

WHY THIS IS A SEPARATE SCRIPT, NOT A NEW TEST FRAMEWORK:
    Every step below calls the SAME functions the live engine uses for a
    real entry — angel_orders.place_limit_order(), get_order_status(),
    cancel_order(). This deliberately does NOT reimplement order
    placement; the point is to test the exact code path that will place
    real trades, not a parallel one that could behave differently and
    give false confidence.

WHY 0.05 (the minimum tick):
    Virtually guarantees no fill. A liquid near-ATM NIFTY option's LTP
    would need to crash to near-zero in the few seconds this test runs
    for a fill to occur — this does not happen in practice.

WHAT THIS DOES NOT TEST:
    Strategy logic (entry_quality, risk_budget, capital_guard, the 10%
    projected-return filter) — all bypassed on purpose. This is narrowly
    an infrastructure test: does the broker call succeed, does the order
    actually land on the exchange, does cancellation work. Strategy-level
    gates were already unit-tested separately; conflating the two here
    would muddy what a failure actually means.

Run manually, once, deliberately. This places a REAL order with REAL
(minimal) capital exposure on your live Angel One account. Requires
typed confirmation before doing anything.
"""

from __future__ import annotations

import sys
import time

from kadima import angel_orders, audit
from kadima.auto_executor import _poll_ltp
from kadima.config import INDICES
from kadima.instrument_master import get_lot_size
from kadima.scrip_master import ScripMaster
from kadima.session import SESSION



def main() -> int:
    print("=== KADIMA order-pathway smoke test ===")
    print("This will place a REAL limit order, priced 50% below the contract's")
    print("own live premium (virtually unfillable), verify Angel One accepted")
    print("it, then cancel it.")
    print()
    # CARRYFORWARD is SmartAPI's carry-forward value (used by CAS trades);
    # "NRML" is not a documented SmartAPI producttype, which likely explains
    # the 2026-08-23 rejection. Kept selectable only to reproduce that test.
    producttype = input("Product type to test [CARRYFORWARD/INTRADAY/NRML]: ").strip().upper()
    if producttype not in ("CARRYFORWARD", "INTRADAY", "NRML"):
        print(f"FAILED: '{producttype}' is not a valid choice (must be exactly")
        print("CARRYFORWARD, INTRADAY or NRML). Not silently defaulting -- rerun")
        print("and enter one exactly. Nothing was done.")
        return 1
    print(f"Testing with producttype={producttype}")
    print()
    confirm = input("Type YES to proceed: ")
    if confirm.strip() != "YES":
        print("Aborted, nothing was done.")
        return 1

    order_id = None
    try:
        print("\n[1/7] Loading scrip master...")
        scrips = ScripMaster.load()
        nifty = INDICES["NIFTY"]

        print("[2/7] Fetching live NIFTY spot price...")
        SESSION.ensure()
        spot_map = _poll_ltp(nifty.exchange, [nifty.token])
        spot = spot_map.get(nifty.token)
        if not spot:
            print("    FAILED: could not fetch spot price. Aborting -- nothing placed.")
            return 1
        print(f"    NIFTY spot: {spot}")

        print("[3/7] Resolving a near-ATM NIFTY CE contract...")
        expiry = scrips.nearest_expiry(nifty)
        if not expiry:
            print("    FAILED: no resolvable expiry. Aborting -- nothing placed.")
            return 1
        atm = round(spot / nifty.strike_step) * nifty.strike_step
        candidates = scrips.strikes_around(nifty, atm=atm, n=1, expiry=expiry)
        ce = next((c for c in candidates if c["opt_type"] == "CE"), None)
        if not ce:
            print("    FAILED: could not resolve a CE contract. Aborting -- nothing placed.")
            return 1
        lot_size = get_lot_size(ce["symbol"], ce["token"])
        if not lot_size:
            print("    FAILED: could not resolve lot size via get_lot_size(). Aborting")
            print("    -- nothing placed. (If running via sudo, check KADIMA_CACHE_DIR")
            print("    points at the real cache -- sudo changes ~ to /root, not the")
            print("    service account's home directory.)")
            return 1
        print(f"    Contract: {ce['symbol']} (token {ce['token']}, "
              f"strike {ce['strike']}, expiry {expiry}, lot size {lot_size})")

        print("[4/7] Fetching the option's own live premium (to price the test safely)...")
        opt_ltp_map = _poll_ltp(nifty.opt_exchange, [ce["token"]])
        opt_ltp = opt_ltp_map.get(ce["token"])
        if not opt_ltp:
            print("    FAILED: could not fetch the option's LTP. Aborting -- nothing placed.")
            return 1
        # Priced well below the contract's real premium -- virtually guaranteed
        # not to fill in the seconds this test takes, but realistic enough
        # relative to the actual market price to pass any exchange-side
        # price-band/sanity check. An arbitrary flat price (e.g. Rs 0.05,
        # tried first on 2026-08-21) can be far enough from reality to trip
        # exactly that kind of check and fail with an unparseable empty
        # response rather than a clean rejection -- confirmed against Angel
        # One's own SmartAPI forum, where their admin's response to this
        # identical error was "your API request must be sending some
        # incorrect data."
        test_price = round(opt_ltp * 0.5, 1)
        print(f"    Option LTP: {opt_ltp}, test price: {test_price} (50% below)")

        print(f"\n[5/7] Placing LIMIT BUY, qty {lot_size}, price Rs {test_price} "
              f"(should NOT fill)...")
        result = angel_orders.place_limit_order(
            tradingsymbol=ce["symbol"], symboltoken=ce["token"],
            exchange=nifty.opt_exchange, transactiontype="BUY",
            quantity=lot_size, price=test_price, producttype=producttype)

        if result.dry_run:
            print("    STOPPED: ORDER_PLACEMENT_ENABLED is False -- this would be a")
            print("    dry run, not a real broker test. Nothing to verify against the")
            print("    live order book. Set ORDER_PLACEMENT_ENABLED=True first if you")
            print("    intend to test the real pathway.")
            return 1

        if not result.ok:
            print(f"    FAILED: {result.reason}. Nothing was placed.")
            return 1

        order_id = result.order_id
        print(f"    Order placed. Order ID: {order_id}")

        print("\n[6/7] Waiting 3s, then confirming it actually landed in the "
              "order book...")
        time.sleep(3)
        status = angel_orders.get_order_status(order_id)
        if status is None:
            print("    WARNING: could not confirm via order book (rate-limited or")
            print("    not found yet). The order may still be genuinely placed --")
            print("    check manually in the Angel One app before assuming otherwise.")
        else:
            print(f"    Confirmed in order book. Status: {status.status}, "
                  f"filled_qty: {status.filled_qty}")
            if status.filled_qty:
                print("    UNEXPECTED: some quantity shows filled.")
                print("    This should not happen -- investigate before trusting")
                print("    anything else about today's session.")

        print(f"\n[7/7] Cancelling order {order_id}...")
        cancel_result = angel_orders.cancel_order(order_id)
        if cancel_result.ok:
            print("    Cancel request accepted.")
        else:
            print(f"    Cancel FAILED: {cancel_result.reason}")
            print(f"    MANUAL ACTION NEEDED: check the Angel One app and cancel "
                  f"order {order_id} yourself.")
            return 1

        time.sleep(2)
        final_status = angel_orders.get_order_status(order_id)
        if final_status:
            print(f"    Final status: {final_status.status}")

        print("\n=== RESULT: order placement pathway verified working. ===")
        audit.log("order_pathway_test_completed", order_id=order_id, success=True)
        return 0

    except Exception as exc:                        # noqa: BLE001
        print(f"\nUNEXPECTED ERROR: {exc}")
        if order_id:
            print(f"An order MAY still be open: {order_id}")
            print("Attempting to cancel it now as a safety measure...")
            try:
                angel_orders.cancel_order(order_id)
                print("Cancel attempted -- VERIFY MANUALLY in the Angel One app "
                      "regardless.")
            except Exception as cancel_exc:           # noqa: BLE001
                print(f"Cancel attempt also failed: {cancel_exc}")
                print(f"MANUAL ACTION NEEDED NOW: check the Angel One app for "
                      f"order {order_id}.")
        audit.log("order_pathway_test_failed", error=str(exc), order_id=order_id)
        return 1


if __name__ == "__main__":
    sys.exit(main())

