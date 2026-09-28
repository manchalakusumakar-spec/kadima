# Resume dashboard, stock scanner and macro watcher

The operator's 15:05 IST screenshot on 28 September confirms the recovery at
`02ae9988e02eb80723d9f85f597dcb28996da6fc` succeeded: engine PID206362,
started_ticks104422145, state `repaired_engine_running_paused`. The current
manual exception is NFO CARRYFORWARD `NIFTY29SEP2622800PE`, token73896, qty65,
confirmed by the operator at14:50 IST. Peripheral services remain stopped.

At 15:28 IST the peripheral check reported `unreviewed_peripheral_dependency:Wants`
for the dashboard, with `start_requested=[]`. The operator's 15:30 IST systemd
output identifies dashboard `Wants=tmp.mount -.mount`; scanner and macro Wants
are empty. All three have `Requires=sysinit.target system.slice -.mount`.
The helper now accepts exact `tmp.mount` only for the dashboard's Wants relation.
The failed admission did not claim a peripheral attempt or issue a start command.
The corrected bootstrap can continue the same paused startup after fresh checks.

This helper pins that successful startup implementation and reads its success
record under `/var/lib/kadima-paused-start-20260928/resume-before-start/`.
It also verifies and fingerprints the original failed attempt in the parent
directory and the retained pause override. It refuses an ambiguous recovery,
changed engine process, changed manual exception or activation. Historical repair
receipts retain their original policy and are checked without alteration.

This step follows a successful `repaired_engine_running_paused` result from the
pinned engine-start command. The operator reported historical BANKEX repair
complete at 08:58 IST on 28 September 2026. That report alone does not satisfy this
step: the command reads the committed repair archive and engine-start receipts
on AWS and checks the current process and authenticated API snapshots.

The operation starts only:

- `kadima-dashboard.service`: the reviewed gateway at `/opt/kadima-dashboard`,
  using the existing KADIMA virtual environment and loopback port 8082.
- `kadima-universe-scanner.service`: `python -m kadima.stock_universe_scanner`,
  selecting the full-universe observer through `KADIMA_CELESTIAL_ENABLED=1`.
- `kadima-macro-watch.service`: `python -m kadima.macro_watch`.

Both scanner implementations and the macro watcher collect observations and
publish snapshots. Their reviewed source does not place orders. The existing
engine retains ownership of execution controls; global and stock entry controls
must remain paused throughout this step. The manual NIFTY carryforward remains
outside automatic management. No broker order endpoint or control-write endpoint
is called by the startup helper.

The audit release's backend import roots do not include these separately launched
observers. This command additionally checks both observer entrypoints and their
transitive local imports, and retains their exact bytes as change fingerprints.
It preserves the AWS-observed macro commodity/news overlay (`61f04cb8`) and its
headline-only helper (`c8f31dd8`), both retrieved and reviewed from GitHub. These
two reference files under `reviewed_observers` are for source admission only;
they are never copied over the installed modules. Every other local observer
dependency must match the staged pinned release or the exact installed bytes
already admitted by the backend source check, including the composed executor
and deployment-specific configuration. The configuration retains its exact
reviewed hash. No observer or runtime source is installed by this command.

The exact AWS scanner and macro service files have not been exported. Therefore
the command reads and admits their actual effective launch configurations before
any start. It accepts only the direct module launches listed above, the reviewed
Ubuntu owner and directories, empty command hooks, supported literal environment
files and limited infrastructure dependencies. A shell wrapper, alternate module,
unexpected dependency or unsupported environment stops the operation with a
redacted service-context diagnostic. It never prints credentials or command text.
The exact root filesystem dependency `-.mount` is accepted under Requires and
Wants. The former follows the reviewed working directories; the latter covers
the dashboard installer's `PrivateTmp=true` setting, which adds mount Wants in
[systemd's documented implicit dependencies](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).
Arbitrary mounts and root-mount trigger/stop relations remain unsupported.
The observed dashboard `tmp.mount` Wants is also accepted, scoped to that exact
unit and relation; it is not admitted under Requires, trigger/stop relations, or
for the engine, scanner or macro watcher. This agrees with the documented private
temporary-directory mount dependencies, without changing the service unit.
Dependency comparisons use canonical sets so display reordering does not block.

Before starting a service and after health checks, it verifies the paused engine
process identity, its source and retained pause override, fresh broker
reconciliation and learning heartbeat during the reviewed runtime window,
persisted paused policies, session timer
hold and watchdog cron hold. The separate EOD watchdog stays stopped. Its cron
entry is not restored; session automation is not armed. Outside that window,
the same authenticated health checks still require current paused controls and
no managed positions, while runtime-worker verification is explicitly pending.
The gateway's private configuration must retain the same API token binding.

All three peripheral services must initially be stopped. The helper starts them
in dashboard/scanner/macro order and verifies their process identity. The gateway
must answer its loopback health endpoint. Scanner and macro snapshots must have
timestamps after this operation began. Empty observations during startup or
before the market session are reported truthfully; starting the process is not
proof of live market coverage. The command reports market-data verification as
pending until it is checked separately.

The operation changes no runtime file, environment, trading policy or ledger. It
writes one private attempt/status record at
`/var/lib/kadima-paused-peripherals-20260928`. If a start or check fails, it stops
only peripheral services whose start command this operation invoked, in reverse
order, and reports any cleanup failures. It never stops or restarts the engine.
An externally started service is not included in cleanup.

The shell bootstrap requires a full reviewed maintenance commit, verifies the
fetched commits and executes `start_paused_peripherals.py --apply` from a private
archive. Do not publish a command with a placeholder commit. Before publication,
run the focused tests, source review, shell syntax checks and required GitHub
verification. The workflow includes this bootstrap's shell check and privileged
peripheral tests alongside the existing repair/startup checks.

Expected success: `state=paused_peripherals_running`, `activation=false`, three
entries in `start_requested`, empty `cleanup_failures`, dashboard health and fresh
scanner/macro snapshot timestamps. Trading activation remains a subsequent step.
