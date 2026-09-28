# Priority MCX, CAS and owned-position watchdog release

Prepared after the 28 September 2026 ordinary automation receipt
`auto_trading_deployed_waiting_for_session`. This package is an additive overlay
on the composed AWS release `8837cd6dc04e16123e94d8b78a3a716f83ed022c`, including
the installed MCX/session/portfolio executor. It does not replace the server
checkout with an older PR branch.

## Resulting behavior

| Component | Behavior |
|---|---|
| MCX | Existing five-minute futures/option confirmation strategy; maximum one affordable option lot; explicit capital ceiling no higher than ₹15,000; existing open capital and portfolio stop risk consume headroom. |
| CAS | The PR #7 expiry premium-awakening strategy, integrated with the current session permit, manual carry-forward exclusions, durable fills, daily loss headroom and shared execution lock. Initial entry plus at most one confirmed expansion add share the original budget, capped at ₹15,000. |
| Scheduler | One coordinator owns global and stock intent. Separate expiring equity and MCX permits prevent the equity close from disabling MCX and prevent an MCX permit from authorizing an equity entry. Operator changes hold both lanes for the rest of the day. |
| Watchdog | Separate `kadima-owned-watchdog.service` and timer. Uses recorded real buys minus sells and matching broker product/quantity. Manual and adopted positions are excluded. Requests guarded exits from a responsive engine; if the engine has released its lifetime lock, restores the ledger and uses an exclusive exit-only fallback. |

All orders retain the existing INTRADAY, LIMIT, DAY transport. The entry and exit
clocks account for Angel One's published square-off rules:

* Equity derivatives: entries before **15:15 IST**, exits due **15:18 IST**, ahead
  of the broker's **15:20** intraday square-off. This earlier broker-aware cutoff
  applies to the ordinary equity lanes as well as CAS. CAS is not switched to a
  carry-forward product to extend its window.
* MCX: entries before **22:35 IST**, exits due **22:40 IST**. This deliberately
  uses the earlier option-devolvement-day cutoff every trading day; it does not
  depend on an unverified daily expiry calendar. On the remaining 2026
  morning-closed/evening-open holidays, entry eligibility begins at **17:00**.
* Calendar coverage ends **31 December 2026**. Unknown future calendars and
  special weekend sessions do not permit new entries.

An entry still needs current source quotes, reconciliation, news/macro checks,
funds, the strategy's unchanged numeric setup thresholds and no pending order.
Arming does not imply an order or fill. No minimum number of trades is promised.

The watchdog never treats acknowledgement as closure. An unresponsive engine
that still holds the execution lock, an ambiguous pending order, stale exit bid,
or unmatched broker quantity produces an alert instead of a competing exit.
Stops and watchdog exits remain subject to broker availability and liquidity.
The earlier unsafe watchdog cron remains held; the new timer replaces its role.

## Installation and verification

Run the pinned `priority_trading/deploy.sh COMMIT MCX_CAPITAL` on the AWS host.
The wrapper fetches both immutable commits. It first validates every runtime
manifest source and tightens **only** exact-source, engine-owned (`ubuntu:ubuntu`)
mode **0664 to 0644**. Existing protected root/engine files retain their modes.
No recursive chmod/chown, content edit, ownership change, service action or
database/broker access is performed by this permission step. Unknown contents,
owners/modes, links or replaced inodes block before deployment. It holds the
existing maintenance locks, logs the plan and each result in a private
`/var/lib/kadima-priority-modes-*/status.json`, and preserves any already tightened
modes on failure. The separate normal installer then runs all existing checks:

1. Verifies the ordinary deployment receipt, exact composed sources, service
   context, paused controls, historical repair provenance and absence of local
   managed exposure or unresolved intents.
2. Reads fresh broker books. Accepts a flat account or the previously authorized
   exact manual NIFTY carry-forward holding; it never adopts or modifies it.
3. Backs up changed source/configuration, stops the old scheduler, revokes the
   permit and restarts only the engine after another broker check.
4. Verifies the loaded features with the authenticated runtime status endpoint,
   verifies the standalone watchdog while entries remain paused, then enables
   the two timers. Each entry remains subject to independent lane readiness.

Status: `/var/lib/kadima-priority-trading-v1/status.json`.
Expected completed installation state: `priority_trading_installed_scheduled`.
The report includes the actual scheduler reasons for equity and MCX. A waiting
reason is not an execution failure or proof of a live fill.

The 28 September 21:21 IST operator output stopped during admission with
`runtime_source_owner_or_mode_changed`, before an attempt claim or service
change. The earlier check accepted only engine-owned source, although the
pinned prior installers preserve existing root ownership. The corrected
admission accepts root or the verified engine owner, requires engine-readable
regular single-link files without group/other write or special mode bits, and
preserves UID, GID and mode. It also binds inode/metadata through replacement
and reports the relative filename and nonsecret metadata on refusal. The old
output did not identify the file or its metadata, so the host diagnosis is
conditional on this check; no recursive ownership/permission change is needed.
An initial refusal leaves the existing scheduler state unobserved, not disabled.

The follow-up AWS output shared at 21:55 IST identified the actual first file:
`kadima/auto_executor.py`, UID/GID 1000/1000, mode 0664. No attempt was claimed,
deployment performed or service change requested. The permission preflight
above resolves that observed mode for all matching runtime manifest files while
keeping the installer's strict metadata admission unchanged. Protected root
ownership was not the cause of this particular refusal.

Any incomplete attempt retains its backups and revokes entry permission. It
does not roll back a trading database, manufacture a broker fill, reset a kill
latch, overwrite unknown source, or automatically repeat an uncertain install.

The 22:19 IST host diagnostics identify the 22:12 failure as the initial
`kadima-owned-watchdog.service` check: its request to the engine raised
`HTTPError`, with the response status/detail omitted by the installed runner.
The engine is active; the old scheduler's last successful observation predates
this attempt. Its earlier `outside_scheduled_window` result is not evidence that
the new priority scheduler started. Both timers were disabled by cleanup.

For this installed-but-held state, run `probe_watchdog.py` from a pinned archive
with its adjacent unchanged `manifest.json`. It preserves the original attempt
and backups, obtains the existing maintenance locks, checks installed runtime
hashes, inactive services/disabled timers, matching watchdog/engine API tokens,
a revoked permit, fresh paused controls and no managed positions. It calls the
existing guarded watchdog endpoint once and reports its HTTP status plus only
allowlisted detail, followed by pause/process/permit checks. The engine journal
is reduced to traceback file/line/function and exception types. Credentials,
provider bodies and exception messages are omitted. This endpoint ordinarily
performs reconciliation and owned exit handling; the probe is not described as
a generic read-only broker operation. It never enables a timer, restarts a
service, installs source or retries the original installer. An observed HTTP
response is diagnostic evidence, not completed activation or verified fills.

Offline verification: `python3 priority_trading/verify.py`. Existing base tests
also run unchanged in CI. The composed suite includes CAS staged fills and
rejections, MCX budget/freshness checks, manual-holding exclusions, watchdog
single-writer behavior, scheduler transitions/lost responses, and deployment
failure boundaries. No live broker order is used for testing.

## Reviewed sources

* Angel One Risk Management Policy, especially F&O 15:20 square-off and MCX
  option-devolvement-day square-off 45 minutes before close:
  <https://www.angelone.in/riskpolicy> (reviewed 28 September 2026).
* MCX trading hours: <https://www.mcxindia.com/faq/market-operations>.
* MCX circular MCX/TRD/636/2025, 15 December 2025, 2026 trading holidays,
  issuer-authored PDF hosted by a broker:
  <https://www.steelcitynettrade.com/Circulars/MCX-Trading%20Holidays%20for%20the%20Calendar%20Year%202026.pdf>.

AWS installation, current broker permissions and market-time data readiness
must be confirmed from the host's resulting receipt; GitHub tests cannot prove
these live conditions.
