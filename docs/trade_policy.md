# KADIMA entry and stop amendment

This amendment implements the approved rules in the existing central executor. It does not enable live orders, send test orders, create a forecast, or connect the separate stock scanner to order placement.

| Rule | Implementation |
|---|---|
| Gamma entry | CONFIRMED and score >=70; the gamma classifier now also uses 70. This score is not a probability. |
| Direction | CE requires spot above VWAP, fast EMA above slow EMA and RSI >50. PE requires all three inverse conditions. Missing values reject entry. |
| Freshness | Complete option-chain receipt <=30 seconds old, underlying receipt <=30 seconds old, alignment snapshot <=240 seconds old, and a separate fresh entry LTP. Chain age is checked again before submission. These are receipt times, not exchange-latency guarantees. |
| Contracts | Current instrument-master ladder/expiry, positive remaining expiry time and resolved lot size. Spread <=1.5%; existing volume, OI, delta, distance and chase-prevention gates remain. |
| Initial protection | Most recent unbroken same-session five-minute swing low for CE, swing high for PE, confirmed by two closed candles on each side. Missing/non-contiguous/stale bars reject entry. |
| Option stop | Reprice the confirmed underlying pivot using the existing Black–Scholes implementation, calibrating IV to the observed option premium and checking convergence. A missing/invalid model result rejects entry. This is a structural model estimate, not the 30-minute KADIMA forecast. |
| Dynamic protection | Confirmed improving structure can raise the premium stop. Neither the structural nor effective stop can decrease. Both long CE and long PE positions have rising premium stops. |
| Profit trailing | Activate when peak premium reaches +5%; retain 50% of peak profit, preserving any tighter prior stop. |
| Forecast condition | Only a valid matching 30-minute forecast can defer a breached profit trail. Missing, stale or invalid forecasts revert to ordinary trailing. Structural protection, daily-loss and end-of-day exits cannot be deferred by forecasts. The forecast feature flag is preserved. |
| Position slot | One position across callers of the central executor; pending and unresolved orders reserve the slot. Existing durable order intents and confirmed-fill accounting remain. |
| Restart | Underlying pivot, structural stop, effective stop and allocated capital are persisted with positions, including transactional fill updates. |

## Allocated capital and sizing

The existing source allocation caps remain: gamma <=₹62,000 and Bull Shield <=₹30,000, each capped by available broker funds. These caps are existing configuration, not newly chosen account equity. Low available funds are no longer rounded upward to the old minimum band.

Planned risk budget = **0.15 × allocated trading capital**.

For each option unit:

- Entry uses the actual submitted buffered BUY limit (currently LTP plus 2.5%, rounded as before).
- Planned stop proceeds reserve 2.5% below the structural stop for exit slippage.
- An additional 1% of purchase premium is reserved for estimated round-trip costs. This is a configurable code estimate, not a published broker fee schedule.
- Planned unit risk = entry limit − stop × 0.975 + entry limit × 0.01.

Whole lots must fit both planned risk and allocated purchase capital including the cost reserve. Sizing is repeated with the fresh entry limit immediately before submission. If one lot fails, no order is submitted. Live gaps, costs or unfilled LIMIT exits can exceed the planned risk; this is not a guaranteed maximum realised loss.

## Operational boundaries

Protective exits run in the existing approximately ten-second monitor, including the former no-auto-exit window. Stops are application-monitored, not newly created broker-hosted stop orders. Existing pending-exit alerts/reconciliation remain essential if an exit does not fill.

The installer requires a maintenance window (19:00–08:00 IST), matching source hashes, no unresolved local positions/orders, and a read-only broker confirmation that the account is flat. It backs up changed source, preserves trading flags/credentials, restarts the prior active services, and restores source on installation failure. This window is a deployment policy, not a statement of exchange hours. Database changes are additive; the database is never rolled back.

Legacy restored positions without the new fields retain the old emergency percentage fallback. The flat-only installer prevents opening the amended deployment with such a position. New entries cannot use that fallback.

**Still pending:** the F&O stock scanner is alerts-only and still shortlists 20. It needs fresh underlying candles/indicators plus a durable handoff to this same central executor before stock auto-entry can be enabled. Creating a second independent executor would violate the shared position limit. No real 30-minute forecast producer has been found or connected, so normal trailing remains the operational fallback.

## Verification

Offline unit and lifecycle tests cover gamma eligibility, CE/PE direction and pivots, missing/stale data rejection, premium mapping, allocation sizing and reserves, +5% activation, 50% retention, stop ratcheting, restart persistence, forecast fallback, pending-order slot reservations, duplicate prevention, and partial/confirmed fills. Live fills require observation in a subsequent session; no test orders were placed.
