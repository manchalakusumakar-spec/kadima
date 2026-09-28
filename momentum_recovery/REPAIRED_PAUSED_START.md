# Start the repaired engine with entries paused

## Confirmed replacement manual holding — 28 September, 14:50 IST

The 14:48:21 IST broker receipt showed complete books, zero pending orders,
historical BANKEX absent, and exactly `NFO / CARRYFORWARD /
NIFTY29SEP2622800PE / token 73896 / netqty 65`. At 14:50 IST the operator
confirmed this is his manual trade and that he will manage it himself.

Current startup admission and runtime reconciliation now require either an
empty position book or precisely this new holding. The former 23250 CE qty130
exception is not accepted for current startup. Any pending order, different
identity/quantity, extra exposure, incomplete book or historical exposure blocks.
The engine must report the PE as `MANUAL_CARRYFORWARD`, `auto_managed=false`,
with no managed positions, while entries remain paused. Its verified carryforward
path observes the holding without adopting, exiting or trailing it.

The pinned historical broker reader and repair validator remain byte-for-byte
unchanged. Its receipt still truthfully evaluates the original CE policy and
reports `readiness=false / unexpected_exposure` for this replacement PE. Only
the current startup helper applies the operator-confirmed exact exception to
that validated receipt; it does not rewrite the receipt or claim broker flatness.
The startup status records `startup_manual_holding_exception` explicitly.
Use the newly pinned recovery command; initial admission failures did not consume
the recovery attempt. All other recovery, source and runtime gates remain.

## Fresh broker admission diagnostic

The latest operator screenshot on 28 September shows recovery commit
`8e04eadb4ea7d74d98586b205b9b3820743616ff` blocked at
`fresh_broker_readiness_required`, with `state=blocked`, `activation=false` and
`start_requested=[]`. In that revision this report comes from initial admission,
before creating or claiming the separate recovery attempt. No service start was
requested. The printed status pathname alone does not prove a file was written.

The startup helper now includes `broker_readiness` on that failure, using only
the bounded receipt accepted by the existing strict validator. It shows book
completeness, pending order details, nonzero positions and the redacted error
stage/reason. Raw SDK output, provider messages and credentials are not printed;
invalid or stale receipts are not attached. The same diagnostic is preserved if
the second broker check blocks after the attempt is claimed.

Run the updated pinned `resume_repaired_paused.sh` command to recheck and continue
the paused start if all gates pass. An initial admission failure leaves the
recovery attempt available; a claimed or ambiguous recovery remains one-shot.
Do not infer the current blocker from an earlier order cancellation or manual
position: the previous generic report did not retain that evidence.

## Recovery from the retained override

At 13:16 IST the operator supplied a screenshot of the corrected command failing
at `engine_dependencies_changed`, with the pause override retained and both
`start_command_invoked=false` and `start_requested=[]`. This is an observed
startup failure, not a failed or reversed historical repair.

The old helper compared dependency lists as raw strings. Such comparison can
mistake display reordering after daemon reload for a dependency change. It now
compares canonical relation sets; real changes still block and include the
before/after unit names. The screenshot did not retain those lists, so reordering
is a supported explanation, not a proven host-side diff.

`resume_repaired_paused.sh` selects a narrow recovery path. It requires the exact
recorded before-start failure, private original attempt/status/override records,
the exact unchanged override bytes and manifest, stopped services, and the
complete dependency sets reported by the operator at 09:29. It also verifies
the override is already loaded with the required pause environment. It neither
deletes original attempt records nor rewrites/removes the override nor reloads
systemd. All existing source, repair, pause, fresh broker and runtime gates still
apply.

The recovery has its own one-attempt marker and status under
`/var/lib/kadima-paused-start-20260928/resume-before-start/`. Original records in
the parent directory remain unchanged and are fingerprinted throughout. Any
ambiguous, already-started or different failure is refused. A recovery failure
cannot be blindly retried. Later peripheral startup must read this recovery
status if recovery succeeds; the original status remains the historical failure.

## Initial startup

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
