# Historical expired-option reconciliation

`reconcile_contract_note.py` repairs one narrowly verified prior-day expired
BFO option position from operator-reviewed contract-note evidence. It does not
send any orders, clear a kill, install source, or auto-resume. On apply it pauses
stock live intent through the existing revision-checked API for installation;
global Auto Trade must already be paused and stays paused.
It is not a general bypass for an unresolved submission or an open position.

Supply `--evidence /path/to/reviewed.json`. Without `--apply` this checks source,
process identity, paused API controls and the read-only local ledger only.
The optional `--broker-check-only` mode completes those same admission checks,
then performs one fresh broker login and reads position/order books before
returning. It does not pause controls, stop/restart services, repair the database,
or place/modify/cancel orders. It is mutually exclusive with `--apply`. It can
be combined with `--off-session-unrestored` when that mode's admission checks pass.
`--apply` performs a fresh broker login/read before stopping the engine, repeats
the read after stopping, makes a SQLite backup, and repeats the broker check
before the transaction. The scanner remains running. Fresh unsuccessful or
unreadable responses, nonzero broker quantities and pending orders all block.

Broker failures now print a sanitized `NOTE_BROKER_DIAGNOSTIC` with the authored
stage (`imports`, `account`, `login`, `position`, `orderBook`, or `transport`),
criterion that failed, and an allowlisted exception type. Checkpoints identify
`check_only`, `before_stop`, `after_stop`, `before_commit`, or `after_restart`.
An unsuccessful broker response may include an exact recognized AB/AG code;
unknown codes, provider messages, raw responses, exception messages, tokens and
captured SDK logs are never echoed. No login retry or forced-session behavior
is added. An existing generic flat-check failure alone does not show whether the
failure happened before or after stopping the service.

Evidence keys are `document_sha256`, `signal_id`, `quantity`, `symbol`, `token`,
`exchange`, `trade_date`, `expiry`, `entry_at`, `intent_at`, `exit_at`,
`entry_price`, `exit_price`, `expected_entry_price`, `exchange_order_id`,
`trade_id`, `intent`, `day_buy_qty`, and `day_sell_qty`. The document digest
identifies the reviewed document, not a digital-signature verification. The
operator must review the actual fills and confirm the evidence belongs to the
installed account. Never put document passwords or account credentials in it.

`entry_at` is the time at which the complete reviewed entry quantity was
filled. For two partial buys forming that quantity, use the second fill's time
and their quantity-weighted entry price. This is execution evidence, distinct
from the engine's later manual-adoption time. The existing `positions.entry_ts`
is preserved; the archive retains the reviewed completed-entry time.

The eligible case is exactly one manual-adopted `exit_pending` position and
exactly one matching unknown SELL intent, with no order ID, no accounted fills,
no previous external fills, and no partial remainder. The contract-note day
must equal its expiry and be before today in IST. Known broker order IDs,
same-day positions, changed quantities, source changes and armed controls block.

The transaction archives the complete original position and request plus the
evidence and broker-check receipts. It administratively resolves the old
request without assigning it a broker ID, execution quantity, or notional. The
documented execution is recorded separately as an external historical fill.
Its exchange order/trade numbers use an `EXCHANGE:` namespace, never a made-up
SmartAPI order ID. Corrected entry basis, exit price and actual historical exit
time are written to the closed position. Historical profit is counted once in
the external-fill table on the original trade date, not the repair date.

SQLite backups are root-private under `/var/backups/kadima-note-*`. The engine
is restarted after a stop attempt even if a later check fails. Auto Trade stays
paused. Success requires empty API positions and healthy reconciliation. If
commit succeeds but postchecking fails, the tool explicitly reports that state;
do not restore the database or repeat a trade. Inspect current state first.
Re-running a committed repair fails its unresolved-position precondition and
cannot duplicate the fill or P&L. The separate guarded momentum installer must
still perform all its usual source, local-state and broker checks.

Limitations: this is a supervised evidence-import repair, not permanent
cross-day broker-history retrieval. It does not change live adoption cost-basis
logic or claim the unknown request itself was executed. The existing broker
preflight convention is retained: explicit success with `data: null` is an
empty book; missing `data` or unsuccessful status is unavailable. Fresh books
are observations, not an account lock: do not trade manually during the repair.

Offline tests cover evidence rejection, changed local state, existing orders or
fills, atomic rollback, duplicate prevention, historical P&L attribution, and
broker-read failures versus actual empty books. These do not validate the AWS
host or real broker responses.

## Weekend startup before position restoration

The explicit `--off-session-unrestored` option is for a narrower maintenance
case: the reviewed PR19 `run.py` starts its APIs before waiting for a trading
session. On a weekend restart with force-start disabled, `engine.start()` has
not run, so persisted positions have not been restored into memory. An empty
API position list and a fresh response timestamp do not prove a flat account.

Use this helper from a separate maintenance release based on PR19, retaining
the original installer, runtime and dependency files. The ordinary source graph
checks remain authoritative. Additional exact source pins cover the startup
sequence, clock/session helpers, executor, engine, control API and health store.
The host-specific config must retain the unique direct `IST =
ZoneInfo("Asia/Kolkata")`, environment account and database-path assignments.
Imports, functions, classes, arguments and other ordinary bindings cannot replace
those names or the normal `os`/`ZoneInfo` imports. This ensures the checked
calendar and database are the ones used by the reviewed runtime. Unknown source
versions are refused. No source file or health record is changed by the repair.

The option requires all of the following in both plan and apply:

- The verified engine process started on the same IST calendar day, which is a
  Saturday or Sunday; no operation starts or commits at or after 23:55 IST.
- `KADIMA_FORCE_START` is absent or `0`; Celestial is enabled; global Auto Trade
  is paused and kill is clear. API positions must be empty. Reconciliation health
  must be absent or strictly older than the current process, with a one-second
  margin for the kernel boot-time clock resolution.
- Process identity includes PID, boot time and process start ticks. A reused
  or changed PID cannot satisfy a same-process check. The effective database,
interpreter and pinned sources must remain consistent.
- Evidence includes `account_sha256`: SHA-256 of the installed broker client ID
  after trimming whitespace and uppercasing. The digest must be derived from the
  account independently verified on the reviewed note. Never obtain the digest
  from the installed account merely to make an unverified document match.
  The configured environment and each fresh broker child's imported client ID
  must match it before login or business-state changes. Raw account IDs are not
  printed or stored in evidence.
- The local plan requires the persisted global and stock control rows and checks
  stored reconciliation health. An absent stock row cannot be treated as the
  API's default paused state. An existing stock intent may be enabled during the
  plan; apply pauses it before stopping the engine and requires it paused inside
  the repair transaction.

Example, after preparing and reviewing a private evidence JSON file:

```bash
sudo -n python3 /path/to/maintenance-release/momentum_recovery/reconcile_contract_note.py \
  --evidence /private/path/reviewed.json --off-session-unrestored
```

To diagnose broker readiness without applying the repair, add
`--broker-check-only` to that command. A successful check is only an observation;
the later apply still repeats every admission and fresh broker check.

The default remains a local read-only plan. Adding `--apply` retains all existing
locks, exact position/unknown-intent checks, revision-checked stock pause,
broker-flat checks, source rechecks, engine stop verification, SQLite backup and
atomic comparison. The paused controls and old reconciliation health are checked
again inside the database transaction. The option never authorizes an unresolved
position to be discarded or an unknown request to be treated as a broker fill.

After restart, the helper makes a fourth fresh broker-flat check and verifies the
exact closed position, unchanged request fill accounting, archived originals and
evidence, one external historical fill, and absence of any unresolved local
positions or intents. Global Auto Trade and stock live intent stay paused. The
success message explicitly says **live startup reconciliation remains pending**:
the restarted process is still waiting for the next session. It does not write a
healthy flag, invoke resume, or claim the live monitor has started. A failed
postcheck reports that the repair committed and must not be repeated blindly.

The default mode still requires the matching position in the API and healthy
reconciliation after restart. The weekend option does not weaken that default,
the broker-flat proof, or the subsequent source installer's exposure checks.
