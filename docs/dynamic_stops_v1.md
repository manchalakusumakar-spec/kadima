# Dynamic stops: offline draft and bounded structure exception

Status: DISABLED_EXECUTION_ADAPTER. The executor and confirmed-fill ledger now
accept explicitly bound plans; rollout defaults OFF. No AWS service or broker
setting has been changed. `stops.py` preserves its legacy two-argument
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
2. Outside the explicit manual CAS no-stop policy below,
   `StopPlan.hard_stop_premium` and `StopPosition.protected_stop` always bind.
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
deadlines are required by the base draft. Known manual sources follow the
explicit manual policy below; unknown sources remain rejected.

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
- Integrate verified manual-position ownership and the approved manual policy;
  resolve the automated CAS risk sleeve
  and pre-expiry Gamma regime ambiguity from the source draft.
- Validate shorter-regime/instrument settings; do not assume NIFTY's points apply
  across all indices or that a long-horizon forecast is usable for CAS.
- Build a source-matched deployment preserving installed AWS session/portfolio
  overlays. This patch neither verifies nor activates an AWS installation.

## Source attribution and manual CAS policy (24 September 2026)

`signal.source` identifies the originating scanner, not the market's subsequent
behaviour. `StopPlan.source` and `StopPlan.regime` are saved with the exact
contract and position state. Execution integration must bind them to the actual
entry intent/order and resolve conflicting scanner claims before adoption.

| Source | Initial stop regime |
| --- | --- |
| `gamma_blast`, `gamma_pre_entry` | GAMMA |
| `scalp`, `index_scalp`, `fast_momentum` | SCALP |
| `cas_blast` | CAS |
| `bull_shield`, `bear_shield`, `sniper` | INTRADAY |
| `manual`, `manual_adopted`, `MANUAL_ADOPTED` | INTRADAY with manual CAS policy |
| Unknown / absent | Reject |

The existing pre-expiry modifier can assign automated Intraday signals below
90 to Scalp at entry. Gamma below 90 on that day still requires an explicit
configured choice. Manual positions are exempt from that modifier. Gamma
Pre-Entry can graduate to Confirmed; arbitrary later signals cannot reclassify
an existing position.

User-approved manual rule: before **15:15 IST**, use Intraday protection. From
15:15 until the position's verified hard exit, return `manual_cas_no_stop` with
`StopDecision.stop=None`. This disables technical, premium, trailing, earned
profit-floor, structure-forecast and thesis-driven strategy exits for manual
positions during CAS. No new automatic profit partial is generated in this
hold mode. Session exit, user/operator exits and already-submitted intents
remain authoritative. A pre-CAS pending exit is never silently cancelled.
The existing repository CAS start is daily, not gated to the option's expiry.
It is a strategy boundary, not a claim about exchange auction/session hours.

Full remaining premium can be lost during this no-stop window. Draft planning
therefore reserves full premium plus the configured per-unit cost before
sizing a manual plan. `initial_stop` is not a broker adoption method: an actual
manual position still needs verified ownership, fill price/quantity and a
deliberate execution-layer binding. It never resizes an existing broker trade.
An earlier broker/product deadline still exits earlier, even at/before 15:15.
Manual CAS uses that verified deadline, not the automatic CAS draft's separate
15:18:30 cap. Broker square-off, margin liquidation and user exits are outside
the stop engine's control.

State schema version 3 persists source and the manual clock/policy snapshot.
Versions 1 and 2 remain readable without granting manual CAS permission to
old unattributed positions. Deployment must reconcile/cancel old broker stops
explicitly where appropriate; this adapter does not discover or cancel external broker stops.

## Candle quality and Fibonacci candidates

`candles.py` counts each cumulative-volume increment once per token for session
VWAP, then gives that increment to each active timeframe including a new bar.
Callbacks observe the previous candle's data before the next tick's volume.
The first sample establishes a baseline; a mid-session start cannot reconstruct
earlier trades. Session VWAP is an observed-tick estimate, not a certified
exchange VWAP. Missing/decreasing volume invalidates VWAP until the next daily
reset; absent volume returns `None`, never a close-price stand-in. Older ticks
cannot alter later candles or reset a newer session. Timestamps normalize to IST.
The feed currently records receipt timestamps; this change does not recover
missing exchange timestamps or invent candles for gaps/unsampled close times.

`tools/verify_candles.py` opens an existing SQLite database read-only, selects
the requested historical window directly, and compares every OHLCV field.
Both feeds must cover every expected five-minute slot. Empty data, duplicates,
unclosed/malformed bars, broker errors and any mismatch return nonzero. End is
exclusive; supply the actual historical session/window, including special hours.
Defaults are 09:15–15:30, not an assertion that all dates/products use that close.
The verifier does not overwrite history. Repair requires a backed-up,
source-attributed rebuild from trustworthy ticks or broker candles.

`structure_levels.py` accepts normalized or SQLite closed five-minute bars,
an aware `now`, asset, spot and ATR. `fibonacci_levels` returns unqualified
23.6%, 38.2%, 50%, 61.8% and 78.6% retracements of the last three confirmed
alternating swing legs. The 50% midpoint is conventional, not Fibonacci-derived.
Each two-left/two-right swing becomes available only when the second right bar
closes. Future/forming bars are ignored; duplicate, unordered, gapped, invalid
or stale current-session history is rejected. No historical day/week pivots or
volume proxy for a cash index are fabricated.

`build_zones` creates zones around confirmed pivots, with half-width
`max(1 point, 0.1 * ATR)` as an **unvalidated research default**. Fibonacci
overlap is corroboration only; a Fib line cannot qualify a zone alone. A
completed rejection must occur after every contributing reference was available;
consecutive touching bars count once. A later close beyond the zone invalidates
it. Output includes stable identity, formation time, actual source-bar close,
last reaction, reference provenance and `experimental=True`. The guard's
existing proximity/breach buffers and independent forecast-validation gate
remain unchanged. This pure builder is not yet connected to the live executor.

Custom candidates are preserved exactly: 18, 33, 44, 55, 66, 77, 88, 103, 108,
122, 133, 135, 155. They are disabled; units/anchor and held-out evidence are
required before interpreting them as premium prices, index offsets or periods.
Compare them against round-number and shifted-level controls, with spread,
costs, expiry and volatility segmentation. No historical edge is established.
The available year-long CSV could not be materialized/read; a single older
option-chain snapshot cannot validate intraday reversal paths or 60-second
forecast probabilities.


## Execution integration and release status

`KADIMA_REGIME_STOPS_ENABLED=1` permits new bindings and enables the agreed
manual policy for verified, managed NFO/BFO positions. It is OFF by default.
Do not enable it merely because the unit tests pass. It changes manual CAS
exit behavior immediately, even when no new regime plan has been bound.
Unbound manual positions retain the existing Intraday protection before CAS.
Existing persisted bindings continue to be evaluated if the flag is later
turned off; rollback must account for these positions first.

The internal `AutoExecutor.publish_stop_context` / `bind_stop_plan` boundary
requires reconciled ownership, final confirmed fills, an exact entry-time,
price, source and contract match, verified tick size and a future product/session
deadline. It never derives a probability from scanner scores. Binding preserves
previously earned protection. Plan source is immutable. Actual pending orders
block binding; unknown sources and fractional lots remain rejected.

Bound positions bypass legacy price/VWAP/thesis exits. Operator/session exits
remain authoritative. Manual CAS also bypasses the strategy daily-loss exit,
as explicitly requested; this does not prevent broker liquidation. Dashboard
stop tightening updates the persisted active and protected stops, and is
rejected during manual CAS. Legacy structure refresh cannot overwrite a bound
plan. A persisted-state error blocks new entries and audits the affected position
without silently reverting it to a conflicting legacy stop.

The ledger persists the order link before transport and commits cumulative fills,
position quantities and stop state together. Repeated broker snapshots cannot
subtract quantity twice. Rejected and partially cancelled exits stay latched;
remaining quantity is retried only after a terminal broker status. A full exit
behind an outstanding partial requests cancellation once and waits for its final
fill cursor. Cancellation acknowledgement is not proof of cancellation. An
ambiguous cancellation requires broker reconciliation/operator investigation.
Fresh matching bid/ask and the persisted tick size are required for a LIMIT sell;
stale depth cannot be made fresh merely by receiving an LTP. Decisions continue
on the one-second monitor even without a quote. No automatic market-order
fallback or cancel/reprice loop has been added.

Still required before live release:

- A production adapter must supply and bind plans after confirmed fills, with
  measured ATR/Greeks, source tags, tick sizes and verified session deadlines.
  Current scanner entry paths do not automatically call the new binding API.
- Wire fresh underlying context, completed bars and structure zones; validate
  the short-horizon recovery probability out of sample before populating
  `STRUCTURE_GUARD_VALIDATIONS`. The >=65% exception remains unavailable while
  that registry is empty. Historical reversal profitability is unverified.
- Reconcile any external stops and verify cancellation/repricing behavior in a
  broker sandbox; an unfilled LIMIT order does not guarantee a timely exit.
- Inventory the actual AWS source and configuration, compose against installed
  session/portfolio overlays, rehearse flat-account installation and rollback,
  then observe the deployed service and broker reconciliation.

Run the following from a separate checkout of this PR on AWS to obtain the
read-only inventory. It prints selected service flags and file hashes, not
credentials; it does not install, enable trading or prove broker flatness:

```sh
sudo -n python3 tools/deployment_report.py --target /home/ubuntu/kadima-py
```

The session installer retains an exact hash allowlist including this executor;
its composition tests preserve exit methods. The older MCX installer still uses
its separately pinned executor: running it does not deploy the new stop adapter.
Its dependency set includes the stop modules used by shared ledger reconciliation.
Do not run an old installer as a substitute for a source-matched stop release.
