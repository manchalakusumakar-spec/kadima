# Read the stopped installation before historical repair and startup

The 08:24 IST operator result verified the permission correction (0664 to 0644,
same owner and bytes), source/context/account admission, paused controls and the
historical ledger case. Both broker books completed. The manual NIFTY qty 130
holding is permitted and BANKEX exposure is absent, but one broker order is
nonterminal. A positive `stopped_readiness_observed` state means observation
finished; `broker.readiness=false` still blocks repair/startup. The next reader
prints only validated, bounded pending-order details to identify that order.
It does not cancel or modify it or relax the pending-order gate. The local
unknown historical SELL intent and this broker order are separate observations;
do not assume they are the same order.

At 01:00 IST on 28 September, the first host readiness run stopped at service
property parsing before any broker call. All five units were observed inactive.
The reader must handle systemd's repeated `EnvironmentFiles=` output and prove
omitted structured arrays empty rather than assuming that a missing line is an
empty property. No ledger repair, restart or activation occurred.

The same observation found an Ubuntu crontab reference with SHA256
`b0814a2c4c0ffcf904743c6232b20313bd471ba3d2c91afef30c75bfa2d20847`.
This exactly matches `55 9 * * 1-5 systemctl start kadima-eod-watchdog`.
Its schedule uses the cron environment's timezone, which has not been established.
The stopped service can therefore still be invoked by cron.

The next bootstrap, `prepare_stopped_restart.sh`, uses the explicit
`--hold-reviewed-watchdog-cron` option. After the completed-maintenance and
stopped-service checks, it privately backs up the full Ubuntu crontab, comments
only that exact reviewed line, checks the installed result and reruns readiness.
Every other byte of the captured crontab is preserved. Changes observed by the
before/after checks, an unexpected target line or incomplete prior state block
further mutation. The standard crontab tool has no atomic compare-and-swap;
the maintenance locks do not coordinate an independent crontab editor, so an
edit between the last read and installation can still be overwritten. Avoid
editing this crontab during the command; its original private backup is retained.
A repeat accepts only the exact
verified held state; it never overwrites later crontab edits. The receipt reports
whether the hold was verified, including when a later check fails. Keep the
backup for deliberate restoration after the manual holding and watchdog policy
have been reviewed. This option does not start services or change trades.

PR19 and the combined PR20/PR24/custom-dashboard source installations succeeded
on 28 September 2026 at 00:13 and 00:34 IST respectively. All five application
services remain stopped in the supplied host result. New source is installed;
it has not been activated or verified in a running engine.

`check_stopped_readiness.py` has no ledger-repair, service-start or activation
mode. Its default is read-only; only the explicit option above can change cron.
The bootstrap stages the exact published maintenance commit and PR24
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
`momentum_recovery/prepare_stopped_restart.sh` with the same SHA to include the
reviewed cron hold (`check_stopped_readiness.sh` remains read-only). The bootstrap
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
