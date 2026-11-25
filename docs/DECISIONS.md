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

## 2026-11-13 — Day forecast anchored on the 09:20 close (installed)

**Decision (trader):** "deploy". Installed 08:36 IST pre-market, after a 4-day replay that did not
meet the agreed bar (path error 138 -> 95 better; end error 189 -> 223 and direction 1/4 -> 0/4
worse). The trader chose to deploy anyway. The 09:15 day path is provisional; the verified 09:20 close re-anchors the
official, scored path; fast reforecast (one candle >= max(30, half the 09:15 range)) until 10:15.
Switches `ANCHOR_0920` / `FAST_REFORECAST` in `kadima/day_forecast.py`. PR #65. Backup and
rollback: `deploy/release-20261113-day-anchor/deploy_day_anchor_20261113.sh --rollback`.
Watch: end error and direction in the nightly report; the 09:15 copy is in `day_forecast_provisional`.

## 2026-11-20 — Day forecast: FLAT is "no direction call" (prepared, NOT installed)

**Trigger:** day forecast FLAT on 9 sessions in a row; 19-Nov (+315) and 20-Nov (+345) missed.
**Check first (474 NIFTY days, Oct-2024 to 20-Nov):** the day move so far (09:20 ... 14:15), gap,
first candle, previous day and previous 3 days do not predict the rest of the day (all |t| < 2,
direction hits about 50%; recent months lean slightly to reversal). A trend-carry change was
therefore NOT built: it would fit 19/20-Nov and lose on average.
**Finding:** the live day model said FLAT on all 20 scored days; FLAT counted as a hit only when
the day moved < 0.10% (2 of 20). The hourly model's UP/DOWN calls hit 35/54 (65%); FLAT 42/145.
**Decision (trader):** "Score day FLAT fairly" - a FLAT day call is no direction call (dir_hit NULL),
old rule kept as flat_rule_hit, band/path/end error unchanged. Switch `SCORE_FLAT_AS_NO_CALL`.
Replay on a copy of forecast.db: 20 days re-scored, path/band/end error identical on all 20.
Deploy: `deploy/release-20261120-day-flat/deploy_day_flat_no_call_20261120.sh` (install on YES).

## 2026-11-26 — NSE data for the forecaster: India VIX sizes the 09:15 slot (installed)

**Request (trader, 25-Nov night):** "use the data live from NSE and improve the accuracy"; data chosen:
NSE option chain, India VIX + GIFT Nifty, breadth, FII/DII + participant OI; rollout: "install if replay is better".
**Access:** NSE's live website APIs refuse the server (HTTP 403, cloud IPs blocked); NSE archives work.
The live NSE option chain / FII-DII API therefore cannot be used from EC2 (needs the PC or a proxy).
GIFT Nifty has no history to test on (live state file only).
**Tested (525 sessions, Oct-2024..25-Nov):** India VIX predicts the day's range (r 0.41 vs 0.32 for the
previous day's range). Participant OI (FII/DII/PRO/Client), FII options, advances/declines: no reliable
direction signal (no |t| > 2.5 stable in both halves except noise) -> stored only, not used.
**Change:** `kadima/nse_daily.py` stores NSE archive data nightly (table nse_daily). The 09:15 hourly slot
(made at 09:15:04, no chain IV yet) uses 0.9 x India VIX as its IV instead of the fixed 1.4x widening.
Replay: interval score -4% on 250 sessions (both halves), -9% on 29 sessions through the live code; hit
rate of the band same (74.7% vs 73.9%), band ~10% narrower, direction 6 vs 5 of 29. 25-Nov alone was
worse (band wider than needed). Day forecast and later slots unchanged (VIX widened them too much).
Switches: `KADIMA_VIX_OPEN=0` (off), `KADIMA_VIX_IV_SCALE`. Rollback:
`deploy/release-20261126-nse-vix/deploy_nse_vix_20261126.sh --rollback`.
**Next candidate (not done):** later slots blend ATM IV 50/50 and over-cover (Nov live 76% vs ~69% target).
