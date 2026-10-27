# KADIMA decision log

Shared record for everyone changing KADIMA (Claude, ChatGPT/Codex, manual edits).
Read this before changing anything it mentions; add a dated entry for every decision.

## 2026-10-14 — Session scheduler owns morning arming; `auto_arm` retired

**Decision (trader):** keep the session scheduler (`kadima-session-auto`), retire `kadima/auto_arm.py`.

**Why:** on 13-Oct every auto entry (31 attempts, incl. 8 eligible scalps) was refused with
`lane_permit_not_granted`. Scheduler journal for the session:

| Equity lane reason | Minutes |
|---|---|
| `outside_scheduled_window` | 4 |
| `operator_control_changed` | 1 (~09:05 IST) |
| `operator_pause_today` | rest of day |

The scheduler takes its day baseline of the Celestial controls at midnight. The `auto_arm`
cron (09:05 IST) flips `auto_trade_enabled`, bumping the revision; the scheduler treats any
revision it did not write as an operator change and sets `OPERATOR_PAUSED` for the day.

**Scheduler kept because it** checks the exchange calendar and holidays, reconciliation, news
and worker health, issues a short boot-bound lease that lapses when health fails, arms both
global auto-trade and stock-scalp live, respects real operator pauses, and never resets a kill.
`auto_arm` only flipped one flag once at 09:05.

**Changes:**
- EC2: the `kadima.auto_arm` cron line is commented out (`# DISABLED 2026-10-14 …`).
- Code: `auto_arm.main()` returns immediately (audit event `auto_arm_retired`) whenever
  `/var/lib/kadima-session-auto` exists, so re-adding the cron cannot recreate the conflict.
  Test: `tools/test_auto_arm_retired.py`.

**Operating rule:** during the session, any dashboard Pause/Resume, threshold change or
stock-scalp toggle is treated as an operator pause for the rest of the day (by design).

**Open (not yet decided):** equity lane also requires stock-scalp `live_enabled`, so a stock
worker problem closes index trading too.

## 2026-10-19 — Early-exit fix, trend/band rules, capital-protection plan

**Trigger:** NIFTY expiry, -1.2% trend day. Winning PEs sold at +1-3% then ran 30-230%;
counter-trend CE buys did most damage; manual CF positions unprotected; 10-lot manual CF
SELLs rejected with no alert. Full incident list: `codex_tasks/2026-10-19_protect_capital_keep_chances.md`.

**Trader principle:** saving is more important than earning, but losing a chance is not acceptable.

**Decisions (trader):**
- Exit fix live: trail arms at +15% (was +5%), retain 50/65/75% ladder; spread is not an exit;
  EMA9/VWAP exits need 2 completed 1-min candles; OI-flip exit needs score >= 70; in profit,
  Celestial adverse signals only lock breakeven. Branch `claude/exit-fix-20261019`.
- Four rules for gamma trades (`kadima/trend_rules.py`, branch `claude/trend-band-rules-20261019`):
  day trend first; gamma only with the trend (SIDEWAYS = both sides, normal size); stop follows
  the hourly forecast band and moves when the band moves; entry at minimum risk (band half
  nearest the stop). Thresholds formula-driven from live data; only fixed number is the 35%
  hard cap. Shadow one session, then live.
- Broker second-line stop one noise-width behind the software stop (not at it); if cancelling
  it fails, KADIMA does not send its own sell.
- Re-entry after stop-out allowed: trend agrees + fresh gamma confirmation + spot in band half
  near the stop; max 2 per strike per day.
- Manual CF positions are to be protected (supersedes the 17-Oct "observed, not managed"
  behaviour); entry block narrowed from whole underlying to the same contract.

**Deviation noted:** trail activation is max(15%, 2x premium noise), not fully noise-driven -
a calm option arming at +2-3% would recreate the 19-Oct early-exit problem.

**Update 21:30 IST (Claude):** built B (CF entry basis), D (3 s near-stop checks), E (re-entry,
max 2/signal/day, 5-min cooldown, requires trend rules live), F (rejection alerts, live) and C in
shadow only; every feature has its own switch in `trend_rules.json`. Broker-stop live placement
deliberately not built: an SL fill KADIMA does not reconcile could leave a phantom open position
or trigger a second sell. Left for Codex with the reconciliation design and a 1-lot test.

## 2026-10-21 — Rule 2 (gamma only with the day trend) live on its own

**Trigger:** 21-Oct a MIDCPNIFTY 13600 CE Gamma Blast went in against a falling market (144 -> 39)
because rule 2 was shadow. Trader (01:04 IST 22-Oct): switch it to live.

**Change (branch `claude/trend-rule2-live-20261021`):** new switch `rule2` in trend_rules.json. With
`rule2=live` and `mode=shadow`, a Gamma Blast against its index's day trend is refused
(`counter_trend_<up|down>_day`); SIDEWAYS allows both sides; band stop and min-risk entry (rules 3-4),
re-entry, monitor and broker stop stay shadow. An index whose own candles cannot vote (no previous
close or day open, e.g. a GAMMA_EXTRA index) takes the NIFTY day trend instead of failing open
(`trend_source` in the trend_rules_entry audit line). `mode=live` still switches all four rules.
Deploy: `deploy/deploy_trend_rule2_live_20261021.sh` (hash-guarded on the 19-Oct trend_rules.py
a33bb6dc, market-hours refusal, backup, auto-rollback on failed health; `--shadow` flips back in 30 s).

## 2026-10-22 — Gamma red-reduction: R2 opening blackout, R3 exhaustion block, threshold 50

**Trigger:** 21-Oct audit — 44 Gamma Blast suggestions: 11 green, 13 red, 20 neither; none auto-traded.
Reds were counter-trend (7), a late chase right after earlier PEs hit +100% (3), and midday no-follow-through (3).
Every green scored 51–58.6, so `below_threshold` (setting 60) blocked all of them. Analysis doc:
"Gamma Blast Red-Reduction & Auto-Qualify Approach" (Claude doc).

**Trader decisions (01:19 IST 22-Oct):** live straight away (no shadow phase); R2 cut-off 09:45; R3 window
30 min after +50%; Celestial threshold 50; `volume_unconfirmed` a note, not a blocker; after 30 resolved
outcomes, roll back any rule that loses more than 10% of greens.

**Found on live EC2 (read-only, SSM):** `trend_rules.json` mode=live, so R1 (counter-trend gamma) was already
live. `celestial.THRESHOLDS` accepted 60 (added 20-Oct) but `trade_policy.eligible` only accepted 65–90, so with
threshold 60 no Celestial gamma could ever pass eligibility — fixed here (both now accept 50–90). Live config
had auto_trade_enabled=false at night (session scheduler arms it). On 21-Oct no gamma reached the executor;
capital_guard also failed closed 29× (broker margin unreadable) and equity read as unavailable 1,746×.

**Change (branch `claude/gamma-red-reduction-20261022`):** `trend_rules.py` gains switches `open_blackout`
and `exhaustion` (off|shadow|live, independent of `mode`), checked first in `entry_gate`; R3 reads
`trade_audit_plans` (strategy GAMMA, same underlying+side, T1/T2 observed in the last 30 min) and fails open
on error. `celestial.py`: THRESHOLDS 50–90, volume → `row['notes']`. `trade_policy.py`: minimum set matches.
Applies to manual Celestial buys too (they share `entry_gate`). Deploy: `deploy/deploy_gamma_red_reduction_20261022.sh`
(hash-guarded on live celestial f70ae5fc / trade_policy bf3c3201 / trend_rules a33bb6dc or rule2 8481cb3a,
market-hours refusal, backups, auto-rollback; `--shadow`, `--rollback` restores threshold 60). 10 tests.

## 2026-10-22 — Index scalping disabled (gamma-first)

**Trader (01:50 IST):** disable scalping. Scalping netted ≈ +₹480 over 16 trades 14–21 Oct; gamma is the focus.
The shared `KADIMA_FAST_MOMENTUM_LIVE` flag also gates move capture, gap-and-go, stock fast and MCX, so the
trader chose a scalp-only switch: `scalp_v2.json` `"entries": "off|shadow|live"` (non-live adds blocker
`scalp_entries_off`; scalp_v2 stays mode=live so v1 rows stay undispatched; signals still audited).
`scalp_v2.py` is not in the repo; `deploy/disable_scalping_20261022.sh` patches the live file in place,
hash-guarded on bc0ed647, with `--enable` / `--disable` (no restart) and `--rollback`.

## 2026-10-22 — Step 0: capital_guard rate-limit cache and single-count headroom

**Trigger:** 21-Oct audit — 29 entries refused `broker_margin_unreadable_failing_closed` (Angel One
"Access denied because of exceeding access rate"; each entry called rmsLimit 3–4 times, no cache), and
`capital_ceiling_reached_deployed_12275_equity_8478`: available cash is already net of open premium but
`max_affordable_lots` subtracted deployed capital again. Trader approved install before the 22-Oct open.

**Change:** `broker_available_funds()` is one cached reader (fresh 5 s; on failure last-good ≤60 s minus
`reserve_funds()` reservations; else one retry after 1 s; else None). Headroom = buffered cash. Each allowed
entry reserves its cost until the next fresh read. 9 tests. Deploy: `deploy/deploy_capital_guard_step0_20261022.sh`
(hash-guarded 5141f361 → a6b23bcf, auto-rollback, `--rollback`).

**Also decided 02:01–02:03 (core-hold build, not yet coded):** 09:30 entry for a core trade on a 4-of-4 early
trend (09:45 otherwise); core re-entries exempt from R3; ₹10,000 daily loss limit kept; core hold goes live
straight away; a profitable open trade is exited for a new gamma only if the new trade's projected profit is
higher than the open trade's remaining projected profit.

## 2026-10-24 — Sniper mode: few, early, with-trend, confirmed Gamma Blasts (live, 1 lot)

**Trigger:** 23-Oct audit + trader (22:42 IST): "only few but highly profit making and less risky
trades". 21–23 Oct replay: all Gamma T2s came before 10:30, 86% with the day trend, none on the first
signal of its index/side; 81% of scalp stops followed an earlier stop on the same side; after 10:30
with-trend PE scalps stopped 30 of 34. All gamma suggestions: 47 trades −₹16,228.

**Trader decisions (01:33 IST 24-Oct):** sideways day = no Sniper trade; a 2nd trade is allowed after
a loss (other index/side); sizing: skip when 1-lot stop risk > 15% of available cash, more capital to
be tried, keep the level-based (band) stop; go live directly (no shadow phase).

**Rules (kadima/sniper.py, DATA_DIR/sniper.json mode off|shadow|live):** auto Gamma Blast only;
09:45–10:30; trend UP/DOWN and side agrees; another contract on the same index+side suggested ≥60 s
earlier; premium ≤ +15% over this contract's first suggestion; no observed stop on the side today;
max 2 entries/day, next after a loss on another index/side, one open per index+side; 1 lot; stop risk
≤ 15% of broker cash (unreadable cash = no trade); no-progress exit if not +10% within 20 min. Band
stop with 35% hard cap and the existing trail unchanged. OI/direction flags become notes while live
(tradeable + clean spread still required). Manual Celestial buys untouched. Live errors fail closed.

**Deviation flagged to the trader:** Sniper trades are exempt from R3 (30-min exhaustion) and from the
band-half pullback wait (`exempt_r3`, `exempt_pullback` switches). With R3 kept, the replay took 1 trade
(+₹4,091) instead of 3 (+₹7,783 at ₹22k cash; 5 trades +₹20,629 at ₹1 lakh). spot_beyond_band_stop
still blocks.

**Deploy:** `deploy/deploy_sniper_20261024.sh` (outside market hours; git-fetched, hash-verified,
anchor-based installer `tools/install_sniper_20261024.py`, backups, auto-rollback; `--shadow`, `--off`
flip within 30 s, `--rollback`). 13 tests (`tools/test_sniper_20261024.py`).

## 2026-10-25 — Sniper: A + B filter, FINNIFTY excluded, install in shadow

**Trigger:** 6-month test (24-Apr..23-Oct-2026, 09:45–10:30 entries, proxy option pricing, 1 lot,
₹60/trade charges) on NIFTY/BANKNIFTY/FINNIFTY/SENSEX. All 4 unfiltered: 409 trades −₹37.2k; A+B (B≥0.2%):
144 trades +₹23.7k; A+B (B≥0.3%): 73 trades, 47% win, +₹30.6k. FINNIFTY lost under every filter.
Almost all profit came in October (A+B ≈ +₹64.7k Oct vs −₹32.4k Apr–Sep). Trader confirmed "yes"
to stricter A+B, drop FINNIFTY, 1 lot, shadow first.

**Rules added (rule 1b, 3b in kadima/sniper.py):** `indices` allow-list (default NIFTY,BANKNIFTY,SENSEX);
A = spot within `a_pct` 0.1% of the day high (CE) / low (PE); B = index move ≥ `b_pct` 0.3% in the
trade direction vs the close 15 min earlier. Both from the engine's own closed 5-min index candles
(`hourly_forecast.history_rows`) + live spot; <3 candles today, last candle >10 min old or no spot
= `sniper_ab_no_data` (blocks; fails closed). Gate audit lines now carry spot, away_pct, move15_pct.
`require_second_signal` switch added for rule 4 (kept ON; the 6-month test did not include rule 4,
so live trade counts will be lower than the test).

**Deploy:** same script, now installs with mode=shadow; `--live` flips to live within 30 s. 17 tests.
## 2026-10-25 — Gamma CONFIRMED rule + gamma unblock (installed with Sniper)

**Trader (19:42–20:00 IST):** a Celestial gamma strike is CONFIRMED only when score >= threshold (50),
strike concentration HHI >= 0.09 and 09:45–10:00; only CONFIRMED may auto-trade. Fix whatever else blocks
gamma auto entries. Replay 21–23 Oct: 18 such suggestions, 14 T1 / 0 SL. HHI >= 0.09 effectively selects
MIDCPNIFTY / BANKEX / BANKNIFTY (NIFTY and SENSEX sit near 0.07).

**Bottlenecks found:** (1) 12 of the 14 winners refused `contract_not_tradeable` only because they were one
strike OTM (Celestial allowed OTM only at score >= 90) — fixed: one strike OTM is tradeable. (2) no confirmed
5-min swing on a clean one-way fall (`fresh_structure_or_valid_expiry_unavailable`) — fixed: gamma uses the
trend band stop level, 35% cap kept. (3) band-half pullback wait and R3 — waived for Sniper trades (Sniper).
(4) risk per trade capped at 15% of min(cash, 62,000) (`risk_budget`, Sniper rule 7): at ~₹22k cash every
1-lot MIDCPNIFTY/BANKEX trade (risk ₹4.5–8.8k) was refused. Trader 20:10 IST chose 30% for auto gamma only:
`gamma_confirm.json` risk_fraction 0.30 scales the gamma allocation; Sniper `max_risk_cash_pct` 30 checks it on
real broker cash; capital_guard 1-lot headroom unchanged. Other kinds stay at 15%.
**Also decided 20:10 IST:** Sniper LIVE from 26-Oct (overrides the same day's shadow-first choice) and
Sniper `indices` = NIFTY,BANKNIFTY,SENSEX,MIDCPNIFTY,BANKEX (the CONFIRMED winners are mostly MIDCPNIFTY/BANKEX).

**Change (branch `claude/gamma-confirm-20261025`, on top of Sniper):** `kadima/gamma_confirm.py` +
`tools/install_gamma_unblock_20261025.py` (anchor-based, --check/--rollback) + deploy script. Switch:
`gamma_confirm.json` mode off|shadow|live. Manual Celestial buys unchanged.

## 2026-10-25 — Loss controls for the 30% gamma risk setting

**Trader (20:29 IST, "recommendation please"):** went with the recommendation. (1) Daily loss limit =
min(₹10,000, 30% of the morning's broker cash), floor ₹2,000, fixed ₹10,000 if cash is unreadable; morning cash
persisted in `daily_cash.json` so a mid-day restart keeps it. 15% was not used: portfolio sizing also checks the
remaining daily room, so a ₹3,300 limit would block every MIDCPNIFTY/BANKEX gamma (same as undoing 30%).
(2) Sniper rule 6b: after any losing Sniper trade, no more Sniper entries that day (`stop_after_loss`).
(3) 30% gamma risk kept. Worst realistic day ≈ −₹7–8k at ₹22k cash (was ≈ −₹12–15k).
CAS/MCX keep the fixed ₹10,000 (the new limit is never higher). Files: `kadima/daily_limit.py`,
`tools/install_loss_controls_20261025.py`, `deploy/deploy_loss_controls_20261025.sh`. Switch: `daily_limit.json`
mode off.

## 2026-10-25 — Dashboard shows the new rules; workbook marks CONFIRMED

**Trader (20:38 IST):** summary doc, workbook update, dashboard update — build and deploy. Celestial now shows a
read-only rules strip above the Gamma Blast table (Gamma CONFIRMED / Sniper / Scalp v2 settings, daily loss
limit, realised P&L, room left, auto-trade armed/paused), strike concentration under each tier, and a green
outline on CONFIRMED rows. Data: `risk_controls` in the Celestial top-20 response (`control_api.py`).
Files: `tools/install_dashboard_rules_20261025.py`, `deploy/deploy_dashboard_rules_20261025.sh`.

## 2026-10-25 (night) — CONFIRM v2: tighter CONFIRMED rules (trader confirmed 21:10 IST)
- Evidence: every gamma / Scalp v2 suggestion replayed on minute prices (21-23 Oct, the only
  days the price history still holds) through the live exits (stop, 15% trail ladder, Sniper
  20-min no-progress, EOD). Unfiltered: gamma 60 trades avg +1%, scalp 123 trades avg -0.5%.
- Gamma CONFIRMED v2: score >= threshold, index direction (Bull Shield) agrees with the option
  side, 09:45-10:10 IST. HHI no longer required (did not separate winners; it blocked the
  21-Oct SENSEX +75% run). Replay 9 trades, 9 wins, avg +27% (v1: 5 trades, avg +23%).
- Scalp v2 CONFIRMED: score 80-90 (was 70-90), 09:45-10:30. Replay 9 trades, 89% win, avg +18%
  (70-90: 11 trades, 73%, +13%).
- Exits unchanged: wider trails (30% / 50% / stop-or-EOD) were worse on the full set.
- Caveat: 3 trading days, all falling markets (every gamma pick was a PE). Review after 5 sessions.
- Install: deploy/deploy_confirm_v2_20261025.sh (rollback: --rollback).

## 2026-10-25 (night) — GIFT NIFTY v1: GIFT Nifty beside crude oil (display only)
- Broker contract master has no NSE IX instruments; source = nseindia.com marketStatus "giftnifty".
- Stand-alone worker kadima-gift-nifty (stdlib, 64 MB cap) -> state/gift_nifty.json; 1 call/min in
  GIFT hours (Mon 06:25 - Sat 02:50 IST), every 30 min otherwise, back-off on failures.
- control_api /macro_snapshot adds gift_nifty; dashboard panel "Crude Oil & GIFT Nifty".
- Not used by any score, gate or trade. Install: deploy/deploy_gift_nifty_20261025.sh (--rollback).

## 2026-10-25 (night) — MARKET CONTEXT v1: crude / VIX / FII-DII / news back, light
- kadima-macro-watch was disabled 2026-10-20 18:16 during a kernel OOM event (220 MB peak:
  full contract-master parse). Replaced by kadima-market-context: scans the cached master in
  1 MB pieces for CRUDEOIL MCX + India VIX rows only; same macro_snapshot.json output.
- Hard cap MemoryMax=128M (MemoryHigh 96M), Nice 10. Display only.
- Broker credentials moved from the old unit's Environment= lines to /etc/kadima/market-context.env
  (root 600); old unit keeps EnvironmentFile and stays disabled. Backup /root/...bak-20261025.
- Install: deploy/deploy_market_context_20261025.sh (--rollback).

## 2026-10-25 22:12 IST — BOTTLENECK FIX v1 (trader: "fix them immediately")
- Replay 21-23 Oct: 42 CONFIRMED v2 gamma rows; Sniper (day trend, A+B, second signal) and the old
  VWAP+EMA+RSI alignment check refused all of 21 and 22 Oct (incl. SENSEX +75%).
- CONFIRMED auto trades now skip: trade_policy.eligible, Sniper day-trend / A+B / second-signal,
  trend_rules counter-trend-day block; CONFIRMED scalps skip counter_trend_scalp.
- Kept: stops, trail, daily loss limit, Sniper stop-after-loss / side-stopped / max 2 / 1 lot /
  30% cash risk / 15% chase, spot_beyond_band_stop, clean spread, tradeable, capital checks.
- Switch: sniper.json "confirmed_bypass": false. Install: deploy/deploy_bottleneck_fix_20261025.sh

## 2026-10-26 08:49 IST — CONFIRMED BLINK v1 (dashboard only)
- Only CONFIRMED Gamma rows blink (green); score>50 rows steady light yellow; banner steady.
- tools/install_confirmed_blink_20261026.py; backup dashboard.html.bak-blink-20261026.

## 2026-10-26 15:36 IST — BEST ENTRY v1 (trader: "entry and exit to be fixed immediately", "best possible entry")
- Incident: NIFTY 27-Oct 22450 CE bought 182.10, sold 172.30 (-5.4%, -Rs 637) ~10:14; ran to 355.95.
- Entry (auto gamma / shield): signal arms the contract; buy only after a pullback of
  max(0.5 x 1-min ATR, 1.5%) that holds (bounce 0.25 ATR, not > 0.5 ATR above zone, no spike candle).
  No dip in 3 min -> half size (1 lot stays 1); arm expires at 6 min. Delta > 0.75 skipped.
- Stop: lowest of engine stop / pullback low - 0.25 ATR / entry - 5-min range, 35% hard cap.
- Exit (loss side only): 10-min grace (hard cap still exits); then stop exits need a 1-min close
  below the stop or a 0.5 ATR breach; signal exits must persist 60 s. Profit side, EOD, daily loss
  limit unchanged. Trader chose half size when no pullback comes.
- Deferred: entry-limit re-pricing every 2-3 s (existing 30 s cancel kept); forecast-band dip level.
- Config DATA_DIR/best_entry.json mode off|shadow|live. tools/install_best_entry_20261026.py (--rollback).
- 16:13 IST amendment (trader: "entry shouldn't be missed"): no signal is skipped by Best Entry.
  At 6 min it buys half size even on a spike; delta > 0.75 is a note only. Unfilled auto BUY
  orders are walked up to LTP + 1% every 5 s (max +4% over the first limit, up to 90 s) instead
  of being cancelled at 30 s. Only a signal that stops being eligible while waiting is not bought.

## 2026-10-27 00:20 IST — BEST ENTRY v1 (extended) + MANUAL FORECAST STOP v1 (trader 23:58)
- Trader: threshold 50; tomorrow's expiry live; manual trades get a counter-trend warning and a
  dynamic stop that "can forecast and decide" (no fixed rupee cap).
- 26-Oct audit (127 rows): only KADIMA trade was Scalping v2 NIFTY 22450 CE (09:45:57 @182.10,
  thesis exit scalp_v2_premium_lower_high @172.30 after 130 s); 22650 PE 6 lots was manual
  (manual_hard_stop_breached 14:09, -Rs 9,997).
- Best Entry now also covers fast_momentum (Scalping v2): pullback entry; 3-min grace on scalp
  loss exits (scalp +20% target never held).
- Sniper rule 5 (side stopped) counts Gamma plans only: the scalp's 09:48 stop blocked NIFTY CE
  Gamma at 10:00:54 / 10:01:21 on a +153 day.
- Manual forecast stop: exits a manual trade before the 35% hard stop only when loss >= 12%,
  the 10-min premium slope projects the 35% level within 30 min, premium < 1-min EMA9 for 2
  candles, the day trend is against it, and VWAP / hourly forecast agree when available.
- Celestial score_threshold set to 50 (dashboard CONFIRMED and executor now agree).

## 2026-10-27 00:30 IST — PEAK RUNNER v1 (trader: replace the fixed +20% scalp target)
- "amend it as trailing stoploss of 20.00 profit"; "it should continuously forecast ... once the
  peak is reached as per forecast of latest 15 seconds, it should exit".
- Scalping v2 at +20%: no fixed target; floor stop at entry +20%; wide backstop (keep 50% of the
  gain); exit when the 15-s premium slope forecasts no new high (2 consecutive checks, slope < 0,
  premium >= noise below peak). Thesis exits skipped while running; 2-s monitor while a runner is open.
- Replay 26-Oct 22450 CE (entry ~165): exit ~306 at 09:58 (+Rs 9,179) vs ~198 (+Rs 2,145).
- scalp_runner.json mode off|shadow|live; tools/install_peak_runner_20261027.py (--rollback).

## 2026-10-27 00:35 IST — SCALP NO-CAP v1 (trader: "there is no cap on capital for such trades"; ok 00:26)
- Scalping v2 only: capital = all available cash minus 5% buffer (was <= 30,000); no 2-lot limit;
  no 15% stop-risk lot limit. Kept: real cash checks and the daily loss limit as the cap on total
  stop risk (portfolio_capacity). Gamma / Sniper / Shield / CAS / manual unchanged.
- 26-Oct example: 3 lots of the 22450 CE instead of 1. scalp_capital.json mode off|shadow|live.

## 2026-10-26 (evening) — CONFIRM v3: HHI back for gamma, scalp back to 70-90 (trader confirmed)
- Gamma CONFIRMED v3: score >= threshold, index direction agrees, 09:45-10:10 IST, AND strike
  concentration HHI >= 0.09. Evidence (kadima-audit workbook 21-26 Oct, fixed T1/SL levels,
  09:45-10:10, score >= 50): HHI >= 0.09 17 of 17 hit T1; below 0.09 14 of 27.
  Note this differs from the v2 minute-price replay with live exits, where HHI did not separate
  winners; the trader chose the stricter rule. Expect fewer gamma trades.
- Scalp v2 CONFIRMED: score 70-90 (was 80-90), 09:45-10:30. Audit 09:45-10:30: 70-80 9 of 12 hit
  T2 (avg +12.9%), 80-90 11 of 13 (+15.0%); 70-80 still positive, roughly doubles the trades.
- Caveat: 4 trading days. Review after 5 live sessions.
- Install: deploy/deploy_confirm_v3_20261026.sh (rollback: --rollback). HHI set in both the module
  default and DATA_DIR/gamma_confirm.json so a missing config cannot drop the check.

## 2026-10-27 01:00 IST — STOCK BLAST v1 (trader)
- Trigger: 26-Oct ATHERENERG ran 1414 -> 1612 (14:26-14:54); Move Capture fired CE score 89.5 at 14:26:37
  and was rejected "move_capture_no_liquid_option" on every attempt; 1400 CE went 10.85 -> 215.
  Since 1 Oct: ~90,000 stock triggers, zero stock orders (COFORGE, PATANJALI, POLICYBZR the same).
- Decided: stock-specific strike rules (spread <= 6%, blast <= 8%; delta 0.25-0.85; blast score >= 80 also
  cheap OTM Rs 2-30), 4 strikes each side, entries until 15:10 ("no time bar"), sizing by trigger confidence
  (Claude's call: <75 40%, 75-85 70%, >=85 100% of the existing allocation), exit = existing PEAK RUNNER
  (-35% hard stop, +12% locks +3%, runner from +20%), live from 27-Oct open, per-strike rejection log.
- Index engines unchanged. Off switch: DATA_DIR/stock_blast.json {"mode":"off"}; installer --rollback.

## 2026-10-27 01:30 IST — STOCK BLAST v1.1 (trader: "go ahead")
- Move Capture hot list 10 -> 20; triggers scoring >= 80 are prepared before the rotation.
- Historical candles (stock_fast, stock_execution, gap_live) share one pacer (<= 2.5/s, <= 150/min) with one
  retry on "Too many requests"; rejection logs now carry the real error class and a cleaned message.
- Far strikes (trader approved after 27-Oct ATHER 1580/1600 CE ~34x from previous close): on triggers >= 88,
  a second small order on the best strike > 8% out or under Rs 2 (to 15% out, Rs 0.50 floor, spread <= 20%),
  sized at 20% of the confidence size. The primary order is unchanged.
- Daily Missed Blasts report (read-only): 15:45 IST, contracts that rose >= 3x and what KADIMA did;
  page /missed/ on the dashboard host, JSON pushed with the daily audit.

## 2026-10-27 02:10 IST — STOCK BLAST v1.2 loser filters (trader: "reduce the losers", then "yes")
- From the 26-Oct replay (381 trades, 178 losers): stock entries now need the stock up >= 0.2% in the trade
  direction over the last 5 min; no entry if the option already rose >= 10% in the last 3 min; main premium
  >= Rs 3; strike no more than 0.5% in the money; at most 3 trades per stock per day.
- Replay effect on 26-Oct: 83 trades, 22 losers (was 178), losses -Rs 15k (was -Rs 172k), net P&L about the same.
- One-day fit: every block is logged (STOCK_BLAST_FILTER_BLOCK) to re-check against live days.

## 2026-10-27 08:36 IST — FOCUS LIST v1 (trader: results day, "keep a special focus", "should support F&O universe scanner")
- DATA_DIR/focus_symbols.json: today's results names (LT, MARUTI, BAJAJ-AUTO, TATASTEEL, SBILIFE, CIPLA, INDUSTOWER,
  MAZDOCK, MFSL, PGEL, RADICO, SAIL, WAAREEENER). Focus names: never evicted from the Move Capture hot list,
  trigger at score 60 (others 65), prepared first, up to 5 stock trades/day (others 3), always on the universe
  scanner's gamma shortlist (cap 20 + focus count). Loser filters, loss limit and exits unchanged.

## 2026-10-27 09:14 IST — TREND GRACE v1 (trader confirmed), on top of CONFIRM v3 + BEST ENTRY v1
- 26-Oct review: Shield read PE on an up day, so the 09:45-10:10 CE gamma winners were not
  CONFIRMED; CONFIRMED scalps were sold by noise within minutes, then ran +44%..+102%.
- Gamma CONFIRMED direction: Shield agrees OR day trend agrees (index >= 0.3% from open and on
  the same side of VWAP). HHI 0.09, score, window unchanged. Replay 21/22/23/26 Oct (v3):
  4 -> 5 trades, all wins (+122% -> +143%). Switch gamma_confirm.json "trend_confirm".
- Scalp: BEST ENTRY scalp grace 3 -> 5 min (best_entry.json), v2 no-progress exit 5 -> 10 min.
  Replay (BEST ENTRY loss rules): +134% -> +249%, 60% -> 80% wins, 26-Oct -32% -> +101%.
- Not adopted (tested, no benefit): day-trend filter on scalps, no-chase after a 30-min burst.
- Install: deploy/deploy_trend_grace_20261027.sh (--rollback). After 15:45 IST.

## 2026-10-28 00:03 IST — Sniper daily cap removed (trader)
- sniper.json "max_trades": 999 (was the default 2). Backup sniper.json.bak-maxtrades-20261028.
- Still in force: 1 lot, risk <= 30% of cash, stop after a losing Sniper trade, 09:45-10:30, daily loss limit.
