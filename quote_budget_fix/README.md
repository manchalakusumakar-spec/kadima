# Shared quote scheduling repair

The 18 September 2026 operator report counted 1,040 `LOCAL_QUOTE_BUDGET`
OI failures, 899 across NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY. These are
local admission timeouts, before the SDK sends a quote request. The 17 September
report counted 1,213 local admission timeouts, 1,112 across those four indices.
These counts cover the retained records shown in the supplied screenshots.

The old limiter spaced admissions at 1.1 seconds but let all callers race for
each slot for only three seconds. It had no waiting order. Stock scanning,
index OI and other quote clients could repeatedly displace each other.

The repair queues tickets in SQLite in arrival order and allows a bounded
12-second wait. Each subsequent scanner request joins the tail. Grants retain
the existing 1.1-second spacing. Timed-out tickets are removed; crashed tickets
expire. A locked database fails closed. The old `budget` row is retained, so
upgrading does not reset the rate limit. FIFO fairness requires every quote
client sharing this data directory to load the repair. Requests from other
hosts/data directories remain outside this local budget.

An offline comparison with eight simultaneous workers at production spacing
produced 3 admissions / 5 timeouts with the old code, and 8 admissions / 0
timeouts with the repair. Minimum observed grant spacing was 1.109 seconds in
the repaired run. This is a controlled contention result, not live recovery
evidence or a guarantee of 12-second completion under arbitrary load. It does
not increase the quote service's total capacity or parallelize the index loop.

## Validate and install

Run from an extracted copy of this branch, using the installed Python runtime:

```bash
python3 -B -m unittest discover -s tests -p 'test_shared_quote_scheduler.py' -v
python3 -B -m unittest discover -s tests -p 'test_quote_budget_install.py' -v
python3 -B quote_budget_fix/install.py --root /home/ubuntu/kadima-py
sudo python3 -B quote_budget_fix/install.py --root /home/ubuntu/kadima-py --apply
```

The installer verifies the reviewed original file, makes a backup, validates
Python syntax in memory, and atomically replaces only `kadima/rate_limiter.py`.
An unknown installed version is refused. Reapplying the same repair is a no-op.
It changes no service, rollout, auto-pause, account or risk settings.

Running processes retain the old module until reloaded. The operator must
reload all existing quote clients in a maintenance window after accounting for
open positions: `kadima.service`, `kadima-universe-scanner.service`, and
`kadima-macro-watch.service`, plus any other process importing this limiter.
The installer does not start or enable any service. Keep `quote_budget.sqlite3`
in place; deleting it resets admission history.

## Verify recovery

1. Confirm the newly loaded processes use this file and the same `KADIMA_DATA_DIR`.
2. Compare **new** `oi_poll_failed` events after reload, specifically the
   `shared quote budget` error. Historical totals will not decrease.
3. Check recent successful OI samples and sample age for all four main indices.
   A loop can still be too slow for the existing 30-second freshness gate even
   when individual calls succeed; that needs a measured batching/cadence repair.
4. Allow the existing ten-minute degraded OI window to clear after the last
   failure and enough complete samples to accumulate. No alert is fabricated
   during warm-up. Network/broker failures can still degrade that window.
5. Recheck confirmation audit reasons. Score, OI movement, breakout hold,
   reconciliation, news coverage and explicit rollout controls still apply.
   Restored quotes alone do not mean an entry qualifies.

No entry condition or trading control is loosened by this patch. It repairs
one evidenced upstream blocker; it cannot establish which historical trades
would have qualified or prove that the broker received no orders.

For rollback in the same operator maintenance procedure, restore the printed
backup to `kadima/rate_limiter.py` and reload the same clients. The extra queue
table may remain in the budget database; the old limiter ignores it.
