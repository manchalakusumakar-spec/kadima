# Memory protection, manual operations and detection continuity

This release keeps existing-position management and Gamma/CAS observation
outside automatic memory throttling. It does not guarantee uninterrupted feeds,
complete strike coverage, fills, profit, or detection of every fast move.

## Memory guard

A small thread in each running engine/scanner/macro process samples Linux
`MemAvailable` every two seconds, including while the dashboard is closed.
New BUY entries are checked at admission and again after broker login, directly
before submission. SELL exits, order reconciliation, quotes and detection do
not use this admission gate. No process is killed or restarted automatically.

| Available RAM | Policy |
| --- | --- |
| At least 20%, after startup recovery | Normal operation; a previous warning clears only after recovery. |
| Below 20% | Defer optional news and FII/DII collection; preserve market scans. |
| Below 10%, unavailable metrics, or startup | Block new BUY entries and defer optional work. |
| At least 25% continuously for 60 seconds | Clear memory restriction; existing reconciliation, freshness, cutoff, position, capital and strategy checks still apply. |

A sampling gap over ten seconds restarts recovery. Invalid measurements cannot
become healthy zeroes. Manual entry holds persist across engine restarts and
are separate from memory recovery, Celestial auto-trading flags and the kill
switch. The monitor logs local state transitions; this release does not add
external RAM notifications or AWS auto-resizing.

The scrip-master download now streams to a unique temporary file, validates and
parses it once, then atomically replaces the cache. The prior path held a full
decoded download and parsed the master twice. Each service still owns its own
master; there is no cross-process shared-memory store.

## Website operations

The authenticated Operations panel offers Start, Stop, Restart and Delay
(5/15/30/60 minutes) for:

- Engine, including index Gamma and CAS observation.
- F&O stock Gamma scanner.
- Macro/news worker, including MCX quote observation.

Start on an already-running engine releases only the operations entry hold;
it does not enable Celestial auto trading or clear a kill switch. Pause new
entries leaves scans and existing-position management running. Delay stops the
selected service and schedules a later Start; it is not a quote polling delay.

Stop/Restart/Delay are refused during protected weekday scan windows:
09:00–15:40 IST for engine/stock scans and 09:00–23:30 IST for MCX/macro.
A delay extending into a protected window is refused. These conservative
windows do not implement an exchange holiday calendar. The buttons explain
restrictions. Manual off-session Stop persists until Start; operators must
ensure required services are running before the next session.

Engine Stop/Restart/Delay additionally require an acknowledged entry hold,
then fresh read-only local and broker checks proving flat positions and no
pending/unknown orders. If this check fails, services continue and new entries
remain paused. Start/resume can release that hold. No service control sends,
modifies or cancels a financial order. A lost response is an unknown outcome;
check service state before repeating an action.

The gateway retains its unprivileged sandbox. A root-owned helper exposes a
0600 Unix socket to the existing `ubuntu` service user, with peer checks,
allowlisted service/action names, bounded requests, and serialized mutations.
There is no shell-command endpoint, sudoers change or public operations port.
Mutations return an accepted job immediately and expose session-bound status
polling, preserving the existing proxy timeout. Polling never resubmits an action.
The helper returns only sanitized state. Changes are controlled by the existing
dashboard authentication and Origin checks. The existing server trust model
allows processes under the shared `ubuntu` account to access this socket.

The memory guard itself remains automatic and cannot be disabled through a
Start/Stop button. Protection of existing positions is not optional.

## Detection gaps repaired

- The old CAS module was not connected to the engine and called a nonexistent
  `far_otm_quotes` method. The engine now supplies completed quote snapshots.
- CAS warms from 14:50 IST and can emit observations from 15:15 until the
  configured close. It uses an observed two-minute premium rise of at least
  40%, incremental volume rate at least 3x the preceding approximately
  eighteen-minute rate, and model absolute delta 0.05–0.20. These are heuristic
  criteria, not an exchange guarantee or a claim of measured gamma.
- Samples need fresh broker feed/trade times and a fresh underlying. Missing
  source times, gaps over sixty seconds, date changes or cumulative-volume
  resets require new history. Duplicate timestamps cannot add history.
- The index ladder expands from the configured width to ATM +/- ten strikes
  at 14:50 (bounded to at most 50 contracts per quote request). This improves
  far-OTM coverage but still does not monitor every strike or expiry.
- Index Gamma observations continue until configured session close; the order
  entry cutoff remains enforced in the executor.
- Index entry preparation uses a bounded latest-observation queue, outside the
  detection loop. Replaced/expired snapshots cannot be used for entries.
- The existing full-universe scanner is now the default even with Celestial
  trading rollout disabled. It monitors resolved NFO stock roots at nearest
  expiry ATM +/- one strike, in <=50-token batches. No trading flag is enabled.
  Set `KADIMA_FULL_UNIVERSE_MONITOR=0` to retain the legacy rotating shortlist
  when Celestial itself is disabled. Unresolved mappings stay explicit.
- Qualifying Gamma strike observations and CAS observations are stored before
  notification attempts, independently of whether an order is permitted.
  The website shows the latest 40 saved events within 24 hours. Notification
  queue acceptance is not proof of external delivery.

CAS remains observation-only; this release does not introduce a CAS order
strategy or enable MCX order execution. Polling can miss moves between samples,
source outages reset warmup, and unmonitored contracts remain outside coverage.
An in-flight network call can finish before optional work yields. This release
is not a guarantee against OOM; market-hour memory/scan-age measurements still
need to be observed on EC2 and may justify a larger instance.

## Deployment

From the pinned reviewed release archive, run in this order:

1. `sudo -n python3 executor_fix/install.py` — guarded full backend deployment;
   source checks, flat local/broker positions, no pending orders, backup and
   restart of active engine/scanner/macro services.
2. `sudo -n python3 operations/install.py` — installs the private operations
   socket/helper without changing gateway privileges or trading flags.
3. `sudo -n python3 dashboard_live/upgrade.py --apply` — guarded dashboard update.
4. `sudo -n python3 tools/install_swap_buffer.py --apply` — optional emergency
   buffer setup, with filesystem, disk headroom and available-RAM checks.

The swap tool creates a 1 GiB mode-0600 file on ext4/XFS and enables a systemd
swap unit. Existing active swap is preserved. Unknown files/units are refused;
`/etc/fstab`, swappiness and instance size are unchanged. It never executes
swapoff as rollback. Swap can absorb transient pressure but disk I/O can slow
execution; sufficient physical RAM remains necessary.

Sources: [Linux memory reclaim and OOM](https://docs.kernel.org/admin-guide/mm/concepts.html),
[util-linux swap-file requirements](https://man7.org/linux/man-pages/man8/swapon.8.html).

After install, sign in again and verify advancing RAM timestamps, current
operations status, running services, guard recovery and actual market-hour
coverage. Review saved detections and broker state separately from entry
eligibility. No live order is needed for these checks.
