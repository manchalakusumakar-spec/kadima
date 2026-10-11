"""
execution_pricing.py

Fixes signal-to-order latency slippage in KADIMA.

Problem: entry price is computed once at signal time, but by the time the
LIMIT order reaches Angel One, price has moved (e.g. 78.20 -> 99.00 on a
fast BANKNIFTY PE), and the exact-price LIMIT order never fills.

Fix:
  1. Re-fetch a fresh price immediately before submission (not the stale
     signal-time price).
  2. Use best bid/ask from the option chain instead of LTP, which can lag.
  3. Apply a small directional buffer so the LIMIT has room to fill.
  4. If unfilled after a short window, cancel and resubmit at a refreshed
     price, capped at a small number of retries so it never chases
     indefinitely.

Integrate by calling `execute_with_refresh()` from angel_orders.py instead
of placing a single static LIMIT order.
"""

import time
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Literal

logger = logging.getLogger("kadima.execution_pricing")

Side = Literal["BUY", "SELL"]


# ---------------------------------------------------------------------------
# Config — tune these against real fill-rate data once logged (see bottom)
# ---------------------------------------------------------------------------

BUFFER_PCT = 0.015          # 1.5% buffer beyond best ask/bid
MAX_RETRIES = 3             # total attempts, including the first
FILL_WAIT_SECONDS = 4       # how long to wait for a fill before refreshing
MAX_STALENESS_SECONDS = 2.0  # reject a quote older than this before using it


@dataclass
class Quote:
    best_bid: float
    best_ask: float
    ltp: float
    timestamp: float  # epoch seconds, from the quote source itself


@dataclass
class ExecutionResult:
    success: bool
    order_id: Optional[str]
    fill_price: Optional[float]
    attempts: int
    final_limit_price: Optional[float]
    reason: str


def get_execution_price(quote: Quote, side: Side) -> float:
    """
    Compute the LIMIT price to submit, using best bid/ask (not LTP) plus a
    small directional buffer so the order has room to actually fill.

    BUY  -> price at or slightly above best ask (willing to pay up a bit)
    SELL -> price at or slightly below best bid (willing to give up a bit)
    """
    if side == "BUY":
        base = quote.best_ask if quote.best_ask > 0 else quote.ltp
        return round(base * (1 + BUFFER_PCT), 2)
    else:  # SELL
        base = quote.best_bid if quote.best_bid > 0 else quote.ltp
        return round(base * (1 - BUFFER_PCT), 2)


def execute_with_refresh(
    side: Side,
    symbol_token: str,
    qty: int,
    fetch_quote: Callable[[str], Quote],
    place_order: Callable[[str, Side, int, float], str],
    get_order_status: Callable[[str], dict],
    cancel_order: Callable[[str], None],
) -> ExecutionResult:
    """
    Place a LIMIT order using a freshly-fetched quote, and if it doesn't
    fill within FILL_WAIT_SECONDS, cancel and resubmit at a refreshed
    price. Capped at MAX_RETRIES total attempts.

    Caller supplies the actual broker-facing functions so this module has
    no direct Angel One dependency and stays testable:

      fetch_quote(symbol_token) -> Quote
          Must hit the live option chain / market data endpoint at call
          time — NOT a cached value from signal-scoring time.

      place_order(symbol_token, side, qty, limit_price) -> order_id

      get_order_status(order_id) -> dict with at least:
          {"status": "COMPLETE" | "OPEN" | "REJECTED" | "CANCELLED",
           "fill_price": float | None}

      cancel_order(order_id) -> None
    """
    attempt = 0
    last_limit_price = None

    while attempt < MAX_RETRIES:
        attempt += 1

        quote = fetch_quote(symbol_token)
        staleness = time.time() - quote.timestamp
        if staleness > MAX_STALENESS_SECONDS:
            logger.warning(
                "Quote stale by %.2fs for %s (attempt %d) — refetching skipped, aborting",
                staleness, symbol_token, attempt,
            )
            return ExecutionResult(
                success=False, order_id=None, fill_price=None,
                attempts=attempt, final_limit_price=None,
                reason="stale_quote",
            )

        limit_price = get_execution_price(quote, side)
        last_limit_price = limit_price

        logger.info(
            "Attempt %d/%d: submitting %s %s x%d @ %.2f (bid=%.2f ask=%.2f ltp=%.2f)",
            attempt, MAX_RETRIES, side, symbol_token, qty, limit_price,
            quote.best_bid, quote.best_ask, quote.ltp,
        )

        order_id = place_order(symbol_token, side, qty, limit_price)

        deadline = time.time() + FILL_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(0.5)
            status = get_order_status(order_id)
            if status["status"] == "COMPLETE":
                logger.info(
                    "Filled on attempt %d: %s @ %.2f",
                    attempt, symbol_token, status["fill_price"],
                )
                return ExecutionResult(
                    success=True, order_id=order_id,
                    fill_price=status["fill_price"], attempts=attempt,
                    final_limit_price=limit_price, reason="filled",
                )
            if status["status"] in ("REJECTED", "CANCELLED"):
                logger.warning(
                    "Order %s ended as %s on attempt %d",
                    order_id, status["status"], attempt,
                )
                break  # fall through to retry loop

        else:
            # Timed out waiting — still OPEN, cancel and retry with fresh price
            logger.info(
                "No fill within %.1fs on attempt %d — cancelling and refreshing",
                FILL_WAIT_SECONDS, attempt,
            )
            cancel_order(order_id)

    return ExecutionResult(
        success=False, order_id=None, fill_price=None,
        attempts=attempt, final_limit_price=last_limit_price,
        reason="max_retries_exhausted",
    )


# ---------------------------------------------------------------------------
# Latency measurement — wire this in separately to quantify the real gap
# between signal time and order-ack time, so BUFFER_PCT / FILL_WAIT_SECONDS
# can be tuned from data rather than guesswork.
# ---------------------------------------------------------------------------

def log_latency(signal_ts: float, order_ack_ts: float, symbol_token: str) -> None:
    latency_ms = (order_ack_ts - signal_ts) * 1000
    logger.info("LATENCY %s: %.0fms signal-to-ack", symbol_token, latency_ms)
