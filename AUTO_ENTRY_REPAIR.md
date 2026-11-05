# Auto-entry repair and rollout

Base reviewed: main c53365deddcca84ffee4484ef8246c14dc6cc450.

This change does not establish the cause of the reported absence of automatic
trades on 28 October 2026. EC2 audit records, broker books, deployed SHA and live
configuration for that session remain necessary.

## Changes

- Keep Gamma signal journaling and alerts, but dispatch orders through the
  candidate route only when Celestial is enabled. Legacy mode retains dispatch.
- Add explicit `scope: "indices"` to the authenticated news ingestion route.
  Omitted scope remains `stocks`. Index health uses `news_index`; stock health
  uses `news`. A stock refresh cannot erase index coverage. Each candidate still
  needs explicit symbol coverage, a successful check less than five minutes old,
  and no blocking risk event. The supplied NSE company RSS collector does not
  supply index coverage.
- Surface both news health records. Resume accepts fresh explicit coverage in
  either scope, while per-candidate checks continue to reject uncovered symbols.
- Record changed Celestial readiness states with global pause/kill controls and
  candidate blocked reasons. Missing fresh candidates are recorded separately.

## Required before activation

Connect a trusted index news producer to authenticated POST /api/kadima/news.
Submit events, source, observed_at (timezone-aware), covered_symbols (exact
index names), and scope=indices. Empty events is valid only after a successful
actual source check. Do not submit an empty batch merely to arm trading.
Publish successful coverage at least within the existing 300-second freshness
window. Failed collection must not renew coverage. Review source scope for
macro, exchange and relevant index risks before approving activation.

Verify server rollout flag and saved auto_trade_enabled state; review any
latched kill, broker reconciliation, funding and candidate rejection reasons.
If KADIMA_SESSION_AUTOTRADE=1, repair/verify the scheduler's renewable permit;
the 150-second expiry is retained. Do not disable this guard to restore entries.

No live settings, positions, orders, risk thresholds or permit lifetimes are
changed by this PR. Production deployment and activation require review.

## Validation

`python -m unittest discover -s tests -p test_auto_entry_repairs.py -v`
or, after placing the test in tools:
`python tools/test_auto_entry_repairs.py`

Ten offline regression tests cover scoped news isolation, missing/stale coverage,
index risk events, news API scope forwarding/auth invocation, duplicate dispatch,
readiness logging, paused default and expired permits. Broker/network calls are
mocked. Extracted dispatch/readiness/API functions are exercised independently;
this is not a full-process or broker integration test. Changed modules compile.
