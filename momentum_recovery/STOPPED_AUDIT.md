# Completed: stopped-service audit, price-history and learning update

The operator supplied successful output on 28 September 2026 at 00:34 IST:
21 files installed; all five services left stopped. Do not rerun the completed
apply command. The next read-only procedure is [STOPPED_READINESS.md](STOPPED_READINESS.md).

The operator's 28 September 2026, 00:13 IST result confirms that the PR19
momentum sources installed and four application services remained stopped.
The operator manages the manual carry-forward holding externally. This next
step installs the compatible audit/history/learning sources and composed
dashboard while keeping application execution stopped.

## Exact scope

`install_stopped_audit.py` stages and verifies every blob in PR24 commit
`8837cd6dc04e16123e94d8b78a3a716f83ed022c`, tree
`334e676b2750ba9d7b9feb56d5a2068904c8774a`. This release already includes
PR20. Its predecessor manifest admits the installed PR19 runtime, so there is
no intermediate PR20 install or startup. Only the reviewed backend plan and
the two composed dashboard files are installed.

The uploaded gateway/page match the previously reported AWS hashes. Their
composition preserves live news, timed projections, score-based probability,
stock controls, removed legacy panels, authentication, sessions and order-control
handlers. It adds audit, daily/minute price history and advisory learning views,
and restores the exact upstream `mcxGamma` display helper missing from the
installed page. `reviewed_dashboard/manifest.json` records all source hashes.

The installer verifies the root-private successful PR19 record, four currently
stopped services, held entry/deployment timers, inactive competing workers,
the exact PR19 entrypoint, dependency graph and current UI. It verifies the
running dashboard identity before stopping it. Sources/configuration/credentials
are fingerprinted; credentials are never printed. No fresh paused-control or
broker-flat claim is made from a stopped API or the older maintenance record.

After backups and one durable attempt marker, all five selected services must
be stopped before every replacement and rollback write. Exact installed bytes,
metadata and preserved dependencies are checked afterward. The composed
executor, socket feed, FIFO limiter, local configuration and databases are
preserved. Syntax checks do not import runtime modules or initialize databases.

## Run and inspect

Fetch the published maintenance commit into `/home/ubuntu/kadima-py` without
checking it out. Execute that commit's
`momentum_recovery/install_stopped_audit.sh`, passing the same full commit SHA.
The bootstrap fetches the exact PR24 release and invokes the new installer with
`--apply --manual-holding-external`. The Python entrypoint defaults to plan-only.
It does not call the old audit installer's main, exposure checks or restarting
transaction. The prior after-flat repair runner is not used or weakened.

Success is `audit_learning_source_installed_services_stopped`. The dashboard
gateway also remains stopped, so the dashboard will be unavailable or show a
sign-in/backend error until a reviewed startup. EC2 and SSH remain available.
No instance stop, reboot, OS package update, activation or broker action occurs.

Status and backups are retained under:

```sh
sudo -n cat /var/lib/kadima-stopped-audit-20260928/status.json
```

If any replacement fails, rollback is attempted only while all selected
services remain stopped. Any reactivation or concurrent source edit leaves
rollback unverified for review. No failure path restarts services. Do not delete
attempt markers or blindly rerun after an attempt. Preserve both maintenance
state directories and their backups.

## Remaining repair and startup work

The old historical repair requires an active process/API and its original
runtime hashes; the new audit/learning modules change some of those hashes.
It must not be run against this stopped updated checkout, and its allowlist is
not extended by this release. A separate stopped-state repair must verify the
private account-bound evidence, exact historical rows, SQLite-consistent backup,
effective service/data/account configuration and complete current broker books.
The manual NIFTY holding cannot be used to manufacture a flat-account receipt.

Only a reviewed startup can establish restored reconciliation, persistent paused
controls, authenticated audit/prices/learning APIs and a fresh learning-worker
heartbeat. The API can start before the market-session wait finishes; HTTP
availability alone does not prove the worker is running. Actual quote capture,
resource use and live activation remain pending. The other separate PRs and
design requests remain listed in the deployment inventory.

For combined CI this maintenance branch contains the unchanged PR24 runtime and
test additions, retaining the newer PR21 historical-repair implementation and
tests. The AWS installer still stages the exact pinned PR24 tree and installs
only its focused plan; it never replaces the entire live checkout.
