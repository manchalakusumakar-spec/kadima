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
