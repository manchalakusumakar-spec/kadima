# No-trade investigation: 17–18 September 2026

The deployment of the dashboard/news updates does not prove the trading engine
can enter an order. The current investigation has not yet obtained both days'
server audit logs. Do not claim a historical root cause from today's dashboard.

## Verified source findings

- Shield requires score 65 plus OI, freshness, technical alignment, consecutive
  closed-bar confirmation, and Bank Nifty bank confirmation where applicable.
- Legacy Gamma requires a confirmed Gamma signal. A score alone is insufficient
  without its direction/tier requirements.
- The experimental fast momentum lane defaults to observation. Celestial rollout,
  auto controls and execution-news coverage are separate gates.
- Public RSS headlines in the dashboard are not execution-news coverage.
- Legacy reconciliation never published its successful or failed status to the
  Celestial health table. The dashboard could therefore show unverified despite
  successful legacy reconciliation. This release publishes the actual result
  and reason in both modes; it does not change entry eligibility.
- The old session reporter excluded confirmation checks and pre-dispatch blocks.
  The updated report includes those failures, per-underlying maximum Shield
  scores, fast-lane blocked reasons, and reconciliation errors. It also reads
  saved controls and local order counts with SQLite read-only mode.

## Obtain evidence without deploying engine changes

On the EC2 host, from the existing checkout:

```bash
set -euo pipefail
cd ~/kadima-py
git fetch origin codex/live-inputs-score-probability
kadima_report=$(mktemp /tmp/kadima-execution-report-XXXXXX.py)
git show FETCH_HEAD:tools/review_execution_session.py > "$kadima_report"
sudo python3 "$kadima_report" --date 2026-09-17
sudo python3 "$kadima_report" --date 2026-09-18
```

Use `--audit-dir` and `--db` if the service uses a nondefault data directory.
The report does not import/start the engine, read credentials, send broker
requests, change controls, or restart services. Missing logs/tables are reported
as unavailable, not as zero trades. Saved controls are current, not a historical
record of the service environment. Multiple audit events can describe one
attempt; reason counts are event counts, not distinct failed trades.

The reconciliation code fix still requires deployment of the reviewed engine
file. Running the report does not deploy it. Do not replace an installed engine
file until its source version and the release's compatibility have been checked.
