# Codex task — 19-Oct-2026 incident fixes: protect capital, don't lose chances

Branch to start from: `claude/trend-band-rules-20261019` (contains A1 + A2 below).
Write your work on a new branch `codex/protect-capital-20261019` and open a PR into it.
Read `docs/DECISIONS.md` first and add a dated entry for anything you decide.

Trader priority (Kusumakar, verbatim intent): **saving is more important than earning, but
losing a chance is also not acceptable.** Every change below follows that rule: hard protection
against crashes and wrong-side trades, loose on normal noise so winners can run.

## STATUS (updated 19-Oct 21:30 IST) — read first

Built by Claude on this branch, tested in GitHub Actions (`.github/workflows/protection-20261019.yml`):
- **B** `kadima/protection.py:entry_basis` + one-line hook in `celestial_adoption.py` (switch `cf_protect`).
  Note: in the 17-Oct lineage CF positions are still "observed, not managed"; the EC2 lineage
  (13-Oct snapshot) adopts CF, so the entry-basis fix is what it needed.
- **D** adaptive monitor 3 s / 10 s (switch `monitor`).
- **E** re-entry after a loss stop-out, max 2 per signal per day, 5-min cooldown, only when
  the trend/band rules are live (switch `reentry`).
- **F** rejected-order watchdog with broker reason, KADIMA/MANUAL source, Telegram + Celestial
  health key `rejections` (switch `watchdog`, live by default - alerts only).
- **C** broker second-line stop: **shadow only** (`broker_stop_shadow` audit lines with the exact
  trigger/limit it would place or modify). Live placement NOT built.
- **G** data only: Celestial health keys `trend:<INDEX>` and `rejections`. No dashboard card yet.
Installer: `tools/install_protection_20261019.py` (after the exit-fix and trend installers).

**Remaining for Codex:** C live (reconcile broker-side SL fills with the execution ledger and
external-order detection - our SL order must be "owned", an SL fill must close the position,
never a double sell; then the 1-lot market-hours test), and the G dashboard card. Everything
else below is reference.

## Hard constraints (do not break)

- Buy-only CE/PE. LIMIT orders only (SL orders must be STOPLOSS_LIMIT, never market).
- The live EC2 tree (`~/kadima-py`) is NOT a clean git checkout and is newer than `main`.
  Deliver every change as a **fail-closed anchor installer** in `tools/` (pattern:
  `tools/install_exit_fix_20261019.py`): each anchor must match exactly once or nothing is
  written; back up each file; `py_compile` before swapping; idempotent second run;
  `--rollback`. Test the installer on these snapshots: `codex/ec2-release-source-20261014`,
  `codex/verify-sebi-cas-circular-before-cutoff-change`, `live/2026-10-11` (after A1+A2).
  If you can capture a fresher EC2 snapshot, test on it too and say so.
- Every new behaviour has a mode switch (`off|shadow|live`) in
  `$KADIMA_DATA_DIR/trend_rules.json` (extend that file; re-read every 30 s, no restart).
  Default **shadow**. Shadow = compute + audit, never block, never send/modify orders.
- No deploys during market hours (09:00-15:45 IST). No new broker endpoints untested.
- Gamma and CAS logic otherwise unchanged; scalping (fast_momentum) and gap_and_go unchanged.

## What happened on 19-Oct (NIFTY expiry, NIFTY -1.2% trend day)

1. Winning PEs exited at +1-3% then ran 30-230% (23100 PE 61 lots sold 72.55 -> 242;
   23000 PE stopped -21.6% -> 148; SENSEX 74500 PE +3.4% -> 850). Cause: trail armed at +5%
   keeping 50%, plus single-tick Celestial exits (spread>3%, EMA9 tick, VWAP tick, any OI flip).
2. Counter-trend CE buys on a down day (100-lot 22900 CE, 96-lot 23000 CE, 66-lot 23000 CE...)
   caused most of the loss; quick exits saved ~Rs 2 lakh on them because they went to 0.05.
3. Some losses ran to -16..-25%: structural stop beyond swing (far on cheap expiry premiums),
   10-s polling + LIMIT exit chasing a fast fall; **no broker-side stop exists** although the
   24-Sep stops.py decision said "broker-side disaster stop ON" — never built.
4. Manual CARRYFORWARD positions (22900 CE 25L, 23200 CE 30L, 22850 CE 5L, 23100 CE 20L) had
   no protection: 17-Oct `celestial_adoption.py` marks CF as "observed, not managed" (never
   exits/trails), and earlier code read only `buyavgprice` (0 for an overnight CF position).
5. The same 17-Oct code blocks **all** KADIMA entries in an underlying while any manual CF
   position exists there (`manual_carryforward_same_underlying`) - a whole day of NIFTY
   chances lost to one manual CF lot.
6. Several 10-lot manual CF SELL orders on 22900 CE were rejected (not KADIMA: KADIMA caps
   2 lots, never slices, rounds INTRADAY prices to 0.10). Nobody was told why.

## Already built (review, keep, do not redo)

- **A1 Exit fix** (`claude/exit-fix-20261019`, installer `tools/install_exit_fix_20261019.py`):
  trail arms at +15%, retain ladder 50% / 65% (>=+35%) / 75% (>=+100%); spread no longer an
  exit; EMA9 and VWAP exits need 2 completed 1-min candles; OI-flip exit needs score >= 70;
  while in profit Celestial adverse signals only lock breakeven. Tests:
  `tools/test_exit_fix_20261019.py`. Status: to be installed **live tonight**.
- **A2 Trend + band rules** (`claude/trend-band-rules-20261019`, `kadima/trend_rules.py`,
  installer `tools/install_trend_rules_20261019.py`, tests `tools/test_trend_rules_20261019.py`):
  1 day trend (3 of 4 votes; threshold 25% of 14-day expected range; flip after 2 completed
  15-min closes beyond VWAP and open); 2 gamma only with trend, SIDEWAYS both sides;
  3 stop follows the hourly forecast band (CE lower edge - 0.25xATR, PE upper + 0.25xATR),
  moves both ways, never inside 2x premium 1-min noise, never wider than the 35% hard cap,
  trail (activation max(15%, 2x noise)) is the minimum once armed, band break exits,
  1.5xATR band when no forecast; 4 entry only in the band half nearest the stop, no chasing.
  Status: install in **shadow**, one session, then live.

## New work

### B. Manual carry-forward positions get protection (fixes 4, 5)
- Adopt verified manual `CARRYFORWARD`/`NRML` long option positions as `MANUAL_PROTECTED`
  (replace the "observed, not managed" branch). Entry basis, in order: `buyavgprice`,
  `totalbuyavgprice`, `cfbuyavgprice`, then `netprice` - first finite > 0. Log the raw key
  names once (`adoption_entry_basis` audit) so we can confirm Angel's live field names.
- All exits for adopted positions use the position's own product via
  `angel_orders.product_for` (a CF long must never be sold as INTRADAY or vice versa).
- Replace the underlying-wide entry block with a **same-contract** block: KADIMA must not
  buy a contract you already hold manually; other strikes/sides in that underlying stay open.
- Tests: CF adoption with each fallback field; product of the exit order; entry allowed in
  other strikes, blocked in the same contract.

### C. Broker second-line stop (fixes 3)
- After an entry fill (KADIMA or adopted), place a SELL `STOPLOSS_LIMIT` (variety `STOPLOSS`)
  for the full quantity with trigger = software stop - one noise-width (noise from
  `trend_rules.premium_noise_pct`), never below entry x 0.65; limit = trigger - 2 ticks.
- Modify it only when the software stop has risen by >= one noise-width since the last modify
  (keeps Angel calls low; respect `rate_limiter`). Never move it down.
- Before any software exit: cancel the SL order, confirm cancelled, then send the exit.
  **If the cancel fails or is unconfirmed: do not send the software sell**; retry the cancel,
  let the broker SL work, alert. A double sell is worse than a late exit.
- If the SL fills, reconcile the position as closed (`exit_reason='broker_second_line_stop'`).
- Same product as the position. Order tag `kadima-sl-<signal_id>`.
- Must be proven with a 1-lot live test in market hours before `live` - write the test steps
  in the PR (do not run it yourself).

### D. Faster checks near the stop (fixes 3)
- Monitor interval: 3 s when any open position's LTP is within 2x its noise of its effective
  stop, else 10 s. Batch LTP calls per exchange; stay within `rate_limiter`.

### E. Re-entry after a stop-out (don't lose the chance)
- Same strike and direction may be re-entered when ALL hold: day trend agrees (A2 rule 2);
  a fresh gamma confirmation (new evaluation, not the old episode); spot back in the band half
  nearest the stop (A2 rule 4). Max **2 re-entries per strike per day**; same risk-based
  sizing; new signal id linked to the original (`reentry_of`). Not after 15:10 except CAS.
- Must also lift the episode-dedup that currently makes a persisting CONFIRMED episode
  un-retryable after a stop-out (see `engine._gamma_entry_signal` / `execution_ledger.seen`).

### F. Rejected-order watchdog (fixes 6)
- Every 30 s in market hours read the order book; for every newly rejected order (KADIMA or
  manual) write audit `order_rejected` with symbol, side, product, qty, price, broker `text`
  reason, time, and `source` = KADIMA if the `ordertag` is ours else MANUAL; show it on
  Celestial (health key `rejections`) and send one Telegram alert per order.
- If a manual SELL exceeds the open long quantity of the same product+contract, alert
  "this sell would create a short position" with held vs. requested quantity.

### G. Visibility
- Celestial card per index: day trend label, since, threshold %, current band and band stop,
  and for each open position: software stop, broker SL trigger, source (KADIMA/MANUAL).
- Daily audit CSV (existing 16:15 task): add columns source, exit_reason, trend_at_entry,
  band_stop_at_exit, would_have_been_blocked (shadow decisions).

## Acceptance

- All new and existing tests pass (`tools/test_*.py`), plus new tests for B-F.
- Replay `tools/replay_day.py` on 2026-10-19 data (if present on EC2) and report: CE entries
  blocked by trend, exits changed by A1, re-entries E would have taken.
- One installer per feature group, each with mode default shadow, `--live`, `--rollback`.
- `docs/DECISIONS.md` entry for 2026-10-19 updated with what you built and any deviation.
