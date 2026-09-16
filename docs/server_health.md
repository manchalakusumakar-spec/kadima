# Live server resources

KADIMA Celestial displays authenticated host resource readings beneath the
system controls. The visible dashboard requests `/dashboard-api/server_health`
every two seconds. Requests time out after four seconds, never overlap, and
stop at sign-out. Hidden tabs stop polling and refresh when visible again.
The gateway shares a one-second cache across viewers. It reads Linux counters
directly; there are no broker calls, subprocesses, new services or dependencies.

## Reading the panel

- **RAM used:** `(MemTotal - MemAvailable) / MemTotal`, with used, total and
  available bytes. `MemAvailable` allows for reclaimable cache. A warning starts
  below 20% available RAM, and critical pressure below 10%.
- **CPU busy:** the change in aggregate Linux CPU counters between samples,
  excluding idle, reported I/O wait and hypervisor steal. Wait and steal are
  displayed separately, alongside the one-minute load and logical CPU count.
  The first sample, a counter reset, or a gap over ten seconds shows
  "Measuring" until another valid sample arrives. Guest counters are not counted
  twice. Warning thresholds are 85% busy, 20% I/O wait and 10% steal.
- **Disk used:** capacity unavailable to ordinary processes on `/`, with
  available bytes. Warnings start below 15% available, critical below 5%.
- **Swap:** usage and configured capacity. A host without swap displays
  "Not configured", rather than implying spare swap capacity.
- **Peaks:** maximum observed RAM and CPU percentages during this page session.
  These reset on reload or sign-out, do not persist, and do not cover periods
  when the page is hidden or closed.

The timestamp identifies the actual server sample and is displayed in IST.
Samples older than six seconds, failed/malformed requests, or a timestamp more
than two seconds ahead of the browser clock are not displayed as current.
Last-known readings are dimmed and their bars hidden until a valid update.
Unavailable counters remain unavailable, never healthy zeroes.

The resource badge describes these measurements, not trading readiness, broker
connectivity or an overall efficiency percentage. Warnings apply to the latest
sample; there is no sustained-pressure analysis, crash prevention, capacity
guarantee or alert delivery while the dashboard is closed. The resource-panel patch itself neither adds swap nor changes instance size or service
memory limits. The later protection release adds a separate memory guard and
optional guarded swap installer; see [memory protection and operations](memory_operations_detection.md).

## Deployment and verification

Use `dashboard_live/upgrade.py --apply` from the reviewed release archive.
The existing installer checks installed source hashes, backs up gateway and
HTML, and restarts only `kadima-dashboard.service`. It preserves private
configuration, engine services, trading flags and Nginx. Sign in again after
upgrading, refresh the page and check that the sample time advances.

`tests/test_server_resources.py` checks counter calculations, cache sharing,
missing readings, pressure states, authentication and no-store responses.
`tests/test_celestial_dashboard.cjs` checks automatic updates, page-session
peaks, critical pressure, stale/error/malformed responses, recovery, sign-out
and desktop/mobile layout using intercepted synthetic data. It contacts no
production dashboard or broker.
