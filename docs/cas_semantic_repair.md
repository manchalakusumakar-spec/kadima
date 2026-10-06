# CAS semantic repair — source review only

Base repository: `manchalakusumakar-spec/kadima`.
Base branch: `codex/live-cas-integration`.
Exact parent: `0747318bc79556918083915f5b8863fefbcfbbbc`.
Exact parent tree: `e9f61b4716855d856eac77e75e3d838f12ddffbf`.
All 72 captured file blobs and the reconstructed tree were verified against
GitHub before editing. No deployed-source identity is inferred from this branch.

## Scope

`cas_engine.reserve()` persists `kind='cas'`; the captured `product_for()` only
recognized `cas_blast`. Both now select CARRYFORWARD for orders and existing
product-aware fill/reconciliation callers. `CAS_KIND` stays `cas_blast`: changing
it would silently switch the legacy strategy's persistence and exit policy.
No signal or position migration is performed.

Place and modify preserve two-decimal CARRYFORWARD precision, including 0.05.
CAS entry caps, BUY limits, exit limits and exit repricing share the 0.05 grid:
caps/SELL limits round down, BUY limits round up, and an exit cannot round to
zero. This covers callers that otherwise discard precision before transport.
Other products retain their previous transport rounding and exit buffers.
The configured CAS grid is not a substitute for future contract-master tick
validation, and this change is not a port of all reviewed CAS safety work.

Both scanner observations in `engine.py` remain intact. Admission is exclusive
at the execution boundaries, including a stale eligible new-lane row:

| KADIMA_CAS_EXECUTION (startup) | KADIMA_CAS_LIVE | Entry lane |
| --- | --- | --- |
| off/unset | off/unset | Neither |
| on | off/unset | Legacy `cas_blast`, subject to all other gates |
| off/unset | on | New `cas`, subject to all other gates |
| on | on | Neither: `cas_entry_lane_conflict` |

The new lane applies the shared admission check through `live_blocks()` and
the existing account checks before submission. Legacy scanner submission and
direct `enter()` both check admission; legacy entry rechecks after quote/funds
reads. Repricing checks admission/KILL again after quote reads and cancels the
remaining entry through the existing cancellation path if blocked. Cancellation
acceptance does not book a fill or mark the order terminal. Exit monitoring and
SELL repricing do not depend on lane activation. Existing KILL, session,
reconciliation, capital, daily-loss, stop/trail and time policies are retained.
No deployment, flag activation, broker login or broker order was performed.

## Source boundaries

Seven runtime files change: `angel_orders.py`, `cas_config.py`, `cas_engine.py`,
`cas_orders.py`, `cas_executor.py`, `auto_executor.py`, `execution_ledger.py`.
The shared executor/ledger changes are confined to CAS exit price rounding.
No `timing_recorder` import is removed and no missing module is supplied or
stubbed. The legacy offline fixture now explicitly opts in within its test
process because direct entry no longer bypasses the default-off flag.

The reviewed transport at safety commit
`60ef6d1c9221484c62bd184e1f2336de4e2eb973` was consulted only for its
CARRYFORWARD precision precedent. Runtime source was taken from the exact
parent above; no safety-branch overlay was silently substituted.

## Verification and separate blocker

- `python3 -I -B tools/test_cas_semantics.py`: **17 tests passed**, with mocked
  broker transport and socket connections forbidden. Coverage includes both
  kinds, BUY/SELL place and modify payloads, 0.05 prices, cap/tick direction,
  non-CAS compatibility, fresh default-off settings, conflicting flags,
  file KILL, legacy session gating, repricing control changes, exit availability,
  and product checks on broker fills.
- The initial 16-test semantic suite also ran against the unmodified exact
  parent tree and failed, demonstrating the product/precision/admission defects.
- Python compilation and `git diff --check` passed.
- `tools/test_cas_semantics_integration.py` adds seven full-source regressions
  for reservation/restoration, real ledger-to-transport wiring, new-lane price
  selection, global/file kills, engine dispatch conflicts, owned exits and
  post-quote legacy admission. **They have not executed:** import fails at
  `kadima/auto_executor.py:23` because `kadima/timing_recorder.py` is absent.
- The existing CAS executor and audit suites fail at that same import. The
  missing-source dependency task remains a separate release blocker. Other
  referenced modules (including `celestial`, `fast_momentum`, `gap_and_go`,
  `trade_audit`, `trade_learning`) are also absent from this captured tree;
  fixing the first import alone does not establish source completeness.
- CI runs the semantic checks on the PR head and retains the failing full-source
  gate. No skip, import fallback, fake missing module or green-CI claim is used.

Status: **source repair prepared; full runtime validation blocked**.
Any historically executed `kind='cas'` position that actually used INTRADAY
needs separate broker/product reconciliation; this patch does not relabel or
convert broker holdings. Deployment and activation remain outside this task.
