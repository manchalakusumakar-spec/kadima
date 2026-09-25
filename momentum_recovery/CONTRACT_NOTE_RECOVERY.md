# Historical expired-option reconciliation

`reconcile_contract_note.py` repairs one narrowly verified prior-day expired
BFO option position from operator-reviewed contract-note evidence. It does not
send any orders, clear a kill, install source, or auto-resume. On apply it pauses
stock live intent through the existing revision-checked API for installation;
global Auto Trade must already be paused and stays paused.
It is not a general bypass for an unresolved submission or an open position.

Supply `--evidence /path/to/reviewed.json`. Without `--apply` this checks source,
process identity, paused API controls and the read-only local ledger only.
`--apply` performs a fresh broker login/read before stopping the engine, repeats
the read after stopping, makes a SQLite backup, and repeats the broker check
before the transaction. The scanner remains running. Fresh unsuccessful or
unreadable responses, nonzero broker quantities and pending orders all block.

Evidence keys are `document_sha256`, `signal_id`, `quantity`, `symbol`, `token`,
`exchange`, `trade_date`, `expiry`, `entry_at`, `intent_at`, `exit_at`,
`entry_price`, `exit_price`, `expected_entry_price`, `exchange_order_id`,
`trade_id`, `intent`, `day_buy_qty`, and `day_sell_qty`. The document digest
identifies the reviewed document, not a digital-signature verification. The
operator must review the actual fills and confirm the evidence belongs to the
installed account. Never put document passwords or account credentials in it.

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
