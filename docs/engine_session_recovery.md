# One-time engine session recovery after a recorded AG8001

## Evidence and scope

The operator's 20 September 2026 21:20 IST report contains a POSITIONS/AG8001
entry in the 19 September SDK log folder, but no verified timestamp on that
line and no errors in the bounded last-two-hour journal read. This establishes
a recorded invalid-token response, not a current broker outage, a confirmed
midnight-expiry cause, or a flat account today.

Angel One's current documentation identifies AG8001 as Invalid Token and states
that login sessions last until midnight. The checked KADIMA session code uses
a six-hour age test, not a calendar-day test, and its position reader has no
authentication-specific recovery. A stale cached session is plausible; the new
operation verifies current responses instead of assuming the old error persists.

Official references checked 20 September 2026:
https://smartapi.angelone.in/docs
https://smartapi.angelone.in/docs/User

## Run once from the reviewed checkout

```
sudo -n python3 -I -B -S multi_position_update/recover_engine_session.py --apply
```

Without --apply the helper checks and prints a plan; no new broker login,
reset, source modification or service action is performed. The intended case
is the already-installed session-aware PR #10 release with a latched kill,
paused global/stock controls, and no locally tracked or pending exposure.
It also understands the known multi-position overlay, without applying it.

The helper:

1. Verifies the pinned helpers, installed Python dependency tree, both calendar
   gate copies, scheduler, engine/scanner identities and runtime permissions.
   It refuses the normal arming window (with a five-minute safety margin),
   a separate file kill, forced startup, concurrent maintenance, or changed
   controls. It does not delete any file kill or bypass an active latch.
2. While the original engine remains running and the kill stays latched, makes
   one fresh login in an isolated child using the verified engine environment.
   It reads the current position/order books and requires an explicit flat
   result. Null success data retains the existing broker-preflight convention;
   missing data, failed status, invalid quantities and unresolved orders block.
   Recognized current error codes are printed; SDK bodies/credentials are not.
3. Only after the fresh flat check passes, temporarily stops the scheduler timer
   and active check, then restarts the verified engine without changing source,
   configuration, risk values or the scanner service. This discards the engine's
   own in-memory session. A successful login in an independent child alone does
   not refresh the session cached in the live engine.
4. Verifies a new engine PID, unchanged source/runtime settings and paused
   controls with the kill still on. Calls the existing authenticated reset-kill
   endpoint once. That endpoint performs reconciliation using the new engine's
   own session, and clears the latch only when its flat-account rules succeed.
5. Verifies the reset and original stock settings; restores the previously
   enabled timer only when fresh controls, source and local exposure are safe.
   It never calls auto-resume, changes stock-live intent or submits an order.

The broker preflight and service shutdown can refresh or terminate API sessions
and write normal audit/SDK records. The scanner is left running during recovery;
this is intended for the quiet, outside-session maintenance window, not an
intraday session migration. Manual trades and concurrent maintenance must not
be performed. Observed empty books are not an account-wide execution lock.

## Result handling

Success includes FRESH BROKER CHECK, ENGINE SESSION, RESET VERIFIED, and an
active restored market-day timer. After that the separate reviewed
multi_position_update/install.py --apply can perform its own account checks
and upgrade. A shell wrapper must use set -e so a failed recovery cannot
continue into the upgrade.

A failed new-login book check stops before service restart and prints a current
sanitized failure. A failed guarded reset is never replayed automatically. If
post-restart controls cannot be verified, or a reset response is lost, the timer
may deliberately remain stopped. A successful fresh flat preflight is not used
to bypass the endpoint's later reconciliation. Do not clear state/database rows
or repeatedly reset a kill to force success.

## Limits and validation

This is one-time authentication recovery, not a change to the ongoing session
renewal policy or a permanent fix for every cause of AG8001. The six-hour policy,
midnight edge case and cross-process authentication coordination are unchanged.
It does not alter the lot cap, calendar, strategy thresholds, or risk amounts.
The subsequent multi-position upgrade remains separately verified. No new
broker re-login or replay behavior is added to order placement.

31 targeted tests cover command ordering, preflight failures, paused controls,
new-engine verification, one reset only, ambiguous responses, timer restoration,
helper pinning and sanitized broker failures. In the partial local test tree,
30 passed and the full-checkout dependency/preflight test was skipped; GitHub CI
runs that test against the complete repository. Tests use mocked service/API
I/O and temporary fixtures. They do not validate AWS restarts, actual broker
sessions/fills, strategy returns or the current account state.

No AWS service, token, control or account was changed by adding this helper to
GitHub. Execution is the operator's explicit one-time action.
