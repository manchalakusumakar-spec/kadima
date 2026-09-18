# Read the running engine's controls

Run the extracted script on the AWS host, with permission to read the existing
local API credential:

```bash
sudo python3 -B tools/execution_readiness.py --root /home/ubuntu/kadima-py --runtime
```

No installation or restart is required. The default report remains local-file
only. `--runtime` adds exactly two allowlisted GET requests to loopback: port
8080 `/health` and port 8081 `/api/kadima/config`. It uses the engine process's
existing API token, with the dashboard's existing configuration as a fallback.
Credentials, arbitrary response fields and raw exceptions are never printed.
There is no new broker login, order request, control change or redirect following.

`RUNNING API controls.broker_orders_enabled` is the switch reported by the loaded
API module. `rollout_enabled=false` means the legacy Shield/Gamma lane, for which
Celestial's `auto_trade_enabled=false` is not itself an entry blocker. The separate
fast lane still needs its own activation, news and execution checks. This report
does not enable either lane.

`LATEST broker reconciliation audit` shows the latest timestamped success or
failure retained since the current engine restart, together with its age. A later
failure supersedes an earlier success even if the log lines are out of order.
`startup_reconcile_complete` records that the legacy executor read both broker
books and matched them at that time. It is historical evidence, not a fresh broker
connectivity test. Missing records and old records cannot certify readiness.

Do not use an empty `/order_book` response as a successful connectivity check:
the existing display-compatible wrapper returns an empty list on read failure.
The probe deliberately does not use that route. It reports no overall PASS and
cannot prove complete entry eligibility or a live broker fill. When `market_open`
is false, stale market observations alone are not evidence of an intraday fault.

Tests mock HTTP and exercise the real report parser, endpoint restrictions,
secret redaction, strict control types and latest broker-result ordering.
