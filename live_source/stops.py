"""
stops.py — fixed + trailing stoploss for KADIMA auto_executor.

FIXED STOP:    30% below entry premium (unchanged from STOPLOSS_PCT).
TRAILING STOP: 10% below the highest premium seen since entry.

CRITICAL DESIGN POINT — why trailing needs an activation threshold:

    A 10% trail applied from entry IS a 10% stoploss. At entry the peak
    equals the entry price, so the trail sits at entry * 0.90 — far
    tighter than the 30% fixed stop, which it would immediately override.
    The 30% stop would never be reached; every trade would exit on a 10%
    dip instead.

    So trailing activates only once the position is up by
    TRAILING_ACTIVATION_PCT. Below that, the 30% fixed stop governs.
    Above it, the effective stop is whichever is HIGHER.

RATCHET RULE:
    The stop only ever moves up, never down. Once raised, a falling peak
    cannot lower it.

WARNING — noise exits (your spec, Section 19):
    "Avoid crude premium-percentage trailing if it creates excessive
     noise exits."

    Option premiums routinely move 10%+ intraday without the underlying
    doing anything meaningful. A 10% trail will exit some trades that
    would have resolved profitably. That is a real cost, not a
    theoretical one. TRAILING_STOP_PCT is configurable precisely so you
    can widen it once outcome data shows whether 10% is too tight.
    Check the logged 'trail_exit' events against what the position did
    afterwards before treating 10% as settled.
"""

from __future__ import annotations

from . import audit


# ---------------------------------------------------------------- config
# Move into config.py to keep thresholds centralised.
STOPLOSS_PCT = 30.0              # fixed stop, % below entry
TRAILING_STOP_PCT = 10.0         # trail, % below peak
TRAILING_ACTIVATION_PCT = 15.0   # trailing engages only once up this much
                                 # must exceed TRAILING_STOP_PCT, else the
                                 # trail activates already below entry


def _validate_config() -> None:
    if TRAILING_ACTIVATION_PCT <= TRAILING_STOP_PCT:
        raise ValueError(
            f"TRAILING_ACTIVATION_PCT ({TRAILING_ACTIVATION_PCT}) must be "
            f"greater than TRAILING_STOP_PCT ({TRAILING_STOP_PCT}), "
            "otherwise the trailing stop activates at or below the entry "
            "price and locks in a loss on activation."
        )


_validate_config()


# ---------------------------------------------------------------- fields
# Add these to the Position dataclass in auto_executor.py:
#
#     peak_price: float = 0.0          # high-water mark since entry
#     trailing_active: bool = False
#     effective_stop: float = 0.0      # ratcheted stop actually in force
#
# and in __post_init__:
#
#     if self.peak_price == 0.0:
#         self.peak_price = self.entry_price
#     if self.effective_stop == 0.0:
#         self.effective_stop = self.stoploss_price


def update_stop(pos, ltp: float) -> tuple[float, bool]:
    """
    Update the position's peak and effective stop for the current price.

    Returns (effective_stop, stop_hit).

    Call this at the top of the per-position loop in check_exits(),
    BEFORE the existing stoploss comparison, and use the returned
    effective_stop instead of pos.stoploss_price.
    """
    # 1. Track the high-water mark.
    if ltp > pos.peak_price:
        pos.peak_price = ltp

    fixed_stop = pos.stoploss_price  # entry * (1 - STOPLOSS_PCT/100)

    # 2. Has the position gone far enough into profit to engage trailing?
    activation_price = pos.entry_price * (1 + TRAILING_ACTIVATION_PCT / 100.0)
    if not pos.trailing_active and pos.peak_price >= activation_price:
        pos.trailing_active = True
        audit.log(
            "trailing_stop_activated",
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            peak_price=pos.peak_price,
            activation_price=round(activation_price, 2),
        )

    # 3. Compute candidate stop.
    if pos.trailing_active:
        trail_stop = pos.peak_price * (1 - TRAILING_STOP_PCT / 100.0)
        candidate = max(fixed_stop, trail_stop)
    else:
        candidate = fixed_stop

    # 4. Ratchet: never lower an existing stop.
    if candidate > pos.effective_stop:
        previous = pos.effective_stop
        pos.effective_stop = candidate
        audit.log(
            "stop_raised",
            symbol=pos.symbol,
            previous_stop=round(previous, 2),
            new_stop=round(candidate, 2),
            peak_price=pos.peak_price,
            trailing_active=pos.trailing_active,
        )

    stop_hit = ltp <= pos.effective_stop
    return pos.effective_stop, stop_hit


# ------------------------------------------- replacement for check_exits
# Replace the existing "# 1. Stop-loss" block with:
#
#     effective_stop, stop_hit = stops.update_stop(pos, ltp)
#     if stop_hit:
#         reason = "trailing_stop_hit" if pos.trailing_active else "stoploss_hit"
#         audit.log(
#             reason,
#             symbol=pos.symbol,
#             entry_price=pos.entry_price,
#             peak_price=pos.peak_price,
#             exit_price=ltp,
#             effective_stop=round(effective_stop, 2),
#             gave_back_from_peak_pct=round(
#                 (pos.peak_price - ltp) / pos.peak_price * 100, 1
#             ),
#         )
#         actions.append(self._exit_position(pos, ltp, reason, fraction=1.0))
#         continue
#
# Note: log gave_back_from_peak_pct on every trail exit. That single
# field is what tells you later whether 10% was too tight — if trades
# keep exiting after giving back exactly ~10% and then continuing up,
# the band is the problem, not the setups.
