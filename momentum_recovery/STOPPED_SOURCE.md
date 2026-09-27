# Operator-managed holding: stopped-service source maintenance

At 23:58 IST on 27 September 2026, the operator explicitly authorised stopping
KADIMA while personally managing the manual carry-forward holding. This permits
a restricted source-only update while the application stays stopped. It does
not make the broker account flat and does not resolve the old historical ledger.

`install_stopped_source.py` stages exact PR19 commit
`bd294a7048749258fbaa2cc786f20a441a2a8073`, tree
`1afbeca8a8658039b031817be65a11e47567e4c5`, verifies every staged blob, and uses
its reviewed focused source/dependency plan. It preserves the composed executor,
configuration and dashboard. Plan is the default. Applying requires both
`--apply` and `--manual-holding-external`, reflecting the operator's instruction.

The apply path checks paused controls, held entry/deployment timers and inactive
workers; validates the running process and exact source predecessors; records
one durable attempt; stops the engine and scanner plus the known optional macro
and watchdog services if installed; verifies they stopped; backs up original
sources; and installs only the PR19 focused file set. The installer invokes no
broker reader/order operation, ledger repair, activation or automatic restart.
The existing graceful service shutdown may log out its broker API session and
persist normal engine state; that shutdown path does not square off a position. The
installer does not rewrite the execution ledger or claim every possible external
cron job or broker-side automation has been disabled.

The replacement files cover run.py, stock-fast selection/execution/API, the news
collector and the new preparation diagnostics / Move Capture / quote-watch
modules. The source release does not activate those strategies. The exact
matching predecessor plan, rather than a whole checkout replacement, preserves
the installed socket feed, FIFO limiter, session/portfolio composition and local
configuration. Runtime files are written only after the selected services stop.

If installation fails, source rollback is attempted while services remain
stopped. No failure path automatically resumes trading or services. An attempt
marker prevents blind replay after a partial attempt. Status and source backups
are under `/var/lib/kadima-stopped-source-20260928/`; preserve them for recovery.
The stopped-source result is distinct from a completed end-to-end rollout.

## Operator command

Fetch the published maintenance commit into `/home/ubuntu/kadima-py`, without
checking it out, and run its `momentum_recovery/install_stopped_source.sh` with
the same full commit SHA. The bootstrap fetches the exact PR19 source release,
extracts the maintenance code separately and invokes the stopped-source apply
path. It then prints the existing private UI archive as base64 between labelled
markers so the operator can return it for source review. That archive contains
the installed gateway/page and their hash manifest, not additional credential,
environment, database or evidence files. Its contents remain unreviewed here.

Keep the EC2 machine and SSH session available. This command stops KADIMA
application services; it does not stop/reboot the EC2 instance. The engine and
scanner remain stopped until a separate reviewed restart. The dashboard service
is left available, with backend data unavailable or stale while KADIMA is down.

## What remains pending

- The historical expired-position repair remains separate and unperformed.
  Its pinned runtime admission accepts the PR19 successor run.py and preserves
  the other protected runtime modules. Source-only installation does not bypass
  its broker, process, startup or ledger checks.
- PR20 audit/history and PR24 continuous learning change additional protected
  runtime modules. They are not applied before resolving repair compatibility
  and reviewing the installed UI variants.
- The current after-flat wrapper still requires broker flatness and reviewed UI
  predecessors. The stopped-source command does not edit or call that wrapper,
  its repair, or its original transaction that automatically restarts services.
- CAS, live-inputs, memory, index-scalper, dynamic-stop, standalone RAG and other
  separate requests remain as listed in the deployment inventory. They are not
  silently bundled, activated or labelled deployed.
- Restart, healthy reconciliation, current broker state, API/worker checks and
  eventual activation need their own completed verification. New entries and
  the known automatic entry/deployment timers remain paused throughout this step.
