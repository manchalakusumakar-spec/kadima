# NIFTY Constituent Radar

Source base: c53365deddcca84ffee4484ef8246c14dc6cc450 (main). This is not evidence of the AWS deployed version. Apply by reviewed merge/cherry-pick against the actual installed source; do not replace an AWS checkout or its session/portfolio overlays.

## Behavior

Default disabled. One existing TickFeed connection, no additional threads, broker REST calls, SDK dependencies or option-chain subscriptions. Existing four bank subscriptions are reused. Remaining NSE equity ticks are transient: they do not enter the SQLite tick writer. Existing bank/index persistence is preserved.

Per-stock state uses at most 62 five-second samples, a cumulative-volume baseline and an observed VWAP accumulator. No full-session tick history is retained. Snapshots scan only 50 small buffers. The existing forecast cadence collects the result and `/state` exposes `nifty_constituent_radar`. No dashboard HTML is changed.

Output includes 1/3/5-minute weighted observed returns in basis points, weighted positive/negative participation, sector contributions, top five positive and negative contributors, and a 1-minute versus 3-minute-average acceleration measure. None of these quantities is a calibrated probability or a forecast by itself. `probability` is null. Relative volume is explicitly unavailable until a same-time historical baseline is connected. The VWAP approximation starts at connection; the first cumulative volume is never multiplied by current LTP.

The forecast observer receives these observations as context, so enabling the module can influence its existing forecast provider. No Shield scoring, strike selection, order gate, position sizing, or stop logic is changed. Validation of forecast improvement and net trading results is outstanding.

## Required weight manifest

Set `KADIMA_NIFTY_RADAR_ENABLED=1` and `KADIMA_NIFTY_RADAR_WEIGHTS` to an operator-reviewed JSON file. Do not use synthetic test weights for live operation.

Schema:
- `source`: nonempty provenance identifying the official/licensed weight source and observation date.
- `valid_from`, `valid_until`: ISO dates, inclusive, maximum 31 days apart. Set validity to end before any scheduled constituent/corporate-action change; refresh daily where available.
- `members`: exactly 50 objects, each with `symbol` (NSE symbol without -EQ), `sector`, and `weight` (fraction, not percent).
- Unique symbols and tokens; weights sum to 1 within 0.0001. Symbols must resolve uniquely to NSE equity rows in the existing scrip master.

No bundled production weights: a verified current complete set was not available in this task. The module does not infer weights from market prices or silently use equal weights. Invalid configuration disables radar subscriptions without blocking the existing engine. Restart after updating the manifest.

## Data validity

Radar requires exchange type 1 and a valid SDK exchange timestamp in milliseconds. Missing exchange timestamps, future timestamps, delivery delay over five seconds, nonfinite prices, decreasing cumulative volume, duplicates and out-of-order events are rejected. The SDK timestamp mapping must be checked on AWS before activation. Receipt time alone cannot establish fresh exchange data.

All 50 current quotes must be fresh (five seconds) for complete coverage. Each horizon independently requires a historical sample at or before its cutoff, at most ten seconds away. Missing coverage yields null aggregate direction/return; partial contributions remain diagnostic and are not renormalized. Session rollover clears buffers; expired manifests suppress snapshots.

## Verification and rollout

Run `python -m unittest discover -s tests -v` and `python -m compileall -q kadima` on the reviewed checkout. Focused tests are offline; they do not start the engine, authenticate a broker, or place orders.

Before enabling on AWS: identify deployed commit/overlays, supply verified weights, validate exchange timestamp fields, measure baseline CPU/RSS/feed delay, then compare with the radar enabled during an observation session. Check fresh coverage and sample warmup in `/state`. Forecast accuracy needs held-out comparison against the existing forecaster; this patch does not claim an improvement or automatic training. No zero-load guarantee.

Rollback: disable `KADIMA_NIFTY_RADAR_ENABLED` and restart through the existing operator procedure. No persistent portfolio or broker state is modified by the radar.

## Rest-of-day forecasting target (user update, 7 October)

Requested target: 90% accuracy through the rest of the trading day, including movement timing. This is an unproven acceptance target, not an achieved result. An optional rest-of-day mode now extends the existing provider through session close for NIFTY only. It reuses the existing request cadence and outcome sampler.

A subsequent forecast model must emit timestamped, immutable predictions with explicit windows (for example 15-minute IST intervals), direction, predicted price range, and expected turning windows through market close. Preserve original forecasts alongside revisions. Evaluate direction, range containment/width, and timing separately; define a joint hit only when all predeclared conditions pass. Report abstention coverage, eligible windows, and confidence intervals; do not inflate accuracy through huge ranges, retrospective edits, or counting correlated overlapping forecasts as independent evidence. Timing tolerance and price-error tolerance must be fixed before evaluation. Compare against existing and simple no-change baselines on held-out sessions across trend, range, event and expiry regimes. Do not label a pressure score as a 90% probability.

This patch supplies constituent context, provider-backed rest-of-day forecast generation, and descriptive forecast-versus-outcome scoring. Calibrated predictive skill, exact turning-time evaluation, held-out validation data, and demonstrated 90% performance remain outstanding.


### Rest-of-day configuration and scoring

In the existing private forecaster settings, set `horizon_kind` to `rest_of_day` alongside the already documented enabled/model/API-key settings. Omit it to preserve one-hour operation. No new API connection or extra request cadence is created. The longer response can consume more model tokens/cost than the one-hour response; the existing 12,000 output-token cap and one-attempt-per-cycle behavior remain. Only NIFTY receives up to 75 five-minute checkpoints, including the exact close (final offset can be fractional minutes). No requests start within the last two minutes. This remains observation-only, with no orders or stop overrides.

Reports include timestamped `movement_windows` with expected endpoint direction and low/expected/high endpoint scenarios. These are windows, not promises of the precise minute of a high or low. Forecasts are recorded in the existing SQLite journal before publication, with revisions stored as new runs. Earlier targets cannot be replaced by later predictions.

`rest_of_day` status metrics cover the latest 100 rest-of-day runs (full history remains stored): adjacent-window direction accuracy, endpoint range containment, and a joint window hit. Initial evaluation definitions are a 5-basis-point neutral deadband and maximum 100-basis-point total range width for a joint hit; these are transparent engineering defaults, not empirically optimized thresholds. A joint hit requires correct adjacent endpoint direction and range containment with an acceptable width. Each endpoint uses the first recorded sample within 30 seconds of its target; missing adjacent samples remain unscored. Coverage is reported, and exact turning-time accuracy remains null. Overlapping forecasts are dependent, so descriptive percentages do not establish the 90% held-out target.

Offline verification: 14 focused tests pass, including exact-close grids, legacy one-hour compatibility, provider payload construction without network calls, immutable journal entries, first-sample wins, missing/late samples and broad-range penalties. No paid model call, AWS deployment, or live accuracy test was performed.
