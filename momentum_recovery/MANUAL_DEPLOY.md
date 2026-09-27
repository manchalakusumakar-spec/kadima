# Deploy the reviewed sequence after the account is flat

This operator-run command installs the compatible maintenance sequence. It is
not an all-feature release and it does not activate trading. See
[`docs/deployment_inventory_20260927.md`](../docs/deployment_inventory_20260927.md)
for all 21 checked PRs, ancestry and remaining feature work.

## Before running

Close both pending broker orders and open positions through normal broker
controls. In Celestial keep global Auto Trade paused and Stock Live off. Do not
place manual orders during maintenance. The helper independently verifies the
account; a closed order list alone does not prove a flat account.

The reviewed private historical evidence is expected at
`/tmp/kadima-release.ykY99v/reviewed-bankex-95.json`. Its content must still be the
reviewed account-bound evidence. This path comes from the prior operator handoff;
its current existence has not been checked from this session. A missing or
changed file blocks the command. It never guesses or regenerates evidence.

Use the published manual release's full commit SHA, fetch it into the existing
repository without checking it out, and execute its
`momentum_recovery/deploy_after_flat.sh`, passing the same SHA. The bootstrap
fetches the exact three dependency commits and extracts the manual release into
a separate private temporary directory. It never checks out/reset/cleans the
live AWS worktree. Root then verifies/extracts every dependency blob into a
private bundle before using any repair or installer code.

For a plan with a fresh broker read, omit `--apply` from the Python entrypoint.
The default plan stages sources and evidence and writes a diagnostic status;
it does not disable timers, repair the ledger, restart services or install code.

## Sequence and boundaries

1. Verify private bundle, fresh paused controls, inactive competing workers and
   the account-bound historical repair's source/process/local-state admission.
   Run its `--broker-check-only` path. Any nonzero position, pending order,
   unavailable book, wrong account or changed historical record stops here.
2. In apply mode only, disable the session-auto timer and the previous
   `kadima-deploy-20260928.timer`, if present. Their workers must already be
   inactive; the helper never kills them or the engine to pass admission.
3. Record a durable attempt, then run the pinned ordinary weekday historical
   repair, PR19 source installer and PR20 audit/history/dashboard installer in
   order. Recheck the bundle, paused controls and scheduler holds at every stage.
   Each existing child retains its own source, broker, backup and restart checks.
4. Verify engine/scanner/dashboard services, authenticated audit/price APIs,
   account/process identity, empty local unresolved positions/orders and paused
   persistent controls. Only then report `verified`.

Pinned dependencies are repair `e4277654951164e6b59fcdcec4670a44eafe0cf1`,
momentum `bd294a7048749258fbaa2cc786f20a441a2a8073`, and final audit/history
`d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce`. These are the unchanged releases in
PR21's reviewed scheduler. The scheduled helper's clock window and source are
unchanged; the manual entrypoint uses its shared verification helpers without
arming or invoking its scheduled `run` operation.

This does not use the weekend-unrestored repair mode. After the held position is
closed on a trading day, the ordinary API restoration and historical admission
must still pass. An already repaired historical record is not silently skipped.

The final source release preserves the installed MCX/session/portfolio executor.
It adds momentum detection/recovery, durable Gamma/trade audit and daily/minute
price history. CAS PR7, live-inputs PR9, separate Move Capture PR14/15, memory
PR16, index scalper PR17, dynamic stops PR18 and design-only new engines are not
composed or enabled by this command.

## Result and recovery

Status is root-private:

```sh
sudo -n cat /var/lib/kadima-deploy-after-flat-20260927/status.json
```

`plan_verified` is not installation. `verified` means the sequence and final
checks completed; it does not mean live trading is enabled or quotes are fresh.
Auto Trade and Stock Live remain paused and both timers remain disabled.
Re-enabling entries requires the final installed release's current gates and
separate operator action; no older `go_live.py` is called between upgrades.

A failed preflight can be rerun after its blocker is resolved, provided no
attempt marker exists. Once a deployment attempt is claimed, the command refuses
automatic re-execution even if a later stage fails. Preserve
`/var/lib/kadima-deploy-after-flat-20260927/attempted.json`, status, private bundle
and child backups. Do not delete the marker, restore the live execution database
or rerun historical repair to force success. A completed earlier stage remains
installed if a later one fails; inspect the named stage first.

No AWS command, broker order, live setting change or existing timer change was
performed while preparing this release. Offline tests use synthetic state and
mocked broker/systemd boundaries. Production source, evidence availability,
resource behavior and live collection still require the host's real checks.
