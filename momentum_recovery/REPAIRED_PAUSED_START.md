# Start the repaired engine with entries paused

The first startup attempt blocked before any start or override change because
its dependency allowlist omitted `-.mount`. At 09:29 IST the operator supplied
the actual host properties: `Requires=sysinit.target system.slice -.mount`,
`RequiresMountsFor=/home/ubuntu/kadima-py`, `Conflicts=shutdown.target`, and all
other inspected dependency groups empty. The engine was inactive with PID 0.
The corrected gate admits only this exact root mount under `Requires`; other
mounts and unexpected dependencies remain blocked. No service unit is edited
to remove the filesystem requirement.

At 08:58 IST on 28 September 2026 the operator reported the pinned historical
repair's success message: "Historical ledger repair committed; all services
remain stopped." The startup command independently verifies its durable status,
archive, exact repaired rows and retained private backup before starting.

The command starts only `kadima.service`. It preserves global Auto Trade pause,
stock pause and their revisions; it removes optional live-lane and force-start
environment flags with a private, removable systemd drop-in. Celestial and the
session permission gate stay enabled because they provide the reviewed entry
and manual-holding behavior. No runtime source or trading policy is replaced.

Two fresh, complete broker observations must admit zero pending orders and either
flatness or only the exact externally managed NIFTY carryforward holding. The
source hashes, account-bound evidence, service configuration, engine dependencies,
repair archive and held watchdog/session triggers are checked around startup.
The known manual holding remains externally managed. The standalone EOD watchdog
stays stopped and its reviewed cron entry stays held.

After startup, GET-only loopback checks verify both APIs, paused control values,
no managed positions and audit, price-history and advisory-learning responses.
During the reviewed weekday runtime window starting at 09:00 IST, success also
requires broker reconciliation and a learning-worker heartbeat dated after this
startup attempt. Reconciliation must describe either no positions or the exact
manual NIFTY carryforward as not automatically managed. Initialization is allowed
a bounded wait; changed pause controls or unexpected managed exposure fail.
Outside that window the report explicitly marks the runtime workers pending.
A successful startup check is not proof of a fresh market feed or successful trades.

Dashboard, universe scanner and macro-watch startup remain pending. Trading
activation remains pending. This command does not start those services or enable
new entries. There is no scheduled 09:15 deployment armed by this maintenance.

The command keeps a private attempt/status record under
`/var/lib/kadima-paused-start-20260928/`. On a startup or health failure it attempts
to stop only the engine it requested, retains the paused override, and reports
the actual cleanup outcome. Do not blindly repeat a failed attempt or remove the
override; review the reported state first.
