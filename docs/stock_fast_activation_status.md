# Stock-fast activation status and source alignment

## Latest supplied runtime evidence

The user's authenticated status screenshot is timestamped
2026-09-19T16:54:46.483869+05:30. It reports global auto NO, stock live NO,
Celestial rollout NO, fast master NO, broker permission YES, kill latched NO,
and stock worker running NO. This is user-supplied host evidence at that time,
not a direct server query by this development session. It establishes that the
new stock-fast route is disabled; it does not characterize every legacy route.

The successful earlier installation did not activate it. run.py registers the
APIs before waiting for in_premarket/in_market_hours, which exclude weekends.
Worker NO on that Saturday is consistent with the normal startup wait when
KADIMA_FORCE_START is not set. It is not proof of a crash, and the screenshot
does not identify every runtime cause. Waiting for the next session does not
turn off permissions on: each explicit master and persistent intent is still
required. Do not use a forced-start setting to manufacture live readiness.

## Corrected activation manifest, not runtime flags

The existing tools/activate_execution.py performs an exact source check against
tools/activation_source.json before operational work. That manifest still pinned
the pre-stock-fast full_universe_scanner.py blob aca08c5c6af10e7fffbbb316eea3d039aeb41731.
The installed stock-fast scanner has blob 31b7e19c9a6283831ab8c2dd1057d2e37fb3d100.
Using the old manifest would therefore refuse this installation before activation.

The manifest now pins the reviewed stock-fast scanner and additionally includes
run.py, stock_fast.py, stock_fast_api.py, the preserved AWS feed.py and the FIFO
rate_limiter.py. No old/new wildcard acceptance is introduced. Other unexpected
file changes still fail. Deployment config.py is not replaced or pinned to a
repository default. The activation helper, runtime flags, database controls,
strategy thresholds, risk limits, kill switch and all installed source remain
unchanged by this repository-only amendment.

Eight new offline tests exercise the existing source_check function against
temporary copies and the checked-out manifest. They check old-manifest rejection,
missing/mutated stock modules, exact feed/FIFO pins and absence of operational
calls. Validation results must come from the amended commit's CI; adding these
tests is not evidence they have already run, nor proof of live readiness.

## Operator-controlled activation order

Real-money activation is a separate user action, not a GitHub merge or a GET.
First verify the already installed release and the account's current exposure,
including pending orders. The existing guarded activation helper manages server
rollout/fast permissions and requests global resume only after fresh runtime
news and reconciliation. It does not set stock_fast_controls.live_enabled.
It is not a dry run: --apply may log in, write dedicated service configuration,
restart the verified engine/scanner and enable existing automatic strategies
once runtime gates pass. Never run it as a harmless status check.

The helper's preflight and wait logic are unchanged. Outside the configured
startup window its readiness wait can end with
startup_health_not_ready_auto_remains_paused. Master configuration may have been
prepared, but global auto stays paused. A later session does not automatically
retry global resume. Do not fabricate health records, disable freshness checks,
override a source mismatch or alter stops to force activation. Master activation
can also permit existing index fast strategies; missing per-index news remains
a blocker and is not supplied by the stock-only collector.

After source/account/runtime validation, the operator can use the existing
authenticated global resume control and then Request live stock entries in the
Stock Fast Momentum panel. The stock switch requires the server rollout/fast
permissions first, and changing it is not itself a fill. Verify all four flags,
worker health, fresh coverage, reconciliation, news and the chosen holding policy
before calling the route ready. Orders remain subject to per-candidate checks.

No AWS configuration write, broker request, test order, service restart or
live activation was performed by this amendment. The submitted status remains
OFF until a later successful operator action is observed.
