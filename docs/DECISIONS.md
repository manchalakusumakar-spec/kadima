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
