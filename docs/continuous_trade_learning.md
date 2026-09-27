# Continuous trade learning

This release adds a permanent evidence/review loop, stacked on the exact PR20
trade-audit and price-history release `d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce`.
It does not assert that past losses were caused by any particular code defect.

## Flow

1. Existing producers preserve a first proposed plan and allowlisted features.
2. Strike ranking caches the freshly polled ladder, scores, Greeks, bid/ask and
   contract identity in bounded memory. Ranking order and scores stay unchanged.
3. Before the existing broker submission, the audit adapter persists the first
   decision for that signal, including the selected contract/alternatives,
   proposed limit, plan, available features and an eligible existing AI forecast.
   No snapshot created after the first BUY fill qualifies as prospective evidence.
4. Existing quote observations retain sampled premium extrema only during owned
   exposure, after a confirmed live fill. These are not complete market extremes.
5. Confirmed fill updates enqueue reviews. An independent engine worker processes
   at most 100 signals every 30 seconds. A persisted rotating sweep recovers missed
   notifications and restarts, and revisits outcomes whose forecast horizon matured.
6. Each review separates gross win/loss/flat, open, unfilled, paper and unresolved
   states. Partial exits and unknown orders cannot become training wins/losses.
7. Forecaster context receives fresh aggregate history. Strike diagnostics include
   matching historical profile groups. The authenticated Celestial panel shows
   each trade's observations, missing evidence and forecast evaluation.

## What the engine learns

Facts and research candidates, with signal/fill references:

- Entry fill versus the recorded LTP reference; this is not executable-ask slippage.
- Target/stop evidence and gross reward-to-risk at the actual entry fill.
- Confirmed OI/volume setup associations with profitable completed trades, when
  these features actually existed in the fresh captured producer data.
- Actual fill-confirmation holding time versus the proposed maximum hold.
- Sampled profit giveback; selling at a sampled high is never assumed possible.
- Original AI forecast checkpoints versus the existing journal's actual underlying
  observations. Missing outcomes remain missing. This differs from trade profitability.

Wins and losses both contribute. Cause remains **unconfirmed** unless a separate
investigation provides causal evidence. A winning trade is not proof that all its
features helped, and a loss is not proof that all its features should be removed.
There is no fabricated historical feature reconstruction.

## Learning boundaries

Fixed groups separate strategy, underlying, CE/PE, delta band, spread band,
planned holding horizon and expiry bucket. The last 90 days / 2,000 reviewed
records form a bounded evidence window; truncation is reported. At least 30
completed prospective trades across five sessions marks a group RESEARCH_READY,
not approved for trading. Small samples stay COLLECTING.

Forecast errors are grouped by model, prompt version, asset and horizon. Duplicate
run/asset/horizon observations are counted once. Mean absolute error is compared
with the unchanged-price baseline. These traded contexts are a selected sample;
overlapping forecasts are not independent and directional counts are not a
calibrated forward probability. The prompt version changes for this release.

Broker fees are currently absent from the source ledger. Gross P&L is labelled;
net P&L remains null, and no claim of net edge or live rule promotion is allowed.
Alternative contracts are frozen for later research, but their counterfactual
fills and profits are not invented. Full alternative-path replay remains future
work requiring recorded executable quotes.

There are no automatic threshold, lot-size, stop, ranking-score or live-control
writes. Historical evidence can inform the observation-only AI forecaster and
human strike review now. Any executable adaptive policy still requires costs,
later-period validation against the old policy, drawdown/coverage evaluation,
versioning and a separately reviewed rollout. The earlier identified IV-sign,
RR-target and holding-horizon trading changes are not silently bundled here.

## Persistence and resource bounds

Additive `trade_learning_*` tables share the existing engine SQLite database;
SQLite backup of that database preserves both original audit and learning data.
There is one review and one decision per signal, one context per audit plan and
one sampled extrema row per owned signal. Review data remains durable; this
module does not prune the user's trade history. Disk growth therefore follows
trade/suggestion count and needs the host's existing disk monitoring/backups.
Selection memory is capped at 512 contract identities and 40 contracts per ladder.
No new broker requests, feeds or model calls are introduced. The worker uses the
existing forecast journal read-only. Engine exit/submission callbacks use 25 ms
SQLite busy waits and isolate capture failures; failures are counted and exposed.
A failed capture may leave evidence missing, explicitly excluding unavailable
pre-fill evidence from learning. It does not delay protection waiting for repair.

The worker is active while the engine is running, independently of the browser.
Its heartbeat and capture errors are exposed at `GET /api/kadima/learning` and the
session-authenticated `/dashboard-api/learning` proxy. Both accept date, strategy,
limit (1–200), and offset. Unauthenticated access is rejected. A heartbeat older
than 120 seconds is stale; previously saved records never imply current health.

## Rollout

No AWS deployment has been performed. Stage this release separately from the
installed checkout. Use its existing guarded `trade_audit_update/install.py`
with `--with-dashboard --broker-check` for plan/preflight, then `--apply` only
through the existing paused, source-matched, flat-account deployment procedure.
The manifest accepts exact PR20 predecessor blobs and the already-reviewed older
baselines. Unknown host changes are refused; configuration and composed executor
are preserved. The post-check requires authenticated audit, prices and learning
APIs plus a fresh learning-worker heartbeat. An unchanged install still needs a
manual authenticated heartbeat check (the base installer skips restart/post-check
when no files changed).

The old single-command rollout in PR22 is pinned to the older PR20 commit. It
will NOT install this release until its inventory/pins are deliberately updated.
Do not mix this source into a running checkout or bypass a failed exposure check.
After host install, verify a real entry's frozen decision, confirmed closed-trade
review, missing-fee labels, grouped feedback and persistence across a controlled
restart. Offline fixtures do not establish live broker integration or profitability.
