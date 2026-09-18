# Stock fast momentum with guarded live execution

## Release boundary

This change is based on `codex/execution-readiness-repair` at
`bde4c2952b54996b6f3be9a891df3167a00f20e7`. It adds stock execution to the
existing price-led fast strategy without changing Shield/Gamma scores, index
strategy logic, broker transport, shared risk allocations or credentials.
There is an actual call to the shared executor for an eligible stock candidate;
this is not an alerts-only implementation. Installation, a GET request, opening
the dashboard and a GitHub commit do not turn live entries on.

It does not merge the separate CAS or live-inputs branches, and is not proof
that AWS runs this source. Do not replace a newer deployment with this snapshot.
The existing release installer manifest does not automatically include these new
modules. Reconcile the installed sources and service entry points first.

## Detection and coverage

The all-root scanner publishes each complete real stock ladder into a separate
bounded stock-fast snapshot, regardless of Gamma direction or tier. The legacy
Top-20-only scanner is not extended. The full-universe scanner must actually be
running for this producer to exist.

Coverage is resolved active stock-option roots, nearest expiry, ATM plus one
strike either side (six option tokens per root). It is not every expiry or
strike. Up to 512 roots are supported; excess roots fail explicitly rather than
being silently omitted. There are no new WebSocket subscriptions and no synthetic
OHLC candles made from intermittent quotes.

Received LTP, bid, ask and cumulative traded volume are retained per contract.
A three-minute window compares the latest approximately one-minute segment
with its preceding incremental volume rate. Day changes, volume resets,
identity changes, invalid books and gaps over 90 seconds reset affected history.
A temporary invalidated ladder removes its current candidate; genuinely observed
common-token history can survive an ATM rotation until its own freshness expires.

Starting hypotheses: premium increase at least 2%, volume-rate ratio at least
1.5, and both bid and ask rising. Recent acceleration, not the day's percentage
gain, ranks preparation candidates. Real closed one-/five-minute historical
candles are fetched for those candidates outside the shared executor lock.
At most four preparations begin per worker cycle. The history cache is bounded
at 32 roots. Pending preparation counts are exposed; all-root quote observation
does not imply every candidate has completed historical preparation.

Entry planning reuses the existing breakout-and-hold structure and five-minute
context, adds stock breakout volume at least twice the five-bar baseline,
requires an accelerating liquid option with finite Greeks and a spread no wider
than 1.5%, and evaluates the selected 5/10/15-minute scenario's net reward/risk.
Model outputs are fixed-IV sensitivity scenarios, not calibrated forecasts or
confidence intervals. The displayed score is a heuristic, not a win probability.
These thresholds need replay and out-of-sample evaluation with realistic costs.

## Live path and controls

The authenticated stock-fast controls have a separate persisted `live_enabled`
intent, maximum holding time (5, 10 or 15 minutes) and optimistic revision.
Stock live intent defaults to false. The operator can request live stock entries,
pause new stock entries and select holding time from the added dashboard panel.
No per-trade manual confirmation is added once authorized settings and all gates
pass. The live request is not itself a broker order.

A stock request cannot override existing master permissions:
`KADIMA_FAST_MOMENTUM_LIVE`, `KADIMA_CELESTIAL_ENABLED`, global auto-trade intent,
broker-order permission, the kill latch, news coverage, contract validation,
reconciliation, shared position/pending-order slot, capital and daily-loss
limits remain in force. The stock request endpoint does not modify these flags.
A fast score above the threshold is not sufficient by itself.

Qualified candidates retain `strategy=fast_momentum` for the existing executor
contract, with `asset_scope=STOCK` for display and stock-only policies. Their
signal episodes are namespaced by stock, exchange, direction, breakout and
expiry. Planning and revalidation use the same selected contract and original
source timestamps. A replaced/stale snapshot, changed control revision, stop
request or failed shared gate prevents submission. Durable signal IDs and the
shared ledger retain the existing no-duplicate behavior.

Orders reuse the existing bounded LIMIT, lot-size, allocation, margin, quantity,
partial-fill and reconciliation logic. The strategy does not submit directly to
Angel One from the scanner, API GET or browser. An acknowledgement is never
reported as an owned fill by the new panel.

## Holding and protection

A per-signal maximum holding policy is persisted before dispatch. After a
submission it cannot be lengthened by changing the dropdown. Existing structural
stops, trailing protection, no-progress exits and daily priorities remain active.
The stock worker requests exit at the selected holding deadline or its session
exit-request deadline, including cancellation/exit handling for pending and
partially filled entries through the existing executor.

The stock-specific session exit-request deadline is 15:15 IST, with new entries
rejected when their selected horizon reaches that deadline. This is a strategy
buffer, not an exchange closing-time claim. Verify the account's actual product
and broker cutoff before release. It deliberately does not change legacy CAS,
Gamma or index session settings.

Pause stops new stock entries, not existing stock-position deadline checks.
Restart restores per-signal holding policies. Protection still requires a
running service, readable broker state and executable orders. Checks can be
delayed by preparation/network work, shared locks or provider outages. LIMIT
cancellation and exits are requests, not guaranteed fills. This change does not
provide broker-hosted stops, outage-proof protection or a guarantee of exiting
at exactly five/ten/fifteen minutes.

## UI, authentication and diagnostics

The existing control API gets authenticated routes:
- `GET /api/kadima/stock-fast`: intent, health, source freshness, up to 20 display
  rows and actual owned/partial quantities. No broker call and no mutation.
- `POST /api/kadima/stock-fast/config`: exact `patch` and `expected_revision`.
  It changes stock-only intent, never submits an order directly.

The additive dashboard upgrade uses the existing cookie/origin-checked gateway;
no broker or API bearer token is exposed to browser JavaScript. Reads of paused
controls invalidate cached ready displays. WATCH, ARMED, ENTRY_CANDIDATE, BLOCKED
and STALE refer to detection/preparation, not broker fills. Positions separately
show pending, partial fill and filled state from the shared executor.

Audit events include `STOCK_FAST_CANDIDATE`, `STOCK_FAST_DISPATCH_RESULT`,
`STOCK_FAST_PREPARATION_REJECTED` and producer/worker errors. Raw provider
exception bodies are not displayed as rejection reasons.

## Validation and release checklist

The 50 added targeted offline tests passed in the development runtime. They
cover window resets/rates, explicit controls, timestamp replacement, persistent
holding policies, guarded dispatch/revalidation, paused/stopped behavior,
partial/pending protection, API authentication and additive upgrade behavior.
Broker, historical feeds, shared executor and setup/scenario dependencies are
mocked in orchestration tests. They are not a full-engine integration run,
historical strategy backtest, broker fill test or evidence of profitability.
Python compilation and JavaScript syntax checks also pass.

Before live activation, require: source/service-path reconciliation; full
repository regression/CI review; real all-root and historical-data freshness;
healthy broker/local books with installation performed flat and without pending
orders; authenticated desktop/mobile dashboard smoke checks; and controlled
execution/recovery validation. Verify actual fees, lot sizes, broker cutoff and
stop/fill behavior. Retain exact blocked reasons rather than bypassing failed
gates to force the first trade.

No AWS installation, trading activation or broker order was performed during
this implementation.
