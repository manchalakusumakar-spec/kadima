# Restore scheduled auto trading after the paused rollout

The operator requested this on 28 September 2026 after supplying the successful
paused dashboard/scanner/macro startup result. This procedure restores the
installed NSE/BSE auto-trading paths and their session scheduler. It does not
merge or deploy the separate CAS, scalper, stop-adapter or other feature branches.

Run the pinned `enable_session_trading.sh` bootstrap once after hours. It takes
the full reviewed maintenance commit as its sole argument and fetches the fixed
installed learning release `8837cd6dc04e16123e94d8b78a3a716f83ed022c` for comparison.
The Python default is a plan; the bootstrap selects `--apply`. Actual AWS
execution remains the operator's step because this session has no SSH/SSM access.

## 18:39 IST installation blocker corrected

The operator's 28 September screenshot from commit
`90cbd1ad0c2257824a2630f673a6879aaf9513d0` reports
`unsafe_parent_directory`, `state=blocked`, scheduler false and live entries
false. No STATUS banner appears: this is constructor/admission failure before
an attempt is claimed or any service restart/flag/scheduler change is requested.

The installer incorrectly applied the private-directory ancestor rule to each
file under `/run/lock`. That rejects a valid root-owned sticky 01777 lock
directory. Debian's systemd packaging documents that shared layout in
[maintainer discussion 1072105](https://bugs.debian.org/1072105). The screenshot
does not expose actual host permissions, so the corrected helper checks them.

The correction admits only exact `/run/lock` when it is root-owned and either
nonwritable by group/others or exact mode 01777. `/run` and `/` retain the original
nonwritable root-owned ancestor rule. Each opened lock must still be a regular,
single-link root-owned file with no group/other write permission. Directory and
file descriptors reject symlinks; the named inode is checked after nonblocking
exclusive locking. The installer keeps the existing lock names/inodes so it
still excludes the existing maintenance/scheduler processes. It never chmods,
truncates, replaces or unlinks lock files or the shared directory.

The generic private-parent helper and all non-lock configuration, state, source,
account, broker, manual-holding, pause and readiness checks remain unchanged.
The updated bootstrap can retry this initial admission failure; it does not
remove an attempt marker or resume a partially executed activation transaction.

## What the command verifies and changes

1. Verify the private successful paused-peripheral receipt and all four process
   identities; recheck installed composed sources, interpreter, service launch,
   dependencies, environment and account binding. Revalidate the historical
   repair/archive without changing the database.
2. Verify global and stock controls are paused, kill is off, threshold is 65,
   the existing carryforward observer is enabled, and exact scheduler sources,
   units, environment and calendar remain installed. Unknown differences block.
3. Read complete current broker books. Accept only flat books or the confirmed
   manual NFO CARRYFORWARD `NIFTY29SEP2622800PE`, token73896, qty65, with zero
   pending orders. Historical BANKEX exposure must remain absent. The old
   historical receipts are preserved, not reclassified as flat.
4. Back up the pause override and scheduler state/disable marker/permit privately.
   Revoke the entry permit, stop only the engine, then repeat broker and policy
   checks. Other observer processes stay running. This installation is restricted
   to 16:00–08:30 IST and rechecks the time throughout.
5. Replace only the retained engine pause override and add a private environment
   file. Celestial/session gates stay on; fast, Gap and integrated Move Capture
   flags load. Force-start, MCX and CAS remain unset. Neither scores nor risk
   budgets/stops change. Restart the engine with global and stock intent still off.
6. Verify actual process flags, paused authenticated APIs, unchanged policies,
   observer health, sources and fresh broker books again. Only then remove the
   backed-up scheduler hold/state and enable its existing timer. The scheduler
   runs once and must publish a fresh outside-window/non-trading-day status with
   its permit revoked and intents paused.

No direct broker order endpoint is called. The existing Celestial carryforward
observer does not adopt, trail or exit the confirmed manual holding, and blocks
new entries for its underlying while it remains. The standalone EOD watchdog
and exact cron hold stay in place because its direct order path lacks that
manual-position separation. The engine's own managed-position exit logic is
preserved.

## Successful result and next session

Expected installation result:

```
state: auto_trading_deployed_waiting_for_session
scheduler_enabled: true
automatic_session_arming: true
live_entries_enabled_now: false
market_readiness_pending: true
```

This is scheduled automatic activation deployed, not proof of current live
entries or a fill. Starting at 09:00 IST on the next eligible ordinary session,
the existing scheduler requires fresh authenticated snapshots, stock-worker
health, broker reconciliation and news before requesting the guarded global
resume and revision-checked stock-live APIs. Entries are allowed no earlier
than 09:15 and before 15:35, subject to earlier strategy limits and all contract,
news coverage, price, size, stop and risk gates. It retries readiness failures
once per minute and issues a boot/day-bound permit lasting at most 150 seconds.
It does not fabricate feed freshness or force the engine through its session wait.

The existing reviewed calendar covers normal sessions through 31 December 2026;
it excludes weekends and its listed exchange holidays and needs a reviewed
update for new closures, special sessions or 2027. MCX is outside this session
policy and is not armed by this restoration.

An observed manual/risk pause is respected for the rest of that session; kill
is never reset. The operator can persistently disable the scheduler using the
existing `runner.py --disable` action without stopping managed-position exits.

## Status and incomplete attempts

Installation receipt:
`/var/lib/kadima-autotrading-20260928/status.json`

Current scheduler status:
`sudo -n python3 -I -B -S /opt/kadima-session-auto/runner.py --status`

If installation fails after claiming its attempt, it disables the session timer,
stops only its scheduler job and revokes the entry permit. It records which
cleanup actions succeeded and the observed engine state. It does not blindly
restart again, stop a running engine or roll back the trading database. The
engine may be stopped if failure occurred between stop and start. The new flags
can remain installed under the revoked permit and paused controls. Review the
reported stage before recovery; rerunning does not erase the one-attempt marker.

Backups and old rollout/history receipts are retained. No secrets are printed.
Offline regression tests cover time/calendar limits, flag scope, unchanged
policies, snapshot freshness, managed-exposure refusal, operation ordering,
failed/ambiguous stages, late verification failure and bounded cleanup. Existing
scheduler tests cover real guarded arming, pause/kill behavior, lost responses
and permit expiry. These tests do not connect to AWS or the broker.
