# Experimental fast-momentum lane

This change implements an additional **index-only** price-led strategy, not a
lowered Shield/Gamma gate. Existing stock/Shield/Gamma algorithms are retained.
The scanner uses the engine's existing successful option-chain polls and closed
one-/five-minute candles; it creates no new quote requests or background thread.
It is not a new low-latency option WebSocket feed and does not guarantee every move.

## Detection and planning

- Five completed one-minute bars define a range. The next completed bar breaks
  it; another holds it with no more than 0.25 prior mean candle range penetration.
- Current spot must remain through the level, no more than 1.5 mean ranges past
  it. Three completed five-minute bars must have net direction agreement.
- A real selected option needs valid identity, book, spread, OI, volume and delta
  under the existing selector. OI direction need not fire; missing liquid-chain
  evidence does not become an assumed pass.
- At least three minutes of continuous samples establishes incremental volume
  rates. The most recent >=60-second segment must gain >=2% premium and show
  >=1.5x volume rate versus the earlier segment. >90-second gaps, new identities,
  cumulative-volume resets and day changes reset the window.
- Heuristic score = 50 for qualified structure/context/liquidity plus up to 25
  each for premium momentum and volume acceleration. The existing score dropdown
  applies (65 default); this is **not a probability**.
- Stop: option-price mapping of breakout/hold underlying invalidation. Ask is
  the entry reference. T1/T2 are 1R/2R levels including the existing cost reserve.
- 5/10/15-minute scenarios use observed one-minute log drift/dispersion and
  fixed-IV repricing with elapsed time removed. Bounds are sensitivity scenarios,
  **not confidence intervals, target ETAs or validated forecasts**. The longest
  available horizon must have modelled net reward/risk >=1.5. Fees, slippage,
  model error and IV changes can invalidate this. For this experimental lane,
  horizons stop at 15:30 IST or expiry, without changing legacy session settings.

These thresholds are hypotheses for replay and out-of-sample validation, not
evidence of an edge. No claim is made that the supplied screenshots' move would
have produced a fill or profit. Historical executable bid/ask and fills were not
supplied.

## Execution and controls

Installation defaults to **observation**. Live requests require a separate
`KADIMA_FAST_MOMENTUM_LIVE=1` setting **and** existing Celestial rollout/auto flags,
score threshold, fresh news, broker reconciliation, broker-order permission,
market/cutoff, daily-loss, slot, contract, sizing and fresh-quote checks. The live
setting is not exposed as a new dashboard activation button. No settings or
credentials are changed by this patch.

The lane explicitly substitutes its confirmed one-minute structure and
short-horizon net-reward/risk test for the old five-minute pivot, time-to-expiry
projected-return gate and day-range/VWAP chase filter. Those old filters remain
unchanged for all other strategies. It reuses the existing Shield allocation,
shared executor lock, durable order intents, broker fills and exit monitor.
Entry cannot chase beyond 0.5% above its detected ask; signals are persisted by
episode and revalidated immediately before submission. Acknowledgement is not
a fill. Unknown submissions are not blindly retried.

If a fast position has not reached +5% after five minutes, it requests an exit.
Existing structural stop, profit trail, EOD/daily priorities and owned-quantity
handling remain active; reaching T1/T2 does not impose a profit cap. Exit LIMIT
requests and engine stops do not guarantee fills. On restart, position kind and
entry timestamp preserve this policy even if the fast-entry setting is disabled.

## UI and diagnostics

Existing authenticated dashboard data adds the experimental lane and execution
progress. It distinguishes blocked attempts, unconfirmed submissions,
acknowledgements, partial/full fills, terminal no-fill and dry-run. Displayed
snapshot time is not substituted for candidate/quote timestamps. In-memory
rejections reset on restart; the audit log and order ledger remain durable.
State changes log FAST_CANDIDATE; dispatched attempts log FAST_DISPATCH_RESULT.

`tools/review_execution_session.py --date YYYY-MM-DD` reads a whitelisted audit
summary without secrets or broker calls. Optional `--token <underlying token>`
replays price setups from the read-only candle database using only bars closed
at each decision time. This is **price-only replay**, not a full-strategy
backtest: historical spreads/news/volume conditions and fills are not invented.
Use `--db` and `--audit-dir` when deployment uses non-default paths.

## Release boundary

Based on PR2's reviewed Shield-confirmation head. No merge of the separate
anytime-deployment or MCX branches is implied. The guarded installer now includes
the reviewed modules and accepts exact prior reviewed hashes. Unknown deployed versions
and unresolved exposure still stop installation. Reconcile source hashes
before deployment; do not overwrite newer deployment branches with this snapshot.

Before live activation: verify installed versions; verify quote/bar timestamps
and actual scan cadence during a session; review rejection audit; replay recorded
inputs, then evaluate multiple unseen sessions with realistic costs/latency and
against a baseline. Check order/fill/partial-fill recovery and dashboard wiring.
Offline tests establish code behavior, **not strategy profitability or live
readiness**. Current work cannot finish those steps without server evidence.
# Compatibility with the installed FETCH API

The release preserves the reviewed `/state` API additions for F&O scanner
observations and read-only MCX snapshots. It includes `universe_api.py` and
`commodity_watch.py`, with their observed AWS hashes in the installer manifest.
Unknown versions still stop installation; no source check is bypassed.
These readers do not start a collector or enable commodity orders. Existing
snapshot freshness and authentication checks remain in place.

Installation is allowed 24/7. Installation still requires
broker and local flat-position/no-pending-order checks and preserves trading
flags. Installing this release does not arm the experimental strategy.
The installer checks exposure before and after stopping services. If new
exposure appears in between, it aborts and restores service operation without
replacing source. A restart or health-check failure retains the installed code
because trading may already have resumed; it requires a recovery review.
