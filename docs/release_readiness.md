# Baseline-to-release review fixes

This release extends dashboard branch `2d994e8` over AWS baseline `6c3321b`.
It is prepared for review; installing it and validating the running AWS services
are separate steps. Do not treat passing offline tests as confirmed live readiness.

## Behavior

- Bull/Bear index Shields use a shared minimum score of 65, including exactly 65.
  Both CE and PE must still pass the confirmation hold, four-index agreement,
  direction, freshness, contract, structural-stop, capital and position-slot gates.
  Scores are not probabilities. Gamma retains its confirmed minimum of 70.
- The monitor evaluates confirmed owned quantity even while the BUY remainder is
  pending. A triggered stop or manual exit records a durable exit request before
  requesting BUY cancellation. It waits for a terminal broker order-book record,
  then submits one SELL for the final confirmed owned quantity, including fills
  received during cancellation. Price rebound and restart do not clear the request.
- No SELL is sent against an unresolved BUY quantity. Rejected cancellation or a
  missing broker response keeps the entry pending and the exit request latched;
  refresh retries cancellation and the existing alert channel calls for attention.
  An unresolved broker cancellation can still delay protection. This is not a
  broker-hosted protective stop or a guarantee of fills.
- The consolidated executor installer includes every changed backend module,
  including stock and forecast dependencies. It validates the resulting local
  import dependencies before stopping services and imports the installed backend
  before starting services. Import failure follows the source rollback path.

## Installation path

Use a separate checkout of the reviewed release. Run its
`sudo -n python3 executor_fix/install.py` on the supported Ubuntu/systemd host.
Do not run sequential historical `policy_update`, `connection_update`, or
`forecaster_update` installers from this new head: their manifests describe older
incremental versions. The dashboard gateway/appearance installation remains a
separate operation and is not overwritten by this backend installer.

The consolidated manifest accepts the exact baseline and reviewed dashboard-head
file hashes, absent newly added files, or files already equal to the new release.
Unknown local modifications stop installation for review. It does not replace
`kadima/config.py`, credentials, service definitions, dashboard files or databases.
The existing maintenance window, flat local/broker preflight, source backups,
active-service restart and source rollback rules remain in force.

Before release approval, verify the actual server's service manager and source
hashes, run the maintenance preflight, check authenticated state/control responses
and confirm that broker and local positions/orders reconcile. Existing hardcoded
session/cutoff times and live configuration still require operational verification.
Never test the manual Exit button against a real position merely as a UI check.

## Offline verification

Run `PYTHONPATH=. python3 -m unittest discover -s tests -v`.
Regression coverage includes partial BUY stops, durable manual exits, late fills,
rebound, restart, rejected cancellation, missing order books, zero fills, duplicate
SELL prevention, both Shield directions at 65, rejection below 65, and missing
release dependencies. Broker calls are mocked. No order is sent by these tests.

Pending LIMIT exits still use the existing alert-and-reconcile behavior. This
patch adds no automatic price chasing. Live rollout and broker fill observation
remain unverified until performed against the deployed system.
