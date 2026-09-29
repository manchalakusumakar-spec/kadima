# CAS safety refinement — 30 September 2026

## Source and scope

Repository: `manchalakusumakar-spec/kadima`. Source baseline is
`43763e5d622a330c3f6d6770370fabcc30376dc5` on `cas-auto-execution`, after the
original CAS patch `10dfbd5fa0526654d3f7bb2da0ebbc5ecf4c59eb`. This is NOT a
claim about the build deployed during the 29 September trading session.
The source-capture commit `6d6aff7ef82ab6fec95c62e809a4e262059a550d` changed
only the review workflow, not runtime files.

No AWS command, broker login, live order, activation, risk-limit increase or
merge to main is part of this review. CAS P&L remains outside the Gamma review.

## Changes

CAS execution remains OFF by default. Both the existing global operator
permission and the explicit CAS flag must permit entry. Durable kill, file
kill, control revision, session calendar and lease are checked before
submission and every price increase. Installing code is not activation.

The account must have no other open position or unresolved order for CAS to
enter. External/manual exposure blocks CAS admission. An open CAS holding
also blocks new entries through the shared portfolio checker. The ordinary
non-CAS concurrency policy is otherwise unchanged. Without an entry stop,
CAS sizing reserves the full purchase premium at the cap plus a 1% modelling
reserve; existing daily realized losses reduce available risk. Profits do
not enlarge the loss allowance. This is not a guaranteed maximum realized loss.

The scanner retains the 25% premium / 3x incremental-volume trigger. It now
requires source exchange timestamps, a continuous observation history,
expiry-day contract identity and whole-lot bid/ask depth. Missing, stale,
future, repeated, out-of-order or volume-reset data cannot create an impulse.
History is contract/exchange/expiry keyed and bounded. Only contracts passing
tradeability checks compete on acceleration; depth/spread break ties. A
rejected dispatch is not permanently consumed, so a later fresh observation
can retry all gates. There is no claim of improved financial expectancy.

Entry starts at the bid rounded DOWN to the existing 0.1 transport grid,
not at a guaranteed lowest price. The loaded instrument's tick must be
compatible; the source does not invent a tick. Entry increases stop on any
partial fill, pause, kill, stale/changed data, wide book, price-cap breach or
uncertain modification. Cancellation acceptance never releases the account
slot: the ledger must observe terminal broker evidence.

The CAS watcher starts before entry stepping and for restored positions,
even when new CAS entries are disabled. It protects confirmed partial fills,
latches exits before BUY cancellation, and sells only after the final BUY
quantity is known. Quotes are fetched outside the stepping writer lock so a
partial fill can be reconciled while quote acquisition waits. The 3-second
interval is a target cadence, not a latency guarantee under rate limits,
network delay or lock contention.

CAS exits/peaks use a fresh executable bid, not a chart LTP or historical
peak masquerading as a live quote. No stale entry/stop/peak fallback SELL is
used. A missing bid leaves an urgent exit latched and escalated. CARRYFORWARD
prices preserve two decimal places, including a 0.05 bid. CAS exit repricing
uses the current bid and the same order ID on a 3-second target cadence.

There is still no ordinary CAS entry stop. At +30%, the existing 25%-from-peak
trail arms with a floor at entry and never reduces a previous stop. Entry
price is gross breakeven, not breakeven after charges, and an order may fill
below its trigger. Generic Gamma structure updates cannot inject an entry
stop into CAS. EOD, daily-loss and explicit exits remain binding.

Broker fills require exact CAS symbol/token/exchange/side/product. Manual
same-contract fills are not absorbed into the CAS position. Unrelated pending
orders block entries but need not suppress an exactly reconciled owned exit;
unknown identities and same-contract collisions still block unsafe selling.

The standalone CAS watchdog never directly places a broker SELL. It verifies
local live-fill ownership and exact broker quantity, then makes one fixed,
authenticated loopback request to the shared engine. It does not claim that
acknowledgement is a fill. An unresponsive engine, uncertain ownership or a
manual same-contract quantity collision escalates without racing another
writer. The legacy non-CAS watchdog path is not redesigned by this patch.

## Reproduce the checks in an isolated review checkout

```sh
python3 -I -B tools/check_cas_release.py
python3 -I -B tools/test_cas_safety.py
python3 -I -B tools/run_cas_regressions.py test_offline.py
python3 -I -B tools/run_cas_regressions.py test_audit_fixes.py
python3 -m compileall -q kadima tools/check_cas_release.py tools/test_cas_safety.py
```

The regression runner uses real source modules and temporary SQLite databases
with fake order books. Network and broker-session creation are denied. It
never executes the live acceptance-order script. These checks prove specific
software invariants, not production permissions, live fill quality or profit.

## Server deployment boundary — do not pull over a running mixed checkout

Use an isolated checkout of the final pinned review commit first. To compare
installed SOURCE ONLY, without importing the application or touching services:

```sh
python3 -I -B tools/check_cas_release.py --live-root /home/ubuntu/kadima-py
```

Unknown or mixed source blocks. Do not overwrite source or bypass this result.
The earlier reported `priority_trading/runtime` release is a different composed
lineage; this branch must not silently replace its execution/watchdog/session
overlays. Matching source is not proof of a flat account or deployment safety.

Before a separate deployment: establish the actual running build and file
hashes, reconcile broker positions and pending orders, preserve config and
private backups, stop competing writers/schedulers under the reviewed host
procedure, and perform post-install source/import/API checks while entries
remain paused. Roll back source only while verified flat; never roll back the
execution database to erase fills. No activation command is supplied here.

The existing session calendar/permit is NOT widened. If it stops equity
entries at 15:15, CAS will also remain blocked. An explicitly reviewed
CAS-specific late-session authorization is required; disabling the gate is
not a workaround. Contract lot/tick/expiry/freeze settings, clock policy and
broker product permission must be verified for the deployment date. A static
configured quantity ceiling is not proof of the exchange's current freeze.

The dead-engine fallback here is escalation, not an independently authorized
liquidator. No live-ready claim is made until the host's exclusive-writer and
recovery arrangement is verified. The no-entry-stop policy can lose the
allocated premium; the trailing threshold does not guarantee a fill or profit.

## External references checked

- SmartAPI Live Market Data API and product documentation:
  https://smartapi.angelone.in/docs/User
- Angel One risk-management policy (intraday cutoffs are distinct from CARRYFORWARD):
  https://www.angelone.in/riskpolicy
- NSE segment-specific market timings:
  https://www.nseindia.com/static/market-data/market-timings

Use the actual exchange segment and broker product. No generic 15:40 statement
is used to override an installed strategy's earlier authorization cutoff.
