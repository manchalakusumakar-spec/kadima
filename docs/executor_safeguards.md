# Executor safeguards

Subsequent stop/entry amendments are documented in [trade_policy.md](trade_policy.md).
The historical strategy-preservation statements below describe the original safeguards patch.

This patch replaces assumed fills with a persisted order lifecycle. It does not
change trading mode, credentials, strategy thresholds, signal cutoffs or the
existing 15:38 end-of-day setting. That schedule still needs independent review
against the actual instrument session and broker square-off policy; this patch
only fixes the code path that previously suppressed its own EOD rule.

## Behavior

- Before sending an order, save its intent and position state in SQLite, with
  FULL synchronous durability for execution writes. A unique broker order tag
  links ambiguous submissions to their later order-book records.
- An accepted BUY is `entry_pending`, with zero owned quantity until fills are
  confirmed. Partial fills update actual quantity and weighted entry price.
  Request cancellation of an unfilled entry remainder on a partial fill or
  after 30 seconds. Keep tracking until the broker confirms a terminal status.
- An accepted SELL is `exit_pending`. Do not remove the position, invent a fill
  price or realize P&L on acknowledgement. Apply incremental confirmed fills,
  keep unsold quantity, and remove the position only after confirmed closure.
- Store cumulative fill cursors and position updates in one transaction. Repeated
  order-book snapshots and restarts do not double-count fills. Realized P&L is
  gross of fees and taxes. Paper fills are marked and excluded from live P&L.
  Historical closed rows without journal orders retain their old accounting;
  this patch cannot retroactively verify those historical fills.
- Serialize entry/exit/reconciliation mutations. Pending positions reserve slots;
  each submitted signal ID is consumed, and repeated exits cannot send another
  SELL while the first order remains unresolved. Dashboard quantity changes to
  tracked pending orders are rejected; cancellation and price changes remain.
- Restore pending positions on restart. Reconcile exchange, token, product and
  quantity with broker positions; unknown external orders/positions block entry.
  Retry reconciliation every 30 seconds. Missing broker responses never mean flat.
  Previously verified positions can retain exit monitoring during a transient
  position-book outage; an actual quantity mismatch suspends automatic exits.
- Daily-loss and EOD conditions override the no-auto-exit window. Persist every
  new peak. A failed LTP request no longer lets a session exception bypass the
  exit loop. Initial-stop, swing and forecast-trail policy remain otherwise intact.
- Monitor work consumes its 10-second cadence instead of adding a full sleep
  afterward. Slow broker calls can still lengthen a cycle.
- Require a recently received complete option-chain sample and a fresh entry
  LTP. Incomplete chains are not restamped or added to OI velocity history.
  Invalidate Greeks after fresh quotes so ranking recomputes them.
- Retry CONFIRMED gamma entry checks on later successful polls using the same
  signal ID. A direction change creates a new episode. No automatic retry of an
  ambiguous submitted order. Gamma universe scanning still has its existing
  shortlist and remains separate from the index executor.
- Telegram delivery uses a bounded background queue; delivery failures are audited.
- Session login uses one attempt plus a five-second cooldown instead of five
  attempts with 31 seconds of sleep while holding the session lock.
- All getMarketData calls through newly created SESSION SDK clients share a
  SQLite quote budget under KADIMA_DATA_DIR, conservatively spaced 1.1 seconds
  apart. This includes restarted engine/scanner/macro services sharing that path.
  Other hosts or SDK clients bypassing SESSION are outside this budget. The
  existing order-write limiter is adjusted to nine requests/second; minute/hour
  order quotas and account-wide external clients are not globally coordinated.

Broker response fields and current order-rate reference:
https://smartapi.angelone.in/docs/Instruments (order book, postback, Rate Limit).
The conservative quote spacing is a deployment choice following observed rate
errors, not a claim that the broker's published maximum is one request/second.

## Important remaining limits

A pending limit exit is not a guaranteed exit. After 30 seconds it remains tracked
and raises an audit/Telegram warning. This patch does not chase prices, submit
replacement exits blindly, guarantee exchange fills, or implement broker-side
protective stops. Manual/external broker changes can require reconciliation.
Unknown submissions whose tags never appear remain blocked for investigation.
The full entry evaluation is still serialized and uses synchronous broker calls;
this is a correctness fix, not a low-latency execution architecture.

The forecast-conditioned 30-minute trailing feature still defaults to disabled.
There is no validated forecast producer, and no forecast is fabricated here.

## Verification

```
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_execution_*.py' -v
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_forecast_trail.py' -v
```

35 offline tests cover order rejection, acknowledgement without fills, partial
BUY/SELL fills, cancellation, duplicate exits and competing entries, ambiguous
submission/tag recovery, restart idempotency, storage rollback, gross partial
P&L, dry-run exclusion, broker outages, external positions, EOD/daily priority,
high-water persistence, partial quote batches, Greek invalidation, shared quote
budget across processes, login cooldown, gamma retry/direction, and forecasts.
No broker orders were sent during verification.

## Deployment

`executor_fix/install.py` checks file hashes and compiles all replacement Python
files before stopping services. It only runs between 19:00 and 08:00 IST, a chosen
maintenance window. It reads the running engine's existing environment and Python
interpreter without printing or saving credentials. After stopping active engine,
scanner and macro services, it uses that environment for a read-only broker login,
position and order-book preflight. Both local and broker state must be flat with
no unresolved orders. Missing broker confirmation aborts installation.

It backs up source, replaces only manifest files and restarts the previously
active services. No configuration flags or credentials are edited. On immediate
installation failure it restores source and restarts the original services; it
never replaces or rolls back the database. The database additions are additive.

A successful installer message establishes source installation and service
activity, not successful live trading. Inspect the next session's
`execution_order_submitted`, `execution_fill_confirmed`, and reconciliation events
and compare them with the broker order book. Do not use a blind source rollback
while a new lifecycle order is pending; first reconcile/flatten in maintenance.
