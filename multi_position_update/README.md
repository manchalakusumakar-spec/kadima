# Remove the single-concurrent-position restriction

This is a deployable overlay for the observed installed PR #10 release **with
the completed market-day scheduler**. It removes the shared executor's hard
`min(1, MAX_CONCURRENT_POSITIONS)` check and its import of that legacy constant.
It does not replace one with an arbitrary new position-count limit. Different
contracts may remain open together when all strategy and money checks pass.

The repository's baseline `kadima/auto_executor.py` stays unchanged because
prior pinned installers/tests derive exact patches from it. Run this updater to
modify the actual session-guarded AWS executor. Do not copy that unpatched
baseline over the installed executor: doing so would remove the session guard.
The two calendar/permit checks are retained and verified by the patch builder.

## Entry behavior

- Multiple confirmed open positions may coexist. New entries are not rejected
  merely because one other position is open.
- The existing single-engine RLock still serializes submission, reconciliation
  and exits. There is no new parallel broker writer.
- Any unresolved entry/exit order, partial entry or ambiguous response pauses
  additional submissions until the existing ledger confirms terminal order
  state. This is a pending-order safety rule, not one open position at a time.
- Do not create a second strategy position for the same exchange/token while
  that contract is already held. The current broker reconciliation assumes one
  local position per contract. Other contracts are eligible independently.
- Existing score, direction, freshness, news, broker, duplicate-signal, lot-size,
  per-trade risk and daily-loss checks remain. No risk amounts or flags change.
- Final LIMIT-price sizing adds a shared cash and remaining-daily-risk check.
  Open purchase costs include the existing estimated cost reserve. Aggregate
  modeled stop risk plus costs cannot exceed the remaining existing daily loss
  allowance; realized losses reduce room, while profits do not expand it.
  Whole-lot sizing may reduce the next order or reject it if one lot will not fit.
- Pending orders never contribute assumed sale proceeds or assumed free cash.
  Broker funds that cannot be read, invalid position states, missing structural
  stops or nonfinite values block new entry rather than being treated as zero.
- The existing conservative cash rule deducts open costs from buffered broker
  available funds, which may already account for margin. This can be restrictive;
  it is not a new total-equity estimate or permission to use more capital.
- Position quote polls are split into batches of at most 50 tokens and retain
  the existing FIFO quote limiter. This is not a latency guarantee at high load.

Exits, stops, trailing, holding rules, order ledger and reconciliation methods
are not rewritten. The patch changes only the entry method and quote batching,
plus removal of the unused one-position-limit import. Stop-risk estimates are
not guaranteed maximum losses: slippage, gaps, outages and multiple correlated
positions can still cause losses beyond modeled amounts. This change is not a
promise of 100 daily trades, 100 simultaneous positions, or profitability.

## One-time operator installation

Run from a separate reviewed checkout, outside the scheduler's arming window:

```sh
sudo -n python3 -I -B -S multi_position_update/install.py --apply
```

Without `--apply` it checks and prints a plan only. Apply requires the installed
session-aware sources, timer enabled/active, both auto intents paused, no kill,
verified engine/scanner identities, and empty broker/local positions and pending
orders. It may refresh broker authentication to read books; it never submits,
modifies or cancels orders. Do not trade manually or change controls during it.

The updater verifies exact source versions, including both gate copies and the
existing scheduler. It preserves custom `config.py` bytes and fingerprints them.
Unknown source is refused rather than overwritten or broadly allowlisted.
Only the actual installed executor and new `kadima/portfolio_capacity.py` are
written. Credentials, dashboard, PM2, FIFO, calendar, systemd environment, risk
amounts and scheduler state are unchanged. The legacy MAX_CONCURRENT_POSITIONS
value can remain in preserved config.py; this shared executor no longer reads it.

After an initial broker-flat check, the updater temporarily stops the timer and
any active scheduler check, then stops the verified engine/scanner. It checks
broker/local books and source fingerprints again, backs up/installs the two
files, verifies imports, restarts and postchecks the services, then restores the
existing timer. It does not perform an ad hoc same-day activation or clear an
operator pause; the existing working-day schedule remains responsible for arming.

Success prints `MULTI-POSITION INSTALLED` and `MARKET-DAY TIMER: restored active`.
Backups are under `/var/backups/kadima-multi-position-*`. Before restart, failure
restores original session-aware source where necessary. After restart begins,
source is retained rather than rolled back under potential exposure. A postcheck
failure may leave the timer stopped; inspect services and exposure before any
recovery. Databases are never rolled back. Re-running a completed upgrade is
status-only, not a hidden rearm.

This upgrade intentionally changes the installed executor hash. Older stock,
session and activation installers must not be rerun against it; future releases
must reconcile this overlay rather than restore the earlier one-position source.

## Tests and operational boundary

54 targeted tests are included: cash/risk math, multiple positions, pending and
partial orders, contract identity, invalid inputs, quote batching, session guard
preservation, maintenance rollback, and execution of the patched real entry
method with mocked external dependencies. In the partial local development
overlay, 44 passed and 10 full-source tests were skipped; the complete GitHub
checkout runs those tests in CI. CI status is reported separately after pushing.
No live AWS upgrade or broker order has been performed by adding these files.
