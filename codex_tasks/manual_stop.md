# Why did my manual NIFTY 22250 PE (2026-10-09) get no stop-loss? + protect manual trades

Work in ~/kadima-py (LIVE engine, newer than GitHub; no git checkout/pull/reset).
## Hard rules
- No orders and no Angel One API calls during this task. Never print credentials.
- Do not reset the kill switch or change celestial config. Do not restart kadima.
- Back up every file before editing to ~/backup-codex-manualstop-<timestamp>/.
- py_compile every changed file, run tools/test_offline.py and all tools/test_*.py, show diff -u.

## Part A - Diagnose (read only)
I bought NIFTY 13-Oct-2026 22250 PE manually in the Angel One app on 2026-10-09; it lost ~70% and
no stop-loss fired. From /var/lib/kadima/audit/2026-10-09.jsonl and kadima.db report:
1. Every event mentioning this contract (22250PE / its token): time, event, reason, product type
   (INTRADAY vs CARRYFORWARD/NRML), quantity, buy price.
2. How kadima/celestial_adoption.py classified it: adoption_verified / carryforward_observed_not_managed /
   adoption_disabled_or_external_order_pending / unsupported_position_type_or_contract /
   contract_or_lot_mismatch / entry_basis_unavailable, and the exact line of code responsible.
3. Whether a positions row was ever created for it, and whether check_exits ever evaluated it.
4. Whether the 15:15-15:40 no-auto-exit window (config.in_no_auto_exit_window) or reconciliation
   blocking (_reconcile_blocked / _positions_verified / _exit_paused) played a part.
5. When and why kill_latched became True that day, and whether it is related to this trade.
Give a one-paragraph plain-English answer first, then the evidence.

## Part B - Fix: protect manual long option positions
Goal: any LONG option position I open manually (INTRADAY or CARRYFORWARD/NRML) on an index or F&O
stock is protected by KADIMA unless I opt out.
1. Adoption:
   - CARRYFORWARD long options: adopt as kind 'MANUAL_PROTECTED' instead of only observing.
     Keep their product type on the Position (add a product field if missing) and persist it.
   - An external pending order should block adoption ONLY for the same contract (exchange, token),
     not for every position.
   - Quantity must still be a lot multiple; entry basis from buyavgprice.
2. Stops for manual positions (config, documented next to existing risk settings):
   MANUAL_PROTECT_ENABLED = True
   MANUAL_HARD_STOP_PCT = 0.30   # exit if premium falls 30% below buy price
   MANUAL_TRAIL_ACTIVATE_PCT = 0.10 and keep 50% of peak gain once active (reuse existing trail
   helpers - celestial.trail_stop / trade_policy.ratchet - do not invent new trailing math).
   The hard stop must ALSO apply inside the 15:15-15:40 no-auto-exit window (that window was meant
   for KADIMA's own trades; a manual position losing 30% must still be cut). Keep the window
   unchanged for KADIMA's own positions.
3. Exits for manual positions must SELL with the SAME product type the position was bought with
   (CARRYFORWARD stays CARRYFORWARD; check angel_orders.place_limit_order's producttype argument and
   execution_ledger.submit so the exit uses it). CARRYFORWARD manual positions must NOT be closed by
   the EOD force-close (15:38) or the eod_watchdog; only by their stop/trail.
4. If a manual position is seen but cannot be protected (lot mismatch, missing price, unsupported
   instrument), raise a Telegram alert once per position: "Manual position <symbol> is NOT protected:
   <reason>. Place an SL in Angel One."
5. Show manual-protected positions in the existing /api/kadima/top20 positions list with
   source='MANUAL_PROTECTED' and their effective stop.

## Part C - Prove it (offline test file tools/test_manual_protection.py, no broker)
1. CARRYFORWARD long 22250 PE bought at 100 -> adopted MANUAL_PROTECTED, stop 70.
2. LTP 69 at 15:20 (inside no-auto-exit window) -> exit requested, SELL with producttype CARRYFORWARD.
3. INTRADAY manual long -> adopted, same stop, exit uses INTRADAY.
4. Pending external order on a DIFFERENT contract -> adoption still happens; on the SAME contract -> not.
5. CARRYFORWARD position at 15:38 with no stop hit -> NOT force-closed.
6. KADIMA's own position at 15:20 above its stop -> untouched (window unchanged for own trades).
7. Unprotectable manual position -> one alert queued with the reason.
All must pass.

## Report
Part A answer and evidence; files changed; backup path; test results; restart command
(sudo systemctl restart kadima, only after 15:45 or before 09:00); rollback command.
