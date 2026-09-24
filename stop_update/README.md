# Observed AWS MCX host: disabled stop-adapter installation

Observed 24 September 2026 09:11:58 IST: both services running from
`/home/ubuntu/kadima-py`, Celestial enabled, stop rollout unset, stop modules absent.
Executor Git blob: `7cfe6cf21a520ba967d6a672b2f54a99bbe81321`.
Ledger Git blob: `1fde45163927f6fceff4a6ae8573e4242ac4fb18`.
Configuration Git blob: `fb31412c01019a8875a2c9321d71b9b6c840024f`.
These identify source bytes, not current broker positions or live-order readiness.

This package composes the tested adapter onto that MCX executor. It preserves
session entry guards, portfolio sizing, quote batching and MCX exits. The normal
repository executor must NOT be copied over the installed composed executor.

`install.py` defaults to planning. It reuses the pinned installer, requires the
observed source hashes, checks dependencies, and appends reviewed new settings
to the exact installed configuration. Existing configuration bytes survive.
The flag remains OFF. Unknown or subsequently modified source is rejected.

## Obtain a separate release checkout

With existing GitHub access on AWS, run as ubuntu (not sudo):

```sh
set -euo pipefail
release_dir=$(mktemp -d /home/ubuntu/kadima-stops-review.XXXXXX)
git -C /home/ubuntu/kadima-py fetch origin refs/pull/18/head
release_sha=$(git -C /home/ubuntu/kadima-py rev-parse FETCH_HEAD)
git -C /home/ubuntu/kadima-py archive "$release_sha" | tar -x -C "$release_dir"
printf 'RELEASE=%s\nDIRECTORY=%s\n' "$release_sha" "$release_dir"
sudo -n python3 -I -B "$release_dir/stop_update/install.py" --target /home/ubuntu/kadima-py
```

Compare the printed SHA to the reviewed PR head before applying. This fetches
Git objects and extracts into a new directory; it does not switch or reset the
running checkout. If the server is not a Git checkout or authentication fails,
stop and obtain the exact commit archive through authenticated GitHub; do not
paste credentials or replace the working checkout.

## Installation gates

1. Pause automatic entries using the dashboard's existing controls, including
   stock-fast live intent. Resolve positions and pending orders. Do not close a
   position merely to force this installer through.
2. From the same release directory, run `sudo -n python3 -I -B
   stop_update/install.py --broker-check`. It reads broker books using the
   installed session, with order placement disabled in that check subprocess.
   It does not place, modify or cancel an order. All broker positions must be
   flat, including carryforward positions; there is no exemption in this package.
3. After checks succeed, pause the session-auto timer and ensure its service is
   inactive so it cannot resume trading during maintenance. Record its prior
   state. This installer does not automatically stop that scheduler.
4. Run `sudo -n python3 -I -B stop_update/install.py --broker-check --apply`.
   It rechecks paused controls/source identity, backs up affected files under
   `/var/backups`, stops verified engine/scanner services, rechecks broker/local
   flatness, writes only the planned files, checks imports, and restarts services.
   Source is restored if installation fails before restart. After any restart
   attempt, source is retained for inspection; database rollback is never done.
5. Verify service health and installed source before deliberately restoring the
   recorded scheduler state or resuming entries. Existing entry controls are
   not enabled by this installer.

## Not a complete live activation

The production scanners still do not bind new plans automatically. Live
ATR/Greeks/session context and structure producers, validated recovery
probabilities, and broker cancellation/repricing rehearsal remain required.
The custom-number candidates have no validated historical edge. The >=65%
structure exception stays disabled. Installing this package with rollout OFF
also leaves the manual CAS no-stop behavior OFF. Do not enable the flag as a
substitute for completing the missing production integration.

The tests establish deterministic adapter behavior and preservation of the
observed executor's existing entry methods. They do not certify historical
profitability, the current broker account, or a successful AWS deployment.
