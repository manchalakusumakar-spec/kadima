# Resolution of the observed AWS feed dependency

## Evidence and exact comparison

The supplied source-inventory screenshot reports 53 statically checked modules,
four planned stock-fast file updates and one unreviewed dependency:
`kadima/feed.py`. The installed Git blob is
`8a9f8ea0aa5a708739c9297583badabc85fc2c2a`; the previous release blob was
`3d0cd2a4614d20646256fdba8638f8b238d6a0ff`.

The installed blob was retrieved from this repository and compared with the
previous release. The only added import is `math`, and the only changed
existing method is `TickFeed._handle_tick`. All other TickFeed methods are
unchanged. The received tick/callback fields, valid-price scaling, cumulative
volume forwarding, queue interface and public subscription interface remain
compatible with their previous call sites.

The installed change rejects missing/invalid token types and missing, boolean,
unparseable, nonfinite or nonpositive prices. These rejected ticks do not enter
the writer queue or call the engine callback, and do not refresh `_last_tick_at`.
The heartbeat is updated after the accepted tick has been parsed instead of at
method entry. Normal integer/string instrument tokens and numeric prices retain
the existing behavior. This is an input-validation correction worth preserving,
not a reason to overwrite the live feed with the older release.

## Change to this review branch

The release now contains the exact observed AWS feed blob, without editing its
bytes. The additive installer's `FILES` set remains the same four stock-fast
backend files. `feed.py` and the previously preserved FIFO rate-limiter are
checked dependencies, not replacement targets. No installer guard, accepted
hash list, credential, runtime flag, session setting or strategy threshold is
relaxed. An additional unexpected feed edit still fails the equality check.
A change after planning still fails the fingerprint recheck.

This resolves the specific reported source mismatch when the target still has
the observed hash. It does not assert that subsequent service, gateway,
credential, broker-flat or post-install checks will pass. Use the amended
commit, not the older pinned installer snapshot, when retrying. The existing
`stock_fast_update/install.py --broker-check --apply --with-dashboard` path
retains its broker reads, exposure checks, backups and restart/rollback rules.
The installer may refresh authentication and briefly restart the verified
services after its prechecks pass; it does not enable live trading.

## Tests and limits

Seventeen targeted feed tests passed locally using the exact retrieved blob
and isolated audit/storage/session stubs. They cover valid schema/scaling,
invalid tokens/prices, no heartbeat refresh on rejected ticks, recovery after a
bad tick, malformed depth, queue-full behavior and no connection on construction
or subscription. Four additional tests exercise the real installer against
temporary source trees: preserved-feed acceptance without replacement, rejection
of a further feed edit, post-plan fingerprint enforcement and FIFO preservation.
Run the full repository CI for this amended commit before promotion.

These tests do not contact AWS, Angel One or a live WebSocket and are not a
strategy backtest. The preserved feed still timestamps receipts locally and
retains its existing depth/volume handling, generic exception logging, queue
capacity and watchdog design; this amendment is not a comprehensive feed
freshness, security or recovery overhaul. Identical disk files do not prove
in-memory versions, complete market coverage or broker readiness.

No production installation, restart, broker request or trading activation was
performed by this development change. Installation and runtime readiness remain
separate from GitHub CI success.
