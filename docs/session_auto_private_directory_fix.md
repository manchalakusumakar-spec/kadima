# Working-day scheduler: private-directory installation fix

## Observed failure and scope

The user-supplied EC2 screenshot shows the pinned eca4312 setup emitted its
PLAN and successful broker-flat preflight messages, then stopped with
`BLOCKED: private_parent_directory_requires_review`. There is no schedule
completion message. At that exact guard in the pinned installer, service stops,
source replacement, environment/drop-in writes and timer enablement have not
been reached. Directory creation occurs before the old guard, so empty setup
directories may remain; this is not a claim that absolutely no filesystem
change occurred. The broker preflight may have refreshed authentication or
written existing audit/session records. The screenshot does not disclose which
ancestor triggered the generic error or its permissions.

The old helper demanded other-user traversal on every ancestor, even for files
consumed only by root/systemd. It also placed the engine-readable calendar under
`/etc/kadima`, a directory that can legitimately be private. Do not fix this by
recursively chmodding /etc/kadima, changing ownership, printing credentials,
removing risk checks, or force-starting an incomplete installation.

## Change

- Store only the nonsecret calendar at `/etc/kadima-session-auto/policy.json`.
  Both installed gate copies and the installer reference the same new location.
  `/etc/kadima/session-auto.env` remains a root/systemd-consumed file; existing
  credentials, parent directory modes and ownership are unchanged.
- Distinguish root-only directories from engine-readable calendar/permit
  directories. Keep the existing canonical-path, symlink, owner and nonwritable
  target checks. Require public traversal for the shared policy and runtime
  permit, not for root-only systemd configuration or private scheduler state.
  Existing directory permissions are never changed. chmod is limited to newly
  created directories to counteract the bootstrap's private umask.
- Validate existing directories without creating anything before another broker
  login. Revalidate/create directories after the broker check. Unsafe/unreadable
  shared paths still block; this is not a blanket bypass of path checks.
- After writing the staged files, validate/read the actual installed calendar
  as the engine owner before restarting services or enabling the timer. A
  failed read follows the existing pre-restart rollback path.

The scheduler, its automatic global/stock arming, entry-permit limits, calendar
contents, thresholds, capital, stops, kill/pause behavior and timer cadence are
unchanged. This corrects installation paths and checks, not strategy behavior.

## Retry and verification

Retry the same `session_auto/install.py --apply` workflow from this amended,
pinned commit. No manual directory chmod, deletion or stock-fast reinstallation
is required for the reported pre-shutdown failure. It will recheck the actual
installed sources, paused controls and current local/broker exposure, back up
changes, and conditionally restart the verified engine/scanner. Do not make
manual trades or run concurrent maintenance during setup.

Look for `SCHEDULE INSTALLED` and enabled/active timer checks. A timer waiting
outside the scheduled session does not mean immediate live entries. Existing
health, calendar, broker and trade-eligibility gates still apply. The calendar
has not been refreshed or extended by this correction.

This retry is for the reported failed first installation. A previously complete
installation with the old policy layout fails its source-consistency check
rather than being migrated, restarted or rearmed silently. Unknown directory
ownership or a private /etc or /run ancestry may still require explicit review.

## Validation actually performed

Twenty added targeted tests passed locally. They include root-owned 0700 and
0750 credential directories, private umask, public/private reader separation,
symlink/writable/wrong-owner rejection, nonmutating path preflight and the
pre-restart calendar read. One real local unprivileged child verified that the
policy could be read while a root-private credential fixture remained denied.
The tests touch only temporary files and isolated child processes; no broker,
HTTP or systemd call is made. Full repository CI is checked separately after
pushing. Local source/test blobs are hash-checked against uploaded content.

No AWS installation, chmod, service action, broker request or trading activation
was performed by this repository amendment. The retry command remains the
operator's actual installation action.
