# KADIMA: clear EVERY auto-trade blocker before Monday's open

Work in ~/kadima-py (LIVE engine, newer than GitHub). Today is a non-trading day; the goal is that on
the next trading day a valid setup (like 2026-10-09 09:30 BANKNIFTY 55100/55200 CE) is ordered
automatically.

## Hard rules
- No git checkout/pull/reset/stash. Back up every file before editing to ~/backup-codex-preflight-<timestamp>/.
- Never place, modify or cancel an order. Never print credentials (mask anything token/key/password-like).
- Read-only broker calls are allowed ONLY for: position book, order book, RMS limits (to verify the
  account is flat and funded). No other Angel One calls.
- Do not change trading thresholds, stops, sizing rules or the news-gate logic already installed
  unless a step below says so.
- py_compile every changed file, run tools/test_offline.py and every tools/test_*.py; show diff -u.
- Do not restart kadima until the final step, and only if it is outside 09:00-15:45 IST on a weekday.

## Step 1 - Build tools/preflight_autotrade.py (read-only checker, re-runnable every morning)
It prints one line per gate: PASS / FAIL / WARN, the evidence, and the exact fix. Check ALL of these,
reading the real code paths (auto_executor.evaluate_and_enter, fast_momentum.live_blocks,
gap_and_go.live_blocks, celestial.top20, market_session_gate.entry_block, portfolio_capacity,
capital_guard, risk_budget, execution_ledger, killswitch, config):
 1. Env switches for kadima.service (incl. EnvironmentFile): KADIMA_FAST_MOMENTUM_LIVE,
    KADIMA_CELESTIAL_ENABLED, KADIMA_SESSION_AUTOTRADE, KADIMA_API_TOKEN present (masked).
 2. config.ORDER_PLACEMENT_ENABLED is True.
 3. Celestial config: auto_trade_enabled, kill_latched, score_threshold, revision.
 4. Kill switch file /var/lib/kadima/KILL absent.
 5. market_session_gate: policy file + lease/boot files present and valid for the NEXT trading day
    (calendar_reason / permit_reason would return None at 09:16 next trading day).
 6. Execution ledger: no non-terminal (pending/unknown) orders left over.
 7. positions table: no rows stuck in entry_pending / exit_pending / order_unknown.
 8. Broker (read-only): open positions and pending orders. Any open manual position (e.g. NIFTY 22250 PE
    NRML) -> report it; a CARRYFORWARD position blocks new entries in that underlying
    (_manual_carryforward_underlyings) and pending external orders can block adoption.
 9. Funds: RMS available cash vs the cost of 1 lot ATM BANKNIFTY and NIFTY option at last known premium;
    capital_guard / portfolio_capacity / risk_budget would allow >= 1 lot (show the computed numbers).
    Flag any hardcoded leftover limit (e.g. risk_budget MAX_RISK_PER_TRADE_INR=750, STARTING_CAPITAL_INR)
    that would block 1 lot.
10. Daily loss limit state (realized P&L today vs DAILY_LOSS_LIMIT_INR) - resets each day.
11. News gate: index_event_gate returns None for BANKNIFTY/NIFTY/SENSEX at 09:30 next trading day;
    news collector health record exists.
12. Session/login: SmartAPI credentials present, clientPublicIp in session.py matches the instance's
    current public IP (curl -s http://169.254.169.254/latest/meta-data/public-ipv4 or IMDSv2 token).
13. Services: kadima active (or will start via cron/systemd at 08:30), nginx ok, disk free > 1 GB
    (warn if root filesystem > 90% used and list the 10 biggest files/dirs under /var/lib/kadima and ~).
14. Clock/timezone is Asia/Kolkata or code uses IST explicitly.
15. Replay check: re-evaluate the logged 2026-10-09 09:30-09:33 BANKNIFTY SETUP_READY rows through the
    current gate functions (with today's real config) and show which blockers would remain.
Finish with a summary: "AUTO-TRADE READY: YES/NO" and the list of remaining FAILs.

## Step 2 - Fix every FAIL that is a code or file problem (not a user decision)
For each code/file FAIL from Step 1, fix it minimally, with a test in tools/test_preflight_fixes.py.
Examples: stale session-gate lease/policy files, leftover stuck ledger/position rows that the broker
confirms are closed (mark terminal with an audit.log entry, never delete), a hardcoded risk/capital
constant that blocks 1 lot (move it to config.py and make it use the live RMS capital as already
decided: CONFIRMED tier -> availablecash base, ACTIVE tier -> net base, 3% risk), a stale public IP in
session.py.
Do NOT: reset the kill latch, enable auto-trade, close or exit positions, or change thresholds.

## Step 3 - Manual position protection
Read ~/kadima_codex_manual_stop.md (fetch it with:
git -C ~/kadima-py show origin/hourly-forecast:codex_tasks/manual_stop.md > ~/kadima_codex_manual_stop.md
after git -C ~/kadima-py fetch -q origin hourly-forecast) and carry out Parts A, B and C exactly.

## Step 4 - Re-run tools/preflight_autotrade.py and report
- The full PASS/FAIL table.
- What you fixed (files, backup path, tests).
- What only I can do, as a numbered checklist, expected to be:
  a) Close or protect any open manual position flagged in check 8 (or confirm it should stay).
  b) In KADIMA Celestial: press "Reset kill" (needs a flat account), then "Resume" to turn auto-trade on.
  c) Rotate Angel One API key / MPIN / TOTP secret and KADIMA_API_TOKEN (exposed in tool output earlier).
- Restart command if allowed now: sudo systemctl restart kadima
- Rollback command for every changed file.
- The one-line command I should run every morning at 09:00: python3 tools/preflight_autotrade.py
