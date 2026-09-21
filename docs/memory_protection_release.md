# KADIMA memory protection — review release

Base: `codex/move-capture-live` at `bede506373b41e5c5dce2fe283e18621c7c33082`.
This is a source patch, not evidence of an AWS deployment. No AWS resizing,
swap activation, service restart, broker order or live setting change was made.
Do not replace AWS with this whole branch: other feature branches and installed
session/multi-position overlays can differ. Match installed hashes and preserve
those overlays in a separately approved deployment plan first.

## Memory guard

One daemon sampler per participating process reads bounded `/proc/meminfo`
every two seconds, independent of dashboard use. No threads start on import.
The engine starts the monitor after contract loading; news/macro workers start
their own monitors. Detection loops and position management are not gated.

| Available physical RAM | Admission policy |
| --- | --- |
| Below 20% | Warning: defer optional news/FII-DII fetches. |
| Below 15% | Restricted: reject new BUY entries. |
| Below 10% | Critical: continue rejecting new BUY entries. |
| Startup, invalid telemetry, or sampling gap over 10 seconds | Fail closed for new entries; preserve exits. |
| At least 25% continuously for 60 seconds | Recover from restriction/warning. |

The 15% entry threshold is intentionally more conservative than the earlier
resource branch's 10%. At startup a server continuously above 75% RAM usage
will not complete recovery. Size the host for headroom; do not disable the guard
to force entries. Missing/delayed news remains explicitly degraded and other
existing news freshness checks can also block entry.

The executor checks admission before preparation. BUY transport rechecks before
and after session login. SELL exits, cancellations and reconciliation do not
consult this guard. This side-based transport rule is for the current long-option
system: any future short-covering BUY needs explicit risk-reduction semantics.
The authenticated `/state` response includes `memory_guard` and its recovery
state. No dashboard HTML, strategy scores, capital settings or auto flags change.

This is an admission policy, not an OOM-proof reservation. There is no automatic
kill/restart, memory cap, `mlock`, change to scan coverage, or swap accounting as
physical RAM. Linux can still page out critical-process memory.

## Lower contract-refresh peak

ScripMaster downloads to a unique temporary file in 1 MiB chunks, rejects
downloads exceeding 128 MiB, parses once, validates the top-level shape, and
atomically replaces the cache. Malformed/interrupted refreshes preserve the
previous cache. Existing cache-age and live-mode fail-closed behavior is retained.
Each service still has its own parsed master: this is not a shared SQLite index
or a constant-memory JSON parser. No measured RAM saving is claimed yet.

## Standalone 1 GiB emergency swap installer

`python3 tools/install_swap_buffer.py` performs read-only preflight. A later,
explicit operator-approved `sudo -n python3 tools/install_swap_buffer.py --apply`
creates `/var/lib/kadima-swapfile` (0600), formats it, activates its systemd swap
unit, and enables that unit at boot. This command was NOT run on AWS.

Preflight preserves existing swap, refuses unrecognized files/symlinks/units,
requires at least 25% available RAM, ext4/xfs, and disk headroom of the greater
of 3 GiB or 20% after allocating swap. Apply is serialized with a no-follow lock.
It verifies the exact swap path is active. An error retains partial files for
inspection; it never runs swapoff, deletes an uncertain active buffer, edits
fstab, restarts trading services, or silently retries activation.

Existing sysctl settings are deliberately preserved. A low `vm.swappiness=10`
is a candidate for a separately reviewed host-tuning change, not a hard guarantee
that only cold pages will swap. Swappiness expresses relative reclaim cost,
not a percentage threshold. See the [Linux kernel documentation](https://docs.kernel.org/admin-guide/sysctl/vm.html#swappiness)
and [systemd swap-unit documentation](https://www.freedesktop.org/software/systemd/man/systemd.swap.html).

## Verification and release boundary

Added offline coverage: 14 guard/execution tests, 12 swap tests, and 5 contract
cache tests. Broker/system commands are mocked; temporary fixture files are not
real swap. Existing test fixtures now supply deterministic memory conditions.
Source pins include the new guard; the session overlay accepts only the exact old
or new executor hashes, and dependent helper pins are updated, not disabled.

Before AWS rollout: confirm the deployed source and overlays, complete CI,
confirm flat exposure/no pending orders for any required service restart, then
approve a minimal deployment. Afterward verify `/state.memory_guard`, swap boot
configuration, available RAM, swap-in/out activity, quote freshness, and exit-loop
latency through a full market session. Per-service profiling, persistent swap-I/O
alerts, compact shared indexes and EC2 resizing remain separate work.
