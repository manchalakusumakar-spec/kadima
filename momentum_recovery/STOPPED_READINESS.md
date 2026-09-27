# Read the stopped installation before historical repair and startup

PR19 and the combined PR20/PR24/custom-dashboard source installations succeeded
on 28 September 2026 at 00:13 and 00:34 IST respectively. All five application
services remain stopped in the supplied host result. New source is installed;
it has not been activated or verified in a running engine.

`check_stopped_readiness.py` has no apply, repair, service-start or activation
mode. The bootstrap stages the exact published maintenance commit and PR24
release separately from the live checkout. It verifies:

- Both root-private completed maintenance records, current stopped units, held
  entry/deployment timers, exact PR24 dependency plan/entrypoint and composed UI.
- The stopped engine's configured Ubuntu identity, direct Python command,
  working directory, unit/drop-in files and explicitly configured environment.
  Only a strict supported subset is interpreted. Shell wrappers, ambiguous
  escaping, unsupported environment sources or pending daemon reload stop the
  check. Environment files are read as data, never sourced or executed.
- The actual configured database directory and account binding to the private
  reviewed evidence. Credential values and raw environment/SDK logs are never
  printed. Interpreter and configuration fingerprints are rechecked.
- Persisted Auto Trade, Stock Live and kill state through an explicit SQLite
  read-only/query-only transaction. The exact historical position/unknown SELL
  intent must still match the reviewed evidence. No runtime module is imported
  for this database inspection.
- Direct watchdog references in root/Ubuntu crontabs, standard `/etc/cron*`
  files, running command lines and the named watchdog timers. Only locations,
  counts and command hashes are printed. Absence of direct matches is not proof
  that every possible indirect or external trigger is disabled.

With `--broker-check`, a separate Ubuntu process imports only the verified
configuration/session dependencies, logs in, and requests both `position` and
`orderBook`. It reads the order book even when positions are nonzero. No order,
cancel, modify or square-off method is called. Normal session audit logs can be
appended by login; the execution ledger is not edited.

The narrow exposure check accepts either no nonzero positions or exactly the
operator-managed `NFO / CARRYFORWARD / NIFTY29SEP2623250CE / token 73921 / qty 130`.
The latter remains `flat: false`. Historical BANKEX exposure, another holding,
changed quantity, any unresolved broker order, incomplete/invalid books or
account mismatch prevents a positive repair-readiness observation. This does
not authorize any trade or startup.

Fetch the full published maintenance SHA into `/home/ubuntu/kadima-py` without
checking it out, then execute that commit's
`momentum_recovery/check_stopped_readiness.sh` with the same SHA. The bootstrap
uses the existing private evidence path
`/tmp/kadima-release.ykY99v/reviewed-bankex-95.json`. It never invents evidence.
Return the JSON report; no credentials or database upload is required.

If service configuration uses unsupported syntax, the report includes safe
metadata and a specific reason for review. Do not edit the unit or its
credentials just to satisfy the diagnostic. Successful observation is still
separate from repair execution: a subsequent exact historical-repair command
must make a SQLite-consistent backup, recheck current complete broker books and
evidence, retain original rows/unknown-order history and use truthful receipts.

Keep the EOD watchdog stopped. Its standalone source has a direct broker-order
path that does not inherit the engine's manual-carryforward treatment. Also,
global Auto Trade pause does not disable engine reconciliation and all existing
order-management paths. A later reviewed startup must check that the ledger is
resolved and establish API/reconciliation/worker health before activation.
