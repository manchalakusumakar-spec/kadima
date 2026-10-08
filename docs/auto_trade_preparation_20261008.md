# Auto-trade preparation — 8 October 2026
Scope: prepare and verify; no trading activation, orders, service restart or AWS deployment.

## Current evidence
- Repository main: c53365deddcca84ffee4484ef8246c14dc6cc450. This is not proof of deployed source.
- This preparation branch starts at PR37 head cb99d284bf42ab1aa876f9617ff62dba4d175803.
- PR37 workflow 37725658977 ran 1343 tests: 7 failures, 2 errors, 19 skips.
  Three failures concern historical commodity clock fixtures. The remaining four failures
  and two errors concern the old commodity source pin in activation_source.json.
- This follow-up supplies deterministic clocks for those historical fixtures and pins
  commodity_watch.py to blob 769385e0d5af8dcb12ce3fc484dd1510c0dd16be.
  It retains exact source admission; it does not accept arbitrary deployed files.
- PR34/35/36 exact-head workflows also failed. Their focused test claims are not full CI proof.
- PR29 reports missing timing_recorder and blocked full-source integration tests.
  That applies to its captured CAS source; do not infer the same missing file on AWS.
- Dashboard GET returned 200; server_health returned 401 sign_in_required at approximately
  12:42 IST. No authenticated live readiness result was obtained.

## Release preparation sequence
1. Collect current deployed Git blob hashes, process entrypoint, service/drop-in identity,
   running controls and strategy opt-ins using the read-only deployment report. Use
   docs/execution_readiness.md; do not run activate_execution.py --apply for preparation.
2. Compare the actual deployment with each proposed repair. PR34 stock capacity describes
   a multi-position executor; reconcile that with the owner's one-position limit before
   including it. Do not silently change capacity.
3. Compose only repairs applicable to that baseline in an isolated branch. Reconcile
   exact source manifests after review. Never sequentially run historical installers.
   PR35 watchdog, PR36 instrument cache and PR37 commodity monitor are independent candidates,
   not proof they are integrated here.
4. Run all applicable broker-free regressions, compile and actual import checks. Resolve
   missing runtime modules from authentic source, never stubs. Require successful exact-head
   CI and review skipped/root/dashboard checks before selecting a release.
5. Produce installer plan, changed-file manifest, backup/rollback plan and source receipt.
   Do not apply while exposure, source identity or service ownership is unresolved.
6. After installation is separately performed, inspect the running source and authenticate
   diagnostics. Check complete account-bound broker positions/order book against the local
   ledger; unresolved partial fills, product mismatches and unknown orders remain blockers.
7. Verify broker session/funds, valid current contracts and lot/tick sizes; current
   spot/option/feed timestamps; execution gates; no duplicate entries; configured aggregate
   capital and one-position admission; durable partial-fill exits and stop monitoring.
   Check each requested strategy independently, including CAS lane exclusivity/product.
8. Report PREPARED / BLOCKED separately from AUTO ENABLED and per-signal eligibility.
   Preparation alone must not resume, reset KILL or submit a test order.

## Completion evidence still required
Authenticated live state; deployed source identity; chosen combined release with green CI;
installation receipt; broker/local reconciliation; funds and live feed receipts; confirmed
position/risk policy; stop/exit recovery verification. Until supplied, overall readiness
is UNVERIFIED. This file and its CI repair are preparation work, not completion of server readiness.
