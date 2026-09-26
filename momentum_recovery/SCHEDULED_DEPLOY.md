# One scheduled deployment attempt

This helper schedules one guarded attempt on **28 September 2026 at 09:15 IST
(03:45 UTC)**. It does not guarantee deployment: the existing repair and
installation gates must pass at execution time. It never enables trading or
closes a position. A carried NIFTY position still blocks the strict historical
repair before any service stop or database repair.

First keep global auto paused and stock live intent off. Disable the existing
entry scheduler so it cannot arm trading between deployment stages. If its
timer is installed, this stops only that timer, not the engine:

```sh
sudo -n systemctl disable --now kadima-session-auto.timer
sudo -n systemctl show kadima-session-auto.timer kadima-session-auto.service \
  -p LoadState -p ActiveState -p UnitFileState
```

The existing scheduler service must also be inactive. Setup refuses an active
iteration; let it finish and verify controls remain paused. Do not stop the
engine to make admission pass. Absent scheduler units are accepted. The helper
does not change existing units or control flags and does not re-enable the
entry scheduler afterward.

From the reviewed checkout containing this helper, supply a Git repository
that already contains all three exact commits and the reviewed private evidence
JSON. Setup does not fetch or download anything:

```sh
sudo -n python3 -I -B momentum_recovery/scheduled_deploy.py setup \
  --history /absolute/path/to/reviewed/history \
  --evidence /absolute/private/path/to/reviewed.json
```

The pinned releases are PR19 `bd294a7048749258fbaa2cc786f20a441a2a8073`,
PR20 `d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce`, and repair PR21
`e4277654951164e6b59fcdcec4670a44eafe0cf1`. Commit/tree identities and every
extracted blob are checked. The sources, evidence and runner are copied to a
root-private bundle under `/opt/kadima-deploy-20260928` and rechecked before
each execution stage. Setup runs only the pinned helper's pure evidence-schema
validation; there is no broker login, repair or installation during setup.

The one-shot service attempts the normal weekday repair with `--apply`, then
PR19 `--broker-check --apply`, then PR20
`--with-dashboard --broker-check --apply`. It never uses the weekend mode,
`go_live.py`, forced exits or activation. All stages retain their exact source,
broker exposure, local ledger, controls, backup and restart checks. The first
failed stage stops the chain. An already-repaired record or an unrestored
weekday API blocks this specific reviewed repair rather than being skipped.

The timer does not catch up after downtime. The runner admits starts only from
09:15:00 through 09:15:59 IST and records its attempt durably before work. There
are no retries, including after a partial success. Existing child health polls
and cleanup remain intact. Do not restart the deployment unit manually or
delete its attempt marker. A successful earlier stage remains installed if a
later stage fails; inspect the reported stage and backup before recovery.

Verify scheduling and later inspect the result:

```sh
sudo -n systemctl list-timers kadima-deploy-20260928.timer --all
sudo -n cat /var/lib/kadima-deploy-20260928/status.json
sudo -n journalctl -u kadima-deploy-20260928.service --no-pager
```

Only `state: verified` after the attempt means final service/API/database checks
passed. `scheduled` only confirms the timer was installed. Auto remains paused;
price collection and actual session coverage require subsequent fresh quotes.
The existing NIFTY carry is not automatically protected by this scheduling.
