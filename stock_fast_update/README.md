# Stock-fast additive installation

The September 19 host evidence now resolves the previously unknown entrypoint:
`run.py` has base blob `b27d03215ea7ba7e4fe6e73b1052da208beea8d2`, the full-universe
scanner has base blob `aca08c5c6af10e7fffbbb316eea3d039aeb41731`, and the FIFO rate
limiter has blob `9886d951be6997e0ddf8fdda640529f5f751a25c`. The new stock endpoint
returns 404; global rollout and auto intent are off. Broker/news health is
NOT_PUBLISHED, not an authenticated broker failure. A passive count of zero is
not proof that the broker is flat.

`stock_fast_update/install.py` supplies the previously missing additive install
path for this known paused deployment. It is not the older whole-release
`executor_fix/install.py` entry point. The latter is loaded only as a pinned
helper for interpreter validation and the existing broker preflight.

## Modes

Run from a separate, reviewed release directory. The installer must not be run
from the live target directory. It requires root for process inventory and file
ownership preservation; child SDK/import checks run as the verified ubuntu
engine owner using the engine's existing Python interpreter.

```sh
# Source, service and local-control plan; no broker login or service restart.
sudo -n python3 -I -B -S stock_fast_update/install.py --with-dashboard

# Add a current broker-position/order-book check, still no installation.
sudo -n python3 -I -B -S stock_fast_update/install.py --broker-check --with-dashboard

# Operator-directed installation, conditional on all checks passing.
sudo -n python3 -I -B -S stock_fast_update/install.py --broker-check --apply --with-dashboard
```

Unlike the passive host-review tool, `--broker-check` logs into Angel One through
the installed SESSION and reads position/order books. It may refresh API
authentication and write the existing session/audit or local schema records.
It calls no place/modify/cancel/convert-position methods. Raw credentials,
responses, process environments and SDK exception messages are not printed.
Child-only order/fast permissions are disabled; no production environment file
or persistent trading permission is changed.

Do not place manual trades, edit controls, run another installer, or change
service configuration during maintenance. Broker checks are successive
observations, not an atomic account lock against other clients.

## Scope and checks

The four backend files are `run.py`, `kadima/full_universe_scanner.py`,
`kadima/stock_fast.py` and `kadima/stock_fast_api.py`. Existing base/release hashes
must match. Statically reachable local dependencies must equal the reviewed
release, with deployment-specific `config.py` preserved, syntax-checked and
fingerprinted. The FIFO limiter must match exactly and is never replaced.
Unknown/custom dependencies stop the operation; no broad allowlist is added.
This does not establish compatibility with arbitrary other CAS branches.

The engine must be the verified target run.py process. The scanner must use
`-m kadima.stock_universe_scanner` from the same target directory. With dashboard
installation, the existing gateway must run `-m uvicorn gateway:app` from
`/opt/kadima-dashboard`. Unknown launch operands fail before shutdown.

With `--with-dashboard`, the current `/var/www/html/dashboard.html` and
`/opt/kadima-dashboard/gateway.py` receive the previously reviewed additive
panel/routes. Existing content is preserved; authentication/origin/read/write
handler ASTs must match the reviewed gateway. No Nginx or dashboard credential
configuration is changed. Root-owned backups retain original bytes and metadata.
PM2, the bridge, macro service, trading thresholds and all other source files
are untouched by this installer.

Global Celestial rollout and auto intent must remain off, with the kill latch
known off. The local positions and execution-intent tables must exist and be
empty of unresolved states. Missing tables are not counted as zero. Existing
stock-live intent, if present, must be off. NOT_PUBLISHED news/reconciliation is
not an installation error by itself: a separate, fresh broker preflight is
required before shutdown and again after every target service is fully stopped.
Those checks are not replacements for runtime news/reconciliation entry gates.

Source fingerprints and local state are rechecked around shutdown. Only changed
files are backed up and atomically replaced. The installed import graph is
checked before restarting the verified services. No database is rolled back.

A failure before restart restores original source where needed and attempts to
restore service operation. After restart is attempted, source is deliberately
retained on failure because previously authorized legacy execution might have
resumed; recover only after checking service/exposure state. A failed rollback
or failed service restoration needs operator attention. Backups are under
`/var/backups/kadima-stock-fast-*` and are root-readable.

Successful postchecks require active services and an authenticated HTTP-200
stock-fast endpoint with stock-live intent off. At weekends/outside the start
window, the endpoint may be available before the worker/feed starts. This is
installation verification, not a fresh-market readiness or profitable-strategy
assertion. An HTTP-200 response is not a broker fill.

## Live operation remains separate

The feature includes a real stock-fast route into the shared executor. This
installer does not arm it. Existing master permissions, global auto intent,
stock-only live intent, fresh data/news, broker reconciliation, capital/slot
limits and protective exits remain mandatory. Opening the panel never arms it;
qualified entries need no per-trade manual confirmation after authorization.

After installation, verify actual worker/coverage freshness, broker/local books
and dashboard behavior before the operator enables live entries. The existing
portfolio can contain old authorized strategies: preserving their settings is
not the same as asserting that all older trading paths are disabled.

## Validation

39 added offline installer tests passed locally. They use temporary files,
SQLite databases and mocked service/broker callbacks; they do not contact AWS
or Angel One. Tests cover exact source guards, FIFO preservation, config
preservation, gateway auth checks, symlinks, missing/pending/unknown exposure,
second-check failure, pre-restart rollback and no rollback after restart.
Python compilation passed. The uploaded installer/test blob hashes match the
locally tested bytes. Full repository CI must be checked for this amended head.

No production installation, service restart, real broker preflight or live
activation was executed from the development session.
