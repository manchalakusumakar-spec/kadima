# Dynamic stops: offline draft and bounded structure exception

Status: OFFLINE_DRAFT. No executor, broker feed, order-placement flag or AWS
service is changed by this draft. `stops.py` preserves its legacy two-argument
API; normalized draft calls require a `StopPosition` and market context.

The base draft implements the supplied Intraday, Scalp, Gamma and CAS regimes
in `regime_stops.py`. The follow-up rule agreed on 23 September 2026 adds ONE
time-limited exception per position for a technical stop near qualified support
or resistance. Pivots can qualify as corroborating structure. This does not
establish a measured improvement in trading performance.

## Scope and settings

The first configured candidate is NIFTY INTRADAY. Its structure uses completed
5-minute bars, a 14-period spot ATR supplied by the adapter, and a 1-minute
completed bar for breach confirmation. Proximity is `max(5, 0.25 * spot_ATR)`;
the buffer is `max(5, 0.30 * spot_ATR)` beyond the OUTER zone boundary. Maximum
exception duration is 60 seconds, shortened if the forecast event expires first.
Exactly 65% confidence qualifies; 64.999% does not.

Other assets and Scalp/Gamma/CAS keep ordinary regime rules. They need their own
validated short-horizon forecast and tested timing/point settings before adding
them to `STRUCTURE_GUARD`. No code derives a confidence percentage from Shield,
Gamma scores, heat, price ranges, or the model's assertion that it is validated.

## Risk and precedence

1. A pending full exit stays binding. Session deadlines and confirmed thesis
   exits remain authoritative. Pending partial-order fill cursors are never
   overwritten by a later full exit.
2. `StopPlan.hard_stop_premium` and `StopPosition.protected_stop` always bind.
   Breakeven and explicit profit-lock ratchets raise the latter. ATR trailing
   remains a technical stop; it cannot reduce a previously locked floor.
3. The nominal `active_stop` never decreases. An ACTIVE structure exception
   explicitly suspends only its immediate price-touch exit until recovery,
   confirmation of failure, loss of eligibility, or the original deadline.
4. A zone must be selected and armed on a preceding healthy option tick, while
   the option bid is above the technical stop. First selecting it on a stop
   touch is forbidden. The selected zone and buffered boundary are frozen.
5. Forecast confidence >=65% is necessary, as is positive expected incremental
   holding value after remaining costs. No further relaxation is allowed after
   the exception is used, including after recovery or restart.

By default the initial technical stop is also the hard stop. Optional entry
input `signal.hard_stop_premium` reserves extra room BEFORE sizing. It must be
at or above the regime's maximum-loss premium floor and at or below the initial
technical stop. Sizing with this explicit reserve uses
`entry - hard_stop_premium + costs_per_unit`, within the supplied rupee budget.
The existing R metric remains the initial technical-stop distance. A structure
projection outside the reserved risk is rejected; it is not clipped back into
the zone. CAS full-premium risk sizing is unchanged and does not accept this
reserve override. No capital amount is inferred from conversation history.

## Entry and tick contract

Use the existing `initial_stop(signal, chain_row, market)` then
`open_position(plan, signal_id, confirmed_quantity)` APIs. Entry `signal.asset`
must be the canonical underlying name, e.g. `NIFTY`, for structure eligibility.
All timestamps must contain a timezone. Prices and per-unit costs are rupees.
Signal and contract inputs, regime-specific ATR, expiry, and verified session
deadlines are required by the base draft. Manual/adopted positions are not
silently assigned a scanner regime.

Each `update_stop` call includes a matching token/exchange quote with `ltp`,
`bid`, `ask`, `ts`, plus market `now`, `ts`, `asset`, `spot`, and `atr_spot_5m`.
Quotes and current market context must be no older than 5 seconds. Additional
normalized context for arming:

```text
structure:
  id, asset, role (support for CE; resistance for PE), low, high, ts
  bar_seconds: 300
  reaction_at: timestamp of a confirmed reaction, no older than 1800 seconds
  confirmed_reactions: integer >= 1
  references: at least one of pivot, vwap, previous_high, previous_low,
              ema, fibonacci, oi_wall
  oi_valid: true is required if oi_wall is the only corroborating reference
option_risk:
  token, exchange, ts
  delta: signed, positive for CE / negative for PE
  gamma, vega: nonnegative; vega is per one IV percentage point
  iv_down_points: nonnegative adverse IV scenario
  theta_per_second: nonpositive premium change per elapsed second
```

Reference/zone detection is the responsibility of a future fresh-data adapter;
the draft does not manufacture those observations. The local signed delta/gamma
projection maps the technical premium stop to an approximate spot level and
stress-projects premium at the buffered boundary. Invalid, nonmonotonic or
nonfinite mappings fail closed. This approximation does not guarantee a future
option premium. Independent premium protection remains necessary.

The optional `spot_bar` has `asset`, `ts` (close time), `seconds=60`, `closed=true`
and `close`. A completed bar after arming strictly below the CE boundary or
strictly above the PE boundary confirms failure. A wick or unfinished bar does
not. Hard premium risk can trigger before bar confirmation.

## Forecast contract and trusted validation

`market.recovery_forecast` must contain:

```text
event: option_recovery_before_exit
validation_id, model_id, asset, regime, direction, token, exchange, signal_id
level_id, boundary_spot, technical_stop_premium, hard_floor_premium
confidence_pct: finite number in [65, 100], not a 0..1 fraction
generated_at, market_data_at, outcome_deadline
recovery_target_premium: above the technical premium stop
mean_success_premium: conditional mean executable premium on success
mean_failure_premium: conditional mean executable premium over ALL failures
remaining_costs_per_unit: includes execution friction, >= plan.costs
```

Success means reaching the stated option recovery target before the defined
exit conditions and deadline. Failure includes breakdown, hard-risk breach and
timeout, not merely an unfavourable final index price. Holding advantage is
`p * mean_success_premium + (1-p) * mean_failure_premium - current_bid - costs`;
it must be positive. The outcome must be in the future and within the configured
60-second horizon. Forecast generation and input timestamps must both be no
older than 60 seconds; inputs cannot be newer than generation. Expiry or bad
auxiliary input ends an active exception. Recovery requires BOTH option bid
above the original technical stop and spot back beyond its mapped stop in the
trade's favourable direction. Recovery consumes the exception.

The trusted `config.STRUCTURE_GUARD_VALIDATIONS` registry is EMPTY by default.
An operator record keyed by `validation_id` must provide:

```text
status: approved
report_id: reference to actual independent validation evidence
model_id, event, asset, regime
max_horizon_seconds
valid_from, valid_until: timezone-aware validity window
```

It must match the forecast scope, be valid now and cover the outcome horizon.
Neither replay input nor model output can add/approve a registry record.
Test fixtures replace the registry only inside their isolated test package.
During an active exception, model/validation identity, recovery target and
outcome deadline cannot change. New forecasts cannot restart its timer.

The current checklist forecaster has no probability field. Its journal measures
index-price checkpoint errors and scenario coverage, not the first-hit option
recovery event above. There is NO validated recovery producer or calibration
report supplied with this change. Forecast override therefore remains inactive.

## Persistence, deadlines and execution

Version 2 stop state persists hard/profit floors and ARMED/ACTIVE/SPENT guard
state, original deadline and used permission. Version 1 and missing-state
restores retain their existing stop as protected and consume the permission:
unknown history cannot grant a second exception. Corrupt state is rejected.

The adapter must invoke the stop engine on a timer as well as fresh ticks.
An elapsed grace/session deadline returns an EXIT even without a usable quote;
the order adapter must subsequently obtain valid executable depth. Losing
forecast eligibility with no fresh option tick also ends active permission.
This draft does not place an order or imply a fill. LIMIT execution cannot
guarantee fills at the risk threshold.

Confirmed fills alone change remaining quantity through `acknowledge_fill`.
A full exit during a pending partial is queued; reconcile/cancel that order
before submitting the remaining full exit. The production adapter must bind
each cumulative fill to a durable intent/order ID and persist positions and
guard state atomically. The standalone SQLite helper is not that integration.

## Replay and verification

`python3 tools/replay_stops.py recorded-session.jsonl` consumes normalized
`entry`, `tick`, `thesis`, `fill`, and `legacy_exit` records. Entry records carry
`signal`, `chain_row`, `market`, optional `confirmed_quantity` and offline
`draft_regimes`/`draft_modifiers`. Tick records carry `tick` and `market`. Thesis
records carry `signals`. Every record includes `signal_id` and `event`.
Explicit fill records require `policy=DRAFT` and a matching pending reason.
Output exposes technical stop, hard/profit floors and complete guard state.
It does not infer broker fills or P&L for an alternate policy.

Synthetic tests cover the exact threshold, positive holding value, CE/PE
direction, known-before-touch level selection, one-use recovery, timeout,
confirmation, stale/mismatched/unvalidated forecasts, malformed values, hard
floors, reserved sizing, missing/corrupt/legacy state and pending fills.
Historical performance needs real timestamped spot and option bid/ask paths,
costs, slippage and ordered event outcomes. Compare untouched baseline against
the candidate on later held-out sessions, separated by asset/regime/expiry.
Check calibration, net P&L, drawdown, tail losses and surrendered profit, not
only the count of avoided stop-outs. Overlapping forecasts are not independent.

## Remaining release work

- Supply and validate the recovery forecast producer and its evidence registry.
- Integrate qualified zones, pivots, fresh option Greeks and dense quote history.
- Connect executor/ledger persistence, timer calls and stop precedence so legacy
  raw VWAP/premium exits cannot contradict the new policy for opted-in trades.
- Resolve explicit manual-position ownership/regime assignment, CAS risk sleeve
  and pre-expiry Gamma regime ambiguity from the source draft.
- Validate shorter-regime/instrument settings; do not assume NIFTY's points apply
  across all indices or that a long-horizon forecast is usable for CAS.
- Build a source-matched deployment preserving installed AWS session/portfolio
  overlays. This patch neither verifies nor activates an AWS installation.
