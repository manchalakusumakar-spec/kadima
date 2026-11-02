# Fast-entry evidence review — 6 October 2026

**Decision: PR #28 remains a draft. The accessible data supports an audit of
recorded outcomes, but does not support replaying the new filters or claiming
an improvement in net profitability.** No runtime change was deployed.

## Evidence and coverage

Fetched from the private `manchalakusumakar-spec/kadima-audit` repository at
`a045bb431a2ade6399a703d9e77d405dd0bec16b`:

- [Original CSV](https://github.com/manchalakusumakar-spec/kadima-audit/blob/a045bb431a2ade6399a703d9e77d405dd0bec16b/audit/2026-10-05.csv)
  (source blob `e83aaa81755991840fdc4fbd39b20e51a6fe8f76`).
- `reports/history.json`, `reports/report_2026-10-05.md`, `tools/analyze.py`
  and `ec2/SETUP.md` were inspected for definitions and provenance. These are
  derived from the same CSV, not independent corroboration.
- The only branch returned was `main`; its complete tree contained one audit
  CSV, for 5 October. No 6 October CSV, raw candle archive or broker book was
  present in that tree.
- The saved 29 September session-evidence file identifies itself as a manually
  transcribed field projection with omissions and mixed attribution. Its
  provenance does not establish a raw fast-scalping replay dataset.
- No callable authenticated AWS or Angel One history reader was available;
  targeted integration discovery did not find a usable provider.

The CSV contains 17 rows: 13 labelled Scalping/SUGGESTION and four labelled
Manual/PERSISTED_SIGNAL/CLOSED. All final quote statuses are STALE. Timestamps
are offset-aware IST; the CSV contains no export-generation timestamp or
deployed code/configuration identity. Fetching it today does not make it live.

## Independently recomputed findings

| Finding | Evidence | Interpretation |
| --- | --- | --- |
| First recorded event is SL | 9/13 suggestions (69.2%) | Recorded suggestion outcome; not an executed loss rate |
| T1 recorded before any recorded SL | 4/13 (30.8%) | Includes two later T2 touches, one T1-only row and one T1-then-SL row; not a fill or profitable exit |
| Any recorded SL | 10/13 | The extra row touched T1 first; do not collapse it into an SL-first case |
| SL recorded within four minutes | 5/13 suggestions | Suggests reviewing entry/stop placement, but candles are absent so the cause is unproven |
| Explicit original holding horizon | 5/13, all 15 minutes | These five have four SL-first and one T1-first event inside that horizon |
| Missing original holding horizon | 8/13 | Do not infer one from the strategy label |
| Missing immutable suggestion IDs | 13/13 | Distinct CSV rows are preserved; unique signal/episode counts are unknown |
| Closed records with `underlying_lost_vwap` | 4/4, all labelled Manual | The immediate diagnostic priority is adoption/entry-to-exit evidence, not assuming these were auto-scalp stop losses |
| Engine-reported P&L before fees | -387.50 INR across those four rows | No independent broker reconciliation; net P&L unavailable |
| Missing BUY time, actual entry and bought quantity | 4/4 closed rows | Actual holding times and entry/exit slippage cannot be verified |

The four differences between the suggestion/signal timestamp and last SELL
confirmation are about 8.3–9.4 seconds. The previous analyzer substitutes the
signal timestamp for the missing BUY time, then describes this as time held.
These differences are **signal-to-SELL-confirmation intervals**, not verified
entry-to-exit holding times. A manual position may have existed before adoption.

BANKNIFTY 54700 CE touched T1 after 79.5 minutes and SL after 195.7 minutes.
Its final SL label hides that order. Its original holding horizon is absent.
NIFTY 22500 PE touched T1 after 10.2 minutes and T2 after 20.3 minutes; SENSEX
72400 PE touched T1 after 19.2 minutes and T2 after 48.9 minutes. Those later
targets must not be described as proven 15-minute scalp successes.

As an explicitly **hypothetical 15-minute sensitivity window**, the 13 rows
contain eight SL-first records, two T1-first records and three with no recorded
level event inside the window. This is not the missing original plan, and
absence of an exported event does not establish complete quote coverage.

NIFTY 22500 CE appears twice, 31.585 seconds apart, with different entries,
scores and stops. Preserve both rows: missing signal/episode IDs prevent proving
they are erroneous duplicates. ICICIGI's final stale LTP exceeds T1 hours after
its early stop; this is not evidence of recovery during its 15-minute horizon.

## Code connection and limits

At the inspected KADIMA base, `celestial_exit.evaluate` can emit
`underlying_lost_vwap` on a fresh adverse spot/VWAP comparison, with no prior
crossing or completed-bar hold requirement for that branch. The fast entry lane
does not require Shield/VWAP alignment. This is a **possible entry/exit policy
mismatch** to investigate, not the established cause of the four Manual rows.
The deployed source, underlying observations and adoption/BUY chronology are
missing. Do not disable this protection based on this export.

PR #28 changes new fast entries and initial stop placement. It does not change
manual adoption or that VWAP exit. These findings therefore do not demonstrate
that PR #28 would have prevented the four reported exits.

## Replay readiness

| Required evidence | Available? | Consequence |
| --- | --- | --- |
| Suggestion prices, stops and observed event times | Yes, 13 rows | Descriptive audit only |
| Decision-time closed one- and five-minute underlying OHLC | No | Zero of 13 rows can be evaluated against PR #28's entry filters |
| Timestamped option bid/ask, volume, IV, contract master and subsequent path | No | Cannot model entry fills, premium-stop mapping or stop/target order faithfully |
| Immutable IDs and accepted/rejected candidate episodes | No for suggestions | Cannot quantify independent opportunities or filtered winners |
| Complete BUY/SELL broker fills, quantities and charges | No | Cannot reconcile actual net performance |
| Deployed source hashes and strategy/exit settings | No | GitHub main is not proof of the historical runtime |
| Independent chronological evaluation days | One CSV day only | No out-of-sample estimate; no threshold calibration |

The smallest next data handoff is the read-only KADIMA database export for
5 October (closed candles, signals/candidate payloads, option observations,
positions and execution orders/fills), that day's strategy/adoption/exit audit
events, deployed source/configuration identity, and Angel One's matching trade
book/contract note. If rejected candidates or quote paths were not retained,
historical replay for those decisions remains unavailable; collect them going
forward before tuning against outcomes.

Calculations are reproducible in `analysis/fast_entry_review_20261006.ipynb`
using the included newline-normalized evidence copy. The notebook cells were
executed sequentially in a plain Python interpreter and saved with outputs.
Jupyter/nbformat was unavailable; notebook-viewer rendering was not verified.
