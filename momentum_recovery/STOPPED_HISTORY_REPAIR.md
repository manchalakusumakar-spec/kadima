# Repair the reviewed historical ledger case while stopped

The operator reported cancelling the outstanding broker order at 08:34 IST on
28 September 2026. This statement is not substituted for a fresh broker check.
The repair command reads complete current position and order books before
backup and again inside the final guarded transaction. Any pending broker
order, other exposure, incomplete response or changed historical case blocks
the repair. The exact known manual NIFTY carryforward remains permitted and
is recorded truthfully as `flat: false`.

The command is limited to signal 95, expired BFO BANKEX26SEP62900PE/token
845454/quantity 60, using the existing private reviewed contract-note evidence.
It verifies the evidence digest, exact original local-row digest, database
identity and paused control values observed at 08:24 IST. Source, account,
service context, held timers and watchdog cron must still match. All five
application services stay stopped.

Before a successful commit it creates a private, consistent SQLite backup. It
archives the original position and unknown SELL intent, marks that local intent
terminal without inventing a broker fill/order ID for it, and records only the
reviewed external historical fill. A versioned archive keeps the two actual
broker observations; the earlier all-flat verifier is not used. The exact
durable rows, archive, historical external fill and unchanged controls are
checked after commit.

Prior root-run SQLite diagnostics can leave root-owned WAL/shared-memory
sidecars. The apply path handles only the verified database's regular,
single-link sidecars and restores eligible root-owned sidecars to the known
database owner, preserving their contents and modes. Unexpected ownership or
file shape blocks. Plan mode does not perform this ownership correction.
All SQLite connections are closed before final sidecar ownership verification.

`repair_stopped_history.py` defaults to plan mode; the pinned
`repair_stopped_history.sh` bootstrap explicitly uses `--apply`. It does not
place, cancel or modify broker orders, install new runtime source, start any
service, or enable trading.

State and the one-attempt marker are under
`/var/lib/kadima-stopped-history-20260928/`. After a commit attempt, do not repeat
apply to repair an uncertain outcome; inspect the retained status, archive and
backup. A failure after a durable commit must be treated as postcheck failure,
not evidence that nothing changed. Successful repair is reported separately
from the later paused startup and activation steps.
