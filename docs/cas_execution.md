# Separate CAS premium-awakening engine

The Shield confirmation rule remains unchanged. CAS has its own entry path before
the fast-momentum/Gamma dispatch on successful index option polls. CAS does not
require a Shield score, Shield confirmation hold, RSI threshold, OI velocity,
Greeks or the normal model-return gate. It does require fresh executable inputs,
underlying direction, account controls, available funds and reconciled positions.

## Reported motivating trade

The handoff describes SENSEX 74400 PE: 24 lots / 480 quantity, broker average
₹52.76 and exit ₹85.10, approximately ₹15,523.20 gross using the rounded average.
These are user-reported fills, not a reconstructed market replay. No timestamped
bid/ask, option VWAP or intervening quote history was provided. This change does
not establish that an automated order would have filled at ₹14–₹20 or reproduce
the reported P&L. The inexpensive early fills motivate the separate detector.

## Decisions and execution

| Stage | Meaning |
| --- | --- |
| CAS WATCH | Missing input, history warming up, or setup not yet executable. |
| CAS ENTRY | Premium awakening detected; dispatch still requires entry enable, account checks and a fresh quote. Broker acknowledgement is not a fill. |
| CAS ADD | The initial BUY is terminal with owned quantity; the same contract has continued expanding. One add attempt only. |
| CAS EXIT | An exit is requested or pending. Remaining quantity stays owned until confirmed SELL fills. |
| CAS LOCK | Session/underlying/side cannot re-enter, including after restart or strike changes. |

One CAS run per expiry date, underlying and CE/PE direction is persisted in SQLite.
A detected doubling from an observed cheap baseline also locks that side. A
reservation is persisted before the first BUY. A crash in the gap before submission
leaves a locked episode instead of a blind replay. Add reservation is also durable;
a failed add is not automatically retried. Locks roll naturally with the date;
quote history restarts empty after process restart.

## Initial settings

These are explicit, uncalibrated implementation settings in `cas_engine.Policy`,
not estimated win probabilities or backtested optimal thresholds.

| Check | Initial value |
| --- | --- |
| Contract | Its instrument-master expiry is today; ATM or one strike step away at entry |
| Cheap entry ask | ₹1–₹50; verified again at submission |
| History | At least 3 distinct source timestamps spanning 60 seconds; gap over 90 seconds or volume reset restarts warmup |
| Awakening | At least +3% per 30 seconds, normalized to the actual interval; increasing cumulative-volume increments |
| Option VWAP | Broker `avgPrice` required; below/at VWAP observed within 90 seconds, now above it |
| Underlying | CE above VWAP and EMA20; PE below both; fresh spot and technical snapshot |
| Spread/depth | Spread at most 1.5%; valid positive two-sided depth; displayed quantities cover the stage |
| Freshness | Option exchange feed time and spot within 30 seconds; technical snapshot within 240 seconds |
| Chase bounds | Entry ask no more than 1.35× recent low; submission price at most 1% above detected ask |
| Total buying capital | Maximum ₹15,000 across entry and add, further limited by available funds; not a loss allowance |
| Stage size | 1 lot first, up to 1 further lot; total capped by existing `MAX_LOTS_PER_SIGNAL` and episode budget |
| Add | Two rising samples, bid at least 1.15× initial limit and above average cost, ask no more than 1.50× initial limit |
| Initial stop | 5% below entry limit; after fills/adding, monitor also checks 5% below actual average |
| Trail | After +25%, retain 75% of peak gain; stops only tighten |
| Spike exit | After reaching 2× average entry, exit on 10% pullback from peak; reaching 2× alone does not cap profit |
| No expansion | Exit after 120 seconds if the peak never reached +5% |
| Entry / expiry lock | Entries from 09:15 to before 15:25 IST; request exit at 15:28 IST |

CAS has explicit pre-close times independent of the legacy 15:40 configuration;
they are strategy settings, not exchange calendar assertions. Actual expiry is
resolved from contract data rather than hardcoding a weekday or lot size.

The user confirmed ₹10,000–₹15,000 as total CAS buying capital. CAS caps its
allocation at ₹15,000, including the combined entry/add cost and estimated cost
reserve, while retaining lower limits from available funds and account controls.
This is a maximum budget, not a minimum investment or permitted loss. The existing
Shield allocation source is capped only inside CAS; Shield capital rules are unchanged.
The ceiling also applies when an older CAS run with a larger allocation is restored. Initial and added cost/risk together must fit the ORIGINAL CAS allocation
and existing 15% risk fraction, plus current account headroom and broker cash.
Neither a fresh allocation nor a new position slot is created for an add.

Existing rollout, dashboard auto pause, kill latch, kill switch, broker-order flag,
session cutoff, daily loss, pending-order and reconciliation checks remain in
force. Known news risk blocks entries. The dashboard's Shield score selector is
not a CAS probability/threshold control. `KADIMA_CAS_LIVE=1` is the separate entry
opt-in; default is observation. Pausing entries does not disable position exits.

## Feed and broker behavior

`OptionChain` retains `avgPrice`, `exchFeedTime` and top bid/ask quantities in the
completed snapshot. Missing fields disable CAS eligibility only; no LTP-derived
VWAP or assumed depth is substituted. The actual deployed FULL payload must be
checked for these fields/units and source timestamp format before activation.
The adapter accepts timezone-aware ISO time or the broker-style
`17-Sep-2026 14:00:00` format interpreted in IST. An old feed timestamp remains
stale even when a REST request succeeds.

CAS re-fetches FULL quotes before BUY and while monitoring its positions. BUY
prices are rounded upward on the existing transport's ₹0.10 grid and checked
against the price cap; SELL requests use a buffered executable bid. The existing
durable ledger handles partial fills, unknown submissions, cancellation and SELL
confirmation. Pending BUY exits latch a reason, cancel the remainder and wait for
terminal confirmation before selling the final owned quantity. Staged BUY fills
now compute the position's weighted average from incremental notional. A rejected
add keeps the existing position open.

REST polling and the existing 10-second monitor can miss or react late to brief
moves. There is no broker-hosted stop or guaranteed exit fill. With no fresh CAS
exit quote, expiry intent is persisted and a pending BUY is cancelled if possible;
an open position's SELL waits for a usable quote. Pending SELLs retain the existing
alert/reconciliation behavior and are not automatically repriced. Broker MIS rules,
actual feed fields and expiry session operation still require live validation.

## Output and release

Authenticated `/dashboard_data` includes `cas.mode` and `cas.candidates`; the existing
dashboard shows strike/direction, entry trigger, SL/invalidation, fast target zone,
trail/exit cue, block reason, IST timestamp and separate broker status/filled quantity.
Reference target zones (+25%/+50%) do not place profit-cap orders.

This branch is stacked on `codex/fast-momentum-observation` at `b506c918`. It does not
merge the independent PR #5 memory/service-control/CAS-observation changes. Use the
existing guarded `executor_fix/install.py` and dashboard upgrade tooling for this
specific source lineage. The manifests recognize the exact preceding source blobs;
unknown installed versions still stop. Credentials, database, existing flags and
Shield rules are preserved. No AWS deployment, trading activation or real broker
order is performed by this change. Live installation must verify the actual AWS
source and contract feed, reconciliation and account/session settings.

Offline checks exercise chronological CE/PE detection, source gaps, stale/future
quotes, post-move locking, partial fills, rejected/unknown submissions, weighted
average on adds, original-budget enforcement, decay protection and the actual exit
monitor. Browser fixtures contain no broker requests. Passing these checks does
not validate profitability or establish the earliest available fill.
