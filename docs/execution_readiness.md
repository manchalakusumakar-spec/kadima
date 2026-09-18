# Diagnose execution on the running AWS server

The backend installer previously omitted `tools/deployment_report.py`. A missing
report is a packaging defect; it does not prove that the engine or auto trading
is off. The installer now includes the report, accepting only absence, its exact
reviewed previous version, or the new release version.

The report also runs as a standalone Python file, using only the standard
library. It does not need the release manifest, broker SDK, or package imports.
This lets an existing deployment be inspected without deploying the backend,
stopping services, logging into the broker, or changing a trading setting.

## Run without changing the installed engine

Fetch this repair branch from the existing authenticated repository, extract
`tools/deployment_report.py` with `git show <reviewed-commit>:tools/deployment_report.py`
into a temporary file, then run:

```bash
sudo -n python3 /path/to/extracted/deployment_report.py --target /home/ubuntu/kadima-py
```

Use the reviewed full commit ID when extracting the file. Do not run the full
backend installer just to obtain a diagnostic. The report reads the configured
dashboard API token internally and prints only selected status fields; do not
paste credentials or environment files into chat.

## Interpret the result

- `checks.health` describes the local engine endpoint, not order readiness.
- `checks.rollout_enabled`, `auto_trade_enabled`, `broker_orders_enabled`, and
  `kill_latched` come from the authenticated running control API.
- `checks.fast_momentum_live_requested` and `cas_live_requested` read the running
  process environment. A true value is only an opt-in, not proof the strategy is
  deployed or that its entry checks pass.
- `celestial_control_blockers` lists false/unknown switches and missing or stale
  reconciliation/news health. These describe the Celestial path; disabled
  Celestial rollout does not prove that legacy Shield execution is disabled.
- News age is checked against 300 seconds, reconciliation against 120 seconds
  for diagnostics. The actual executor's checks remain authoritative.
- The local database is opened read-only to count positions and pending intents.
  Counts do not replace broker reconciliation or identify a valid entry.
- A report copied alone or run from the target checkout says
  `source_comparison: unavailable_inventory_only`; it cannot independently
  certify the deployed source. Compare reported hashes with a reviewed release.
- The report never calls resume/reset/kill/order endpoints or automatically
  changes settings. An empty blocker list does not validate current quotes,
  signal eligibility, capital limits, broker acceptance, or fills.

The next execution repair must be chosen from the actual live result. Preserve
the kill latch, exposure checks, data-freshness checks and existing risk limits;
do not bypass a blocker merely to force an order.
