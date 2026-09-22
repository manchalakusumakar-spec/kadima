# Two-lot, ten-point index scalper

This release adds an `index_scalp` strategy to the existing shared executor.
It requests **two lots** and triggers a full exit at **broker-confirmed average
entry premium + 10 points**, for both CE and PE. Gross target rupees are
`10 × 2 × current contract lot size`. Fees, spread and slippage reduce realised
profit. An exit trigger is not a guaranteed fill or a guaranteed ten-point gain.

## Entry and protection

- Scan every resolved configured index: NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY,
  FINNIFTY, NIFTYNXT50, BANKEX and SENSEX50. A configured name is not evidence
  that an exchange currently lists a usable contract. The current instrument
  master, expiry and quote availability must validate it.
- Reuse closed one-minute breakout-and-hold candles and five-minute direction,
  plus fresh VWAP/EMA/RSI alignment. Require three minutes of option observations,
  at least 2% recent premium acceleration and 1.5× incremental-volume pace.
- Select a liquid ATM or one-step ITM contract, with the existing spread, delta,
  volume and OI quality checks. These are experimental thresholds, not calibrated
  probabilities. OI availability is a liquidity check, not a claim of directional
  OI confirmation. No predicted ten-point profit is treated as certain.
- Request exactly two whole lots using the current master. Reject if either
  risk allocation or broker capital can support only one. A partial broker fill
  is protected at its actual quantity; the system does not top it up blindly.
- Use a LIMIT entry bounded to the detected ask plus 0.50 premium points.
  Revalidate the signal and price before submission. Preserve the structural
  stop, increasing profit trail, daily-loss check, memory check, reconciliation,
  kill controls and shared one-position limit.
- Trigger exit at +10 premium points or after 15 minutes; earlier protection
  exits remain available. No new entry is accepted with less than 15 minutes
  before the configured forced exit. Target and timeout continue after the
  feature is paused and after a position is restored.
- Keep fill accounting, entry cancellation, duplicate prevention and all SELL
  orders in the existing durable execution ledger. The existing marketable
  LIMIT exit buffer applies, so a +10 trigger can realise less than +10.

The scanner uses existing index option-chain polls. In legacy mode it also polls
configured monthly indices on non-expiry days for scalp coverage, preserving
their original Gamma expiry gating. Existing Gamma/Celestial dispatch on each
index runs before that index's scalper. The shared slot can still mean choosing
one opportunity over another; this release cannot guarantee capturing every move.

## Controls and visibility

`KADIMA_INDEX_SCALP_LIVE` defaults to `0`: detection and status only. Its opt-in
is separate from `KADIMA_FAST_MOMENTUM_LIVE`. The global order switch, Celestial
rollout, auto-trade pause, score threshold and fresh-news coverage still gate
entry. This release does not change their values, arm a session timer, or enable
live execution. Existing positions retain exit protection when entry is paused.

Authenticated `/state.index_scalper` and `/dashboard_data.index_scalper` report
the mode, candidate timestamps, plans and rejection reasons. This is API
visibility; the dashboard HTML is not changed in this release.

## Release and AWS boundary

Base: `codex/memory-protection-swap-review` at
`26ac9ec16044e1275eeaa83b959d981c5fff380d`. This is a feature patch against an
unmerged branch, not a full replacement for the installed AWS checkout.

The read-only command `python3 scalper_update/review.py --root /path/to/kadima`
checks exact baseline/release hashes and dependencies. A mismatch requires
reconciling the installed overlays; do not replace the full server tree or
remove hash checks. Even a successful source review does not verify broker
exposure, service health, feed freshness or deployment.

No AWS connection or deployment workflow was available during development;
the public health probe timed out. No AWS files, services or execution settings
were changed. Runtime deployment and real-money activation are outstanding.

Before a service replacement, the operator must reconcile broker/local positions
and pending orders, preserve installed session and portfolio overlays, and use
the existing maintenance process. After installation, check source identity,
service health, fresh candidate timestamps, rejection reasons and the observation
mode in both authenticated APIs. Never roll back the target/exit code while an
`index_scalp` position or unresolved order remains.

Offline verification covers dynamic lot sizes, both directions, stale inputs,
unknown contracts, exact sizing, last-moment revalidation, partial fills, restart
recovery, duplicate exits, shared slots and existing execution protections.
Broker calls in these tests are mocked; this is not a market backtest or proof
of profitability.

Development validation: 727 of 728 Python tests passed locally. The remaining
existing private-directory permission test could not switch Unix identity in
this container (`PermissionError: Operation not permitted`). It has not been
disabled or weakened. Compilation and the release-source compatibility review
passed. The offline order rehearsal passed 66 tests with broker transport blocked.
GitHub CI status is recorded in the pull request, separately from AWS deployment.
