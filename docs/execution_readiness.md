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

## Confirmed AWS configuration, 18 September 2026 at 09:00 IST

The user's authenticated report showed all four systemd services running,
broker orders permitted, threshold 65, no kill latch and no local positions or
pending execution intents. Celestial rollout and stored auto trading were off;
the fast-entry opt-in was off; CAS modules were absent. Missing Celestial news
and reconciliation health did not by itself demonstrate a broker outage. The
six reported installed engine/entry/control/transport/Shield/fast file hashes
matched the reviewed release.

`tools/activate_execution.py --apply --enable-fast` is a user-run activation for
this installed release. Extract it together with `tools/activation_source.json`,
`tools/deployment_report.py` and `executor_fix/install.py` from the same reviewed
commit. It does not run the installer or replace the engine source.

The helper checks source identity and existing paused/65/broker-enabled/no-kill
controls, validates process ownership and the shared data directory, and uses
the existing read-only broker/local exposure preflight before and after stopping
the engine and scanner. It creates dedicated systemd EnvironmentFile drop-ins
for those two services, enables Celestial rollout and the requested fast opt-in,
restarts, verifies running process flags, then requests the existing authenticated
resume endpoint only after fresh reconciliation and news health. It never
resets a kill latch, changes capital/stop settings or submits a test order.

Failures before restart remove only newly created activation files and restore
service operation. After restart, the mode is retained and live status must be
reviewed; rolling mode back under possible exposure is not attempted. Startup
readiness waits at most roughly one minute plus in-flight request time. A later
retry with the desired flags already running checks exposure and readiness
without restarting the news collector again. If auto is already on, the helper
refuses a restart and prints the current diagnostic.

### Remaining index-news gap

The installed NSE news collector covers stock symbols. Its success does not
prove coverage of NIFTY, BANKNIFTY, MIDCPNIFTY or SENSEX; their per-entry checks
still require explicit fresh index coverage. The helper reports this gap after
global activation. It does not fabricate index coverage, ingest empty synthetic
news batches, relax freshness, or claim that the fast lane can trade with missing
index news. A real index-news integration remains outstanding. CAS requires its
separate release and opt-in, and is not installed or activated by this helper.
