# Isolated no-money order rehearsal

Run `tools/offline_order_smoke.py` from a separate extracted reviewed release,
not from the live installation. This is the no-money interpretation of the
operator's ambiguous assent to either a dry run or a later supervised live
order. It does **not** authorize, schedule or submit a real broker order.

```sh
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -B -S \
  /temporary/release/tools/offline_order_smoke.py \
  --compare-installed /home/ubuntu/kadima-py
```

No sudo, pip installation, source replacement, database migration in production,
API login, kill reset, service restart or timer/control change is required. The
Git fetch/archive wrapper contacts GitHub only to obtain the pinned release;
it is not part of the offline simulation. Never invoke the old
`kadima/test_order_pathway.py` for this rehearsal: that script can submit a
real order and does not provide these isolation or final-state guarantees.

## What is exercised

The helper runs selected existing execution-lifecycle, broker-response-parsing,
portfolio-capacity and multi-position-entry tests. Those tests use real reviewed
transport/ledger/executor logic with synthetic configuration, quotes, balances,
strategy evidence, broker acknowledgements and fills. The multi-position entry
method is assembled with the same pure patch builders used by the updater.
A new complete synthetic journey submits a simulated BUY, applies partial/full
fills without double counting, requests SELL, applies its partial/full fills,
and verifies that no simulated position or order remains. Another test exercises
the real transport's dry-run order formatting without a session login.

The fixture contracts, quantities and prices are artificial; they are not live
strikes, current exchange lot sizes, market forecasts or account profit/loss.
No real entry criteria are relaxed in the running engine. Test-specific fake
permissions exist only in the isolated Python process.

## Isolation

No production configuration, session module or package initializer is imported.
The production database is not opened. The environment is cleared, optional
site packages are excluded, and all simulation databases/files are placed in
private temporary directories and removed afterwards. A Python audit guard
rejects socket/network operations, broker SDK imports, subprocess/service
operations and writes outside the temporary workspace. It also prevents reads
of the real configuration/session files. These checks are defense in depth for
this reviewed test code, not a general hostile-code sandbox.

With `--compare-installed`, the helper additionally compares only six explicit
installed source files (transport, ledger, storage, portfolio capacity, patched
executor and session entry gate) with the reviewed release. It reads those files
but never imports or edits them. A mismatch fails rather than running unknown
installed code. Other files and runtime settings are not thereby verified.

## Result and limits

Success is `OFFLINE_ORDER_TEST_PASSED`, with zero failures/errors/skips,
`installed_source_files_matched: 6` when comparison is requested,
`real_broker_requests: 0`, `production_database_accessed: false` and
`live_settings_changed: false`. Any skipped functional test, unexpected external
operation, unavailable source or differing source version prevents a pass.

This is **not** verification of Angel One authentication, exchange acceptance,
real-time quotes, available margin, actual fills, live exit liquidity, a healthy
running service, strategy profitability or the number of trades tomorrow.
A later live test needs a separately confirmed contract, quantity, maximum buy
price and handling of possible partial/full fills. Passing this offline test is
not permission to queue an after-market order or auto-submit a live smoke test.

The new guard tests and a subprocess test of the complete rehearsal are included
in repository CI. Local development can run guard tests without a complete source
tree; in that case the full-repository subprocess test is explicitly skipped.
Report CI results separately rather than treating a local skip as execution.
