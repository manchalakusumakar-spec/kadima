# Persistent trade audit

This release adds a read-only audit table to `/dashboard.html`. It is stacked on
`codex/momentum-recovery` at `bd294a7048749258fbaa2cc786f20a441a2a8073`.
Publishing this source does not deploy it to AWS or enable trading.

## Daily Price Memory

The dashboard now includes a separate persistent price table, independent of
whether Gamma or a price-movement strategy has suggested a trade. It records
existing stock FULL quotes, received index callbacks, the exchange-timestamped
Move Capture spot feed, accepted option-chain polls and the bounded retained
contract watch. It adds no quote requests or order calls. The reviewed AWS
socket feed and FIFO limiter are unchanged.

For each IST date and exchange/token/contract identity it saves:

- Broker-reported opening price and previous close, when a fresh current-session
  exchange timestamp is supplied. The timestamp is when the reference was
  reported, not a fabricated opening-trade time. Missing exchange timestamps
  permit receipt-labelled samples but cannot establish an opening reference.
- First observed price/time, latest sample, observed high/low and their first
  observed times. A late start never labels its first sample as market open.
- Sampled one-minute first/high/low/last prices, sample counts, cumulative-volume
  endpoints and reset/gap evidence. No synthetic minutes or inferred wicks.

Saved references calculate movement from opening, first observation and previous
close, plus opening gap. Minute history calculates 1/3/5/15-minute sampled
close-to-close returns only with all intervening minutes and no observed gap
over 30 seconds. The response includes the return's as-of time. These historical
features are available for review/replay; live strategy gates still require their
existing fresh evidence and warmup after a restart. Historical data never
restores an old entry permission.

Conflicting opening/previous-close values are flagged and suppress percentages
using those references. Identity includes the full mapped contract symbol so
venues, expiries and reused tokens cannot silently share a baseline. Observed
extremes are not certified exchange session highs/lows. Recorded sample counts
count received observations, not trades; two feeds may observe the same trade.

`price_history.db` lives beside the engine data under `KADIMA_DATA_DIR`. It is
separate from the execution database. Feed callbacks only coalesce observations
into bounded memory; a background writer batches every two seconds using WAL,
FULL synchronous commits and SQLite write transactions. The pending buffer is
capped at 4,096 instrument/minute groups per process (plus one bounded in-flight
batch). Storage failures requeue within that bound and expose error/drop counts.
Disk exhaustion never blocks an order exit. A hard kill can lose the uncommitted
buffer; restart recovery preserves committed references, not unseen samples.

Retention is seven calendar days for minute records and 180 calendar days for
daily references. The active database budget is 1 GiB with at least 256 MiB free
space required. Reclaimed pages are reused; retention does not shrink the file.
The health response reports writer failures, drops and last successful commits.
These bounds control growth; actual AWS memory, disk and write latency still
require host observation. Setting `KADIMA_PRICE_HISTORY_DISABLED=1` disables
background capture for offline tests; do not carry that test setting into the
production service environment.

Authenticated read-only endpoints are `/api/kadima/prices`,
`/api/kadima/prices/minutes?day=YYYY-MM-DD&key=...`, and
`/api/kadima/prices/export`. The cookie gateway exposes `prices`, `price-minutes`
and `price-export`. Date/type/symbol filtering and 50-row dashboard pages cover
all recorded instruments. Daily CSV uses one read snapshot, neutralizes formula
cells and refuses more than 10,000 records rather than silently truncating.

No unrecorded historical tape is backfilled. Existing feeds cover configured
underlyings and their selected option ladders, not every listed strike/expiry.
An instrument dropped from all subscriptions/polls becomes stale; its saved
reference remains. Broker FULL field mapping follows
[SmartAPI market-data documentation](https://smartapi.angelone.in/docs/MarketData)
and the pinned SmartWebSocketV2 SDK fields. Credentials are never recorded.

## Records and meaning

- Each first selected contract/strategy/episode/IST day freezes its original
  source plan in `trade_audit_plans`. Repeated polls update observations and
  blockers, not the original plan. Rows survive shortlist removal and restart.
- Capture covers every fresh Gamma lead with final score >=60 in each published
  scanner batch, including strikes outside the display's Top 20 and additional
  strikes for the same underlying. The score is a heuristic, not a probability.
  The display ranking and entry gates remain the existing rules. Capture also
  covers the first stock/index scalp entry plans,
  confirmed Shield signals, Move Capture contracts after option confirmation,
  selected Gap-and-Go and MCX contracts, and alerts from the existing CAS scanner
  when that scanner runs. This change does not start inactive scanners.
- Entry, explicit exit, stop, T1/T2 and maximum-hold policy are saved only when
  the source supplies them. Browser-only Gamma percentage scenarios are not
  execution plans. A missing value stays blank; target prices are not relabeled
  as an exit forecast, and maximum hold is not an expected duration.
- Actuals use `execution_orders` joined to incremental `execution_fills`, with
  quantity-weighted prices and realized P&L before fees. Acknowledgements are
  not fills. Paper fills are excluded and marked PAPER; mixed paper/live data
  is UNKNOWN. Existing adopted-position estimates are not invented BUY fills.
- Holding time runs from first BUY confirmation to last SELL confirmation for
  a closed position. The ledger has engine confirmation times, not exchange
  execution times. Partial/open positions show elapsed time separately.
- T1/T2/stop times are first observed fresh poll crossings after capture, bounded
  by the original maximum hold/session deadline when supplied. Stop or T2 ends
  the outcome sequence. These are sampled observations, not complete price
  paths, executable fills, or proof that a trade could have earned that return.
  Concurrent scanner writes preserve the first hits and highest observed price;
  older samples cannot replace a newer quote or reverse a terminal outcome.
- RECENT POLL means a sample was received within 30 seconds. It does not prove
  exchange quote freshness. Both freshness labels are red; T1 is light green,
  T2 dark green, stop red. Capture errors are process-local diagnostics; outages
  can leave gaps. Absence of a row does not prove absence of an opportunity.

The authenticated backend GET routes are `/api/kadima/audit` and
`/api/kadima/audit/export`; the existing cookie gateway exposes `audit` and
`audit-export`. Filters are IST date, strategy, execution status and contract/
underlying substring. Pages are capped at 200; CSV export uses a single SQLite
read snapshot and is capped at 5,000 rows. Narrow filters for larger days.
CSV strings are neutralized against spreadsheet formulas. Neither route polls
or writes to a broker. No execution gates, stop policy, score threshold, lot
size, broker transport, live flags or executor file are changed.

## Installation after source review

Use a separate extracted release directory. The default command is a plan,
with no broker login, service stop, source write or activation:

```bash
sudo -n python3 trade_audit_update/install.py --with-dashboard
```

The installer accepts only the pinned predecessor bytes or this exact release.
It also accepts the exact reviewed PR #20 audit predecessor; unknown edits
remain blocked. For a combined rollout, install PR #19 sources from its matching
release first, then this audit/history release while auto remains paused. Do not
use PR #19's combined `go_live.py` as the intermediate step: it requests live
activation before the audit/history upgrade. Any subsequent activation must use
the tooling and source manifest of the final installed release.
It preserves the reviewed stock-fast dashboard panel and routes when present.
Other dashboard edits and executor overlays must first be compared/composed;
unknown source is refused, not overwritten. Dependencies, credential files,
FIFO limiter and service identities are checked using the existing pinned
installer. The live executor must match the reviewed MCX composition and is
never an installation target. Use the matching deployment lineage; do not run
an older installer against files in this release.

Before an operator applies it, pause global auto, turn off stock live intent,
verify no local managed exposure or outstanding orders and review the source
plan. The installer repeats fresh broker exposure checks and preserves its
existing treatment of verified carry-forward positions. Broker checks are
read-only. Back up the SQLite database with SQLite's backup mechanism under the
site's maintenance procedure; do not copy a running WAL database as one file.

```bash
sudo -n python3 trade_audit_update/install.py --with-dashboard --broker-check
# After reviewing the plan and exposure checks:
sudo -n python3 trade_audit_update/install.py --with-dashboard --broker-check --apply
```

Apply uses the existing guarded service-stop/backup/source-check/restart flow
and checks the authenticated audit API after restart. It does not resume auto
or arm any lane. Before restart, failure restores replaced source; after a
restart attempt, source is retained for operator review because exposure may
have changed. Existing deployment rules still apply; this release does not
establish current AWS source identity or readiness.

Audit schema additions are lazy and additive. A source rollback may leave the
new audit tables intact; keep them for review. Never roll back execution tables
or a live database to undo a dashboard change.

## Historical import and verification

An explicit maintenance import is available as `trade_audit.backfill_positions`.
Run with the installed interpreter/configuration against the intended database
only after taking a backup. Process batches using `next_signal_id` until
`scanned` is zero. It only writes audit tables. It never submits or reconciles
orders. Exact contract-matched saved signals are labeled HISTORICAL_SIGNAL;
missing originals are HISTORICAL_ACTUALS_ONLY, with missing plans left blank.
There is no automatic import of historical unexecuted suggestions because
poll snapshots previously replaced them.

```python
from kadima import trade_audit
cursor = 0
while True:
    result = trade_audit.backfill_positions(limit=500, after_signal_id=cursor)
    if not result['scanned']:
        break
    cursor = result['next_signal_id']
```

Offline verification (broker transport is stubbed; dashboard traffic is fully
intercepted at `kadima.test`):

```bash
KADIMA_PRICE_HISTORY_DISABLED=1 python -m unittest discover -s tests -v
python -m compileall -q kadima dashboard_live tools trade_audit_update
node tests/test_celestial_dashboard.cjs
node tests/test_trade_audit_dashboard.cjs
node tests/test_price_history_dashboard.cjs
```

On the host, verify the installed commit/blob identities, authenticated API,
retained stock panel, mobile/desktop table, unchanged flags and source capture
on subsequent real scans. No test order is required for this audit deployment.
Check both audit and price-history APIs. After fresh session quotes arrive,
verify persisted opening/first-observed differences, writer commit times and
minute coverage, then verify a controlled restart preserves the same daily
references. Back up the separate history database with SQLite's backup API;
never roll back execution data to undo a history-display change.
