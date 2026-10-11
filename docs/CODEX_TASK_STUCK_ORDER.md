# Codex task — install the stuck-order release on KADIMA-BRIDGE

Owner: Kusumakar Manchala (trader). Approved 11 Oct 2026: 60-second release, stuck-order
release only, installed **Monday 12 Oct after 15:45 IST**.

## Source
- Repo `manchalakusumakar-spec/kadima`, branch `claude/stuck-order-release` @
  `2d54a04bf6afb37b8d2be851ded7c0f745ec240d`, based on `live/2026-10-11` (the captured server code).
- Changes exactly one live file: `kadima/execution_ledger.py`
  - live (before) SHA-256: `ce2a8aa20480625e4fb6e5c8c29478019b0f13861ab3f9001b7929c87b07a875`
  - new (after) SHA-256: `99920692160d8c13436a4abdd76e9ba61fdddfa7966278d2447f99047ca91775`
- Adds one test file: `tools/test_stuck_order_release.py`
  (SHA-256 `3391a9dddabe6219c690090a3a0200c821e92c7233dc21acec31e8bd537778d7`).
- Read the commit message of 2d54a04 for what changes and what deliberately does not.

## Hard rules (stop and report if any would be broken)
1. Never place, modify or cancel a broker order. No Angel One login from your scripts.
2. Never `git checkout`, `reset`, `pull` or `stash` in `~/kadima-py`.
3. Change no file in `~/kadima-py` other than `kadima/execution_ledger.py` (and adding
   `tools/test_stuck_order_release.py`). No config, `.env`, systemd or other module edits.
4. Step A may run any time. Steps B–D only **after 15:45 IST on a trading day, or on a
   non-trading day**. No restart between 08:30 and 15:45 IST on trading days.
5. If the live `execution_ledger.py` SHA-256 is not exactly the "before" value above, stop
   and report. Do not merge by hand.
6. Keep secrets out of reports.

## Step A — read-only evidence (any time)
Read the live database and audit logs only (SQLite `?mode=ro`):
- Rows in `execution_orders` with `state != 'terminal'` right now: intent, signal_id, side,
  order_id present yes/no, state, created_at. (Do not modify them.)
- For each of the last 10 audit days in `DATA_DIR/audit/*.jsonl`: count of
  `execution_order_unresolved` and `order_submission_unknown` events, and the longest
  continuous stretch per day during which `execution_order_unresolved` kept repeating.
Output: `codex_reports/10_stuck_orders_evidence.md`.

## Step B — preflight (after 15:45)
1. Confirm the live `kadima/execution_ledger.py` SHA-256 equals the "before" value.
2. Confirm no order is waiting: `execution_orders` has no non-terminal row created today,
   and `systemctl is-active kadima` is `active`. If an order is waiting, wait 5 minutes and
   re-check; if still waiting, stop and report.
3. Fetch the two files from `2d54a04` into a scratch folder (not into `~/kadima-py`) and
   verify both SHA-256 values above.
4. On a scratch **copy** of `~/kadima-py` (not the live folder), put the new ledger and the
   test file in place, then run with a temporary `KADIMA_DATA_DIR` and `HOME`:
   `tools/test_stuck_order_release.py`, `tools/test_audit_fixes.py`,
   `tools/test_cas_executor.py`, `tools/test_manual_protection.py`, `tools/test_offline.py`.
   All must pass. Delete the scratch copy afterwards.
5. Show the trader the result of B1–B4 and wait for the trader to type **go**.

## Step C — install (after "go")
1. Back up the live file to `~/kadima-py/backups/stuck_order_<timestamp>/execution_ledger.py`
   with its SHA-256.
2. Copy the new file over `~/kadima-py/kadima/execution_ledger.py` (write with `cp` from the
   verified scratch file; never paste code into an editor). Add the test file under `tools/`.
3. `~/kadima-py/venv/bin/python -m py_compile kadima/execution_ledger.py`.
4. Verify the installed SHA-256 equals the "after" value.
5. `sudo systemctl restart kadima`.

## Step D — after restart
1. `systemctl is-active kadima` is `active`, and the restart counter does not rise over
   5 minutes.
2. `curl -s localhost:8080/health` and `curl -s localhost:8081/health` return ok.
3. Today's audit log shows the normal startup events and no `Traceback` or
   `position_monitor_loop_error` since the restart.
4. The `note` column now exists in `execution_orders` (read-only `PRAGMA table_info`).
5. Rollback on any failure: copy the backup back, `py_compile`, restart, confirm the "before"
   SHA-256 and the health checks, then report.

## Report back
`codex_reports/11_stuck_order_install.md` with: Step A evidence, preflight results, the
installed hash, health checks, and the lines **Broker orders placed: NO** and
**Files changed: kadima/execution_ledger.py only**. Commit the report to branch
`claude/stuck-order-release` and push. Then tell the trader to refresh the live snapshot
branch (`live/2026-10-12`) so GitHub matches the server again.
