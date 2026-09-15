"""
capital_guard.py — dynamic capital ceiling for KADIMA auto_executor.

Enforces a total-deployed-capital limit that the executor cannot exceed,
and sizes down or rejects entries accordingly.

CHANGED FROM ORIGINAL (2026-08-28 patch):
  The ceiling was a fixed MAX_DEPLOYED_CAPITAL_INR = 15,000 constant.
  It is now DYNAMIC: ceiling = starting_capital + realized_pnl_to_date,
  so profitable trades compound into larger allowed deployment on
  subsequent trades, and losing trades shrink it. Realized P&L is
  persisted to disk so it survives process restarts and carries across
  trading days (consistent with the lifetime 90-trade counter's
  no-daily-reset behavior).

DESIGN NOTES — read before deploying (ORIGINAL, still true):

1. CUMULATIVE, NOT PER-TRADE.
   A per-trade cap is not a capital cap. With MAX_CONCURRENT_POSITIONS
   removed, several trades each individually "under the ceiling" can
   still sum past it. This checks the sum of all open positions plus
   the proposed new one.

2. LOT SIZE MUST COME FROM THE INSTRUMENT, NOT A CONSTANT.
   Index lot sizes change (NIFTY has been revised more than once), and
   they differ per underlying — NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY,
   BANKEX, and every F&O stock have different lot sizes. Hardcoding one
   number is how a "1 lot" order silently becomes 3x the intended rupee
   exposure.
   >>> get_lot_size() below is STILL a stub. Wire it to instrument_master.py
   >>> before deploying this file — see the TODO in that function. This
   >>> was true in the original file and remains unresolved; it is NOT
   >>> something this patch fixed, because the instrument_master.py
   >>> interface hasn't been reviewed yet.

3. BROKER MARGIN IS THE SECOND GATE, NOT THE FIRST.
   Available funds from the broker is a separate check from your own
   self-imposed ceiling. You want BOTH: your ceiling stops you deploying
   more than you decided to risk; the margin check stops rejected orders.
   If the broker call fails, this FAILS CLOSED (rejects the entry) rather
   than assuming funds are available.

4. THIS CAPS DEPLOYMENT, NOT LOSS.
   Capital deployed in long options can go to near-zero. This limits how
   much is exposed, not how much can be lost. Options bought can lose
   effectively their full premium. Treat the whole deployed amount as
   at risk.

5. NEW: THE CEILING NOW MOVES WITH REALIZED P&L.
   Because the ceiling compounds upward on wins, a losing streak after a
   winning streak draws down from a LARGER base than the original fixed
   15,000. There is no separate cap on how far the ceiling itself can
   fall — it mechanically tracks starting_capital + realized_pnl, which
   can go negative in principle if realized losses exceed starting
   capital. If daily loss limit enforcement is ever re-enabled elsewhere
   in the codebase, confirm it interacts sensibly with a shrinking ceiling.
"""

from __future__ import annotations

import json
import math
import os
import threading

from . import audit
from . import config
from . import instrument_master
from .session import SESSION


# --------------------------------------------------------------------- config
# Move these into config.py to keep thresholds centralised.
STARTING_CAPITAL_INR = 100_000.0       # base capital before any realized P&L
CAPITAL_SAFETY_BUFFER_PCT = 5.0       # leave headroom for slippage on MARKET orders
REQUIRE_BROKER_MARGIN_CHECK = True    # fail closed if broker margin unreadable

CAPITAL_STATE_PATH = os.path.expanduser("~/kadima-py/state/capital_state.json")


# --------------------------------------------------------------- persisted P&L
class _CapitalState:
    """
    Persists realized_pnl to disk so the dynamic ceiling survives process
    restarts and carries across trading days/sessions — same durability
    requirement as the lifetime trade counter.
    """

    def __init__(self, path: str = CAPITAL_STATE_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._realized_pnl = self._load()

    def _load(self) -> float:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                return float(data.get("realized_pnl", 0.0))
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                audit.log("capital_guard_state_load_failed", error=str(exc))
                return 0.0
        return 0.0

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump({"realized_pnl": self._realized_pnl}, f, indent=2)
        os.replace(tmp_path, self._path)  # atomic write

    def record_realized_pnl(self, delta: float) -> float:
        """Call on every trade close with that trade's realized P&L
        (positive or negative). Returns the new cumulative total."""
        with self._lock:
            self._realized_pnl += delta
            self._persist()
            return self._realized_pnl

    def realized_pnl(self) -> float:
        with self._lock:
            return self._realized_pnl

    def manual_adjust(self, new_realized_pnl: float) -> None:
        """Emergency manual override — e.g. reconciling against actual
        broker statement. Not called automatically by the engine."""
        with self._lock:
            self._realized_pnl = new_realized_pnl
            self._persist()
            audit.log("capital_guard_manual_pnl_adjust", new_value=new_realized_pnl)


_STATE = _CapitalState()


def record_realized_pnl(delta: float) -> float:
    """
    Call this from exit_manager.py / auto_executor.py whenever a position
    closes, with that trade's realized P&L. This is what makes the ceiling
    compound — wire it in alongside wherever the trade_counter.increment()
    call happens on close.
    """
    return _STATE.record_realized_pnl(delta)


def available_equity() -> float:
    """Live available equity read from the broker every call -- NOT a
    static starting-capital constant. broker_available_funds() already
    reflects all realized P&L automatically (it is the live account
    balance), so no separate realized_pnl addition is needed here.
    FAILS CLOSED to 0.0 if the live read fails, blocking new entries
    rather than assuming capital is available."""
    funds = broker_available_funds()
    if funds is None:
        audit.log("capital_guard_equity_unavailable_failing_closed")
        return 0.0
    return funds


def _buffered_ceiling() -> float:
    """Effective ceiling after slippage headroom. MARKET orders can fill
    worse than the quoted LTP, so sizing to the exact ceiling can breach it."""
    equity = available_equity()
    return max(0.0, equity * (1 - CAPITAL_SAFETY_BUFFER_PCT / 100.0))


# --------------------------------------------------------------------- lot size
def get_lot_size(symbol: str, token: str) -> int | None:
    """
    Return the contract lot size for this instrument.

    WIRED (2026-08-28 patch): delegates to instrument_master.get_lot_size(),
    which resolves lot size from Angel One's cached scrip master, verified
    by token with a symbol cross-check. Covers NFO and BFO contracts alike
    (including BANKEX, once that index is enabled) since instrument_master's
    lot map is built from every NFO/BFO row, not filtered to stock options.

    Still fails closed exactly as before: if instrument_master cannot
    resolve the token (stale/missing cache, unknown contract, symbol
    mismatch), it returns None, and this propagates that None upward —
    entries are REJECTED rather than sized on a guess. Do not replace
    this with a hardcoded constant.
    """
    return instrument_master.get_lot_size(symbol, token)


# --------------------------------------------------------------------- exposure
def deployed_capital(positions: dict) -> float:
    """
    Rupee value currently deployed across open positions, valued at
    entry cost (what was actually paid), not marked-to-market.
    """
    total = 0.0
    for pos in positions.values():
        lot_size = get_lot_size(pos.symbol, pos.token)
        if lot_size is None:
            # Cannot value this position -> cannot safely compute headroom.
            # Signal this by returning infinity so any new entry is blocked.
            audit.log(
                "capital_guard_unvaluable_position",
                symbol=pos.symbol,
                token=pos.token,
                note="lot size unavailable; blocking new entries until resolved",
            )
            return float("inf")
        total += pos.entry_price * pos.remaining_qty
    return total


# --------------------------------------------------------------------- broker
def broker_available_funds() -> float | None:
    """
    Available cash/margin from Angel One RMS. Returns None if it cannot
    be read — caller must treat None as "do not trade", not "unlimited".

    >>> Field names in the RMS response vary by broker/API version.
    >>> Verify against an actual rmsLimit() response before relying on it.
    """
    try:
        SESSION.ensure()
        resp = SESSION.api.rmsLimit()
        if not isinstance(resp, dict) or resp.get("status") is not True:
            return None
        data = resp.get("data") or {}
        for key in ("availablecash", "availableCash", "net", "availablemargin"):
            if key in data:
                try:
                    funds = float(data[key])
                    return funds if math.isfinite(funds) and funds >= 0 else None
                except (TypeError, ValueError):
                    continue
        audit.log("capital_guard_rms_unparsed", response_keys=list(data.keys()))
        return None
    except Exception as exc:  # noqa: BLE001
        audit.log("capital_guard_rms_exception", error=str(exc))
        return None


# --------------------------------------------------------------------- sizing
def max_affordable_lots(
    entry_price: float,
    symbol: str,
    token: str,
    positions: dict,
    requested_lots: int,
) -> tuple[int, str]:
    """
    Returns (allowed_lots, reason).

    allowed_lots == 0 means the entry must be rejected outright.
    allowed_lots < requested_lots means size down to that many.

    Unchanged from original EXCEPT headroom is now computed against the
    dynamic _buffered_ceiling() (equity-based) instead of a fixed constant.
    """
    if entry_price is None or entry_price <= 0:
        return 0, "invalid_entry_price"

    lot_size = get_lot_size(symbol, token)
    if lot_size is None:
        return 0, "lot_size_unavailable_cannot_enforce_capital_cap"

    cost_per_lot = entry_price * lot_size

    already = deployed_capital(positions)
    if already == float("inf"):
        return 0, "existing_position_unvaluable"

    headroom = _buffered_ceiling() - already
    if headroom <= 0:
        return 0, f"capital_ceiling_reached_deployed_{int(already)}_equity_{int(available_equity())}"

    if cost_per_lot > headroom:
        return 0, (
            f"single_lot_cost_{int(cost_per_lot)}_exceeds_headroom_{int(headroom)}"
        )

    affordable = int(headroom // cost_per_lot)
    allowed = min(requested_lots, affordable)

    # Second gate: actual broker funds.
    if REQUIRE_BROKER_MARGIN_CHECK:
        funds = broker_available_funds()
        if funds is None:
            return 0, "broker_margin_unreadable_failing_closed"
        while allowed > 0 and (allowed * cost_per_lot) > funds:
            allowed -= 1
        if allowed == 0:
            return 0, f"insufficient_broker_funds_{int(funds)}_need_{int(cost_per_lot)}"

    if allowed < requested_lots:
        return allowed, f"sized_down_from_{requested_lots}_to_{allowed}"

    return allowed, "ok"


# --------------------------------------------------------------------- hook
def enforce(executor, ranked, direction: str, requested_lots: int):
    """
    Call this inside evaluate_and_enter(), AFTER the existing _gates_ok()
    check and AFTER entry_price is resolved, BEFORE building the order.

        allowed, why = capital_guard.enforce(self, ranked, direction, lots)
        if allowed == 0:
            return self._reject("capital_guard_" + why, ranked, direction)
        lots = allowed

    Returns (allowed_lots, reason).
    """

    q = ranked.quote
    allowed, why = max_affordable_lots(
        entry_price=q.ltp,
        symbol=q.symbol,
        token=q.token,
        positions=executor.state.positions,
        requested_lots=requested_lots,
    )
    audit.log(
        "capital_guard_decision",
        symbol=q.symbol,
        direction=direction,
        requested_lots=requested_lots,
        allowed_lots=allowed,
        reason=why,
        already_deployed=deployed_capital(executor.state.positions),
    )
    return allowed, why
def get_capital_for_signal(kind: str, tier: str) -> float:
    """Allocated trading capital, capped by available funds and the source band.
    The executor applies the 15% risk fraction separately.
    bull_shield: 25,000-30,000. gamma_blast: 50,000-62,000.
    WATCH/unknown tier or unknown kind returns 0.0."""
    BANDS = {
        "bull_shield": (25_000.0, 30_000.0),
        "gamma_blast": (50_000.0, 62_000.0),
    }
    band = BANDS.get(kind)
    if band is None or tier not in ("CONFIRMED", "ACTIVE"):
        return 0.0
    _, hi = band
    funds = broker_available_funds()
    if funds is None:
        return 0.0
    return min(funds, hi)
