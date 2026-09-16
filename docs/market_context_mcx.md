# FETCH, timestamps and MCX market context

This release fixes F&O visibility in the authenticated `/state` response and
adds read-only MCX futures/options observations to the existing macro service.
It does not enable MCX order execution or alter equity trading hours, existing
Shield confirmation, trading flags or position management.

## FETCH and timestamp repairs

`fno_universe` reads the scanner's existing atomic snapshot. It includes the
top 20 tracked stocks, selected contracts, coverage and original observation
timestamps. The legacy scanner can price the full universe while scoring only
a rotating shortlist. A stock outside that shortlist is **not scored**, rather
than a failed timestamp. Stock Gamma scores are separate from index Shield.

Snapshot, score and contract ages are checked separately. Missing, corrupt,
oversized, stale and after-hours snapshots cannot be presented as current.
Scanner receipt time does not verify the exchange's last trade time.

The dashboard uses news `published_at` and separate `received_at`; it no longer
asks a news article for the nonexistent `last_updated`. Calendar events show
the meeting date range and the date the published calendar was checked. Empty
browser history after the equity session has a specific explanation. Actual
missing source timestamps remain explicitly unavailable.

## 24/7 context and MCX quotes

The macro service continues running all day, including overnight and weekends.
The existing India-filtered Marketaux news feed is polled every 30 minutes when
its API key is configured. This is contextual news coverage, not a comprehensive
global breaking-news feed. The new headline labels identify possible markets
to watch; they supply no sentiment score, trade direction or order instruction.

MCX FULL quotes are polled every 30 seconds in the weekday 09:00–23:30 IST
monitoring window. This honors the requested 23:30 cap even during the later
winter exchange session. The window is not a holiday calendar. During closures,
old observations retain their exchange timestamps and never become live simply
because the dashboard fetched them again.

Coverage: CRUDEOIL, GOLD, NATURALGAS and SILVER; nearest listed FUTCOM expiry,
plus one CE and one PE in the nearest OPTFUT expiry, selected near the displayed
future's price. These are quote references, not verified option-to-underlying
expiry mappings or execution-approved contracts. A stale futures quote cannot
be used to select new option references. The contract master is refreshed and
must be no more than 12 hours old before MCX polling. No P&L multiplier is guessed.

The authenticated dashboard macro feed and `/state.commodities` expose exchange
feed time, last-trade time and observation time separately. Missing, future,
1970-placeholder or older-than-90-second exchange times fail freshness checks.
Broker request failure preserves old observations with old times and resets
the option activity history.

## MCX Gamma Blast watch

The monitor reuses the existing pure `PremiumWindow` calculations, without
publishing to the order dispatcher. It observes actual five-minute option
premium movement and incremental volume rates. A `premium_blast_watch` requires:

- At least five minutes of continuous same-contract observations.
- Premium gain of at least 5%.
- Recent volume rate at least 1.5 times its preceding rate.
- Positive current OI with a nonzero measured OI change.
- Positive bid/ask with spread no wider than 1.5% of midpoint.
- Fresh exchange feed and trade timestamps.

This is an activity watchlist. OI changing does not prove bullish/bearish OI
support. Computed option gamma, full-chain confirmation and a calibrated MCX
Gamma score are unavailable; `gamma_score` stays null, and execution eligibility
stays false. Futures have no option gamma. Switching contracts, stale data,
counter resets and failed requests require a new observation window.

## News interpretation as of 16 September 2026

The visible headlines are useful for selecting research targets, but the
screenshot supplies no surprise-versus-consensus measure, current price reaction
or transaction-cost estimate. It cannot establish a profitable trade.

| Context | Possible transmission, to be confirmed | Evidence needed |
| --- | --- | --- |
| RBI policy | Funding rates and credit expectations affect bank/financial stock earnings; effects on margins and growth can offset | Actual policy decision versus expectations, bank breadth, price/volume and the existing Shield conditions |
| Fed policy | Dollar/yield changes can alter Indian equity flows and precious-metal prices; INR can offset USD commodity moves | Official decision, yields/FX and the relevant contract's fresh reaction |
| Crude supply/inventory news | Upstream realizations may benefit from higher crude; fuel/input costs may pressure airlines and some industrial consumers | Verified supply/inventory surprise, crude/INR reaction, issuer exposure and hedging/tax effects |
| Tata Sons listing debate | Potential ownership-value read-through to specific listed holders; no automatic benefit to every Tata company | Official company/exchange disclosure, actual ownership exposure and a confirmed listing timetable |

The Tata Sons item reports an adviser's request to pursue an IPO. It does not
announce a completed approval or a confirmed IPO date. The broader market-wrap
headline is context, not an independent trading trigger. Unexpected news may
create volatility; anticipated news may already be priced and can reverse after
release. Use the resulting watchlist with market confirmation and costs, not as
a replacement for the existing entry gate.

Sources reviewed:

- [RBI published policy calendar](https://www.rbi.org.in/scripts/annualpolicy.aspx)
- [Federal Reserve September 2026 calendar](https://www.federalreserve.gov/newsevents/2026-september.htm)
- [Economic Times Tata Sons report](https://economictimes.indiatimes.com/news/company/corporate-trends/tata-sons-should-commit-to-long-delayed-ipo-after-rbi-setback-proxy-adviser-says/articleshow/134290758.cms)
- [Angel One intraday square-off times](https://www.angelone.in/support/your-orders/square-off)
- [SmartAPI margin request reference](https://smartapi.angelone.in/smartapi/forum/topic/4010/calculate-margin-requirements-with-smartapi-s-new-margin-calculator-api)

## Deployment and outstanding MCX execution work

Install from a reviewed, pinned archive using the existing consolidated
`executor_fix/install.py`, then `dashboard_live/upgrade.py --apply`. Source
checks, backups and flat-position/pending-order checks remain in place. Updates
are permitted at any hour. The backend installer must complete successfully
before claiming the new monitor is running. Refresh the dashboard with Ctrl+F5.

Run `bash tools/check_mcx_access.sh` from the release archive for the improved
read-only check. It recognizes MCX profile aliases, reports an allowlisted
broker error code and error categories, and sends a margin calculation with
the observed reference price and explicit `orderType: LIMIT`. It never submits, modifies or cancels orders,
logs out, changes services or prints raw broker account/session details.

EC2 deployment completed on 17 September 2026 at approximately 00:55 IST:
the backend services restarted successfully and the dashboard gateway passed
health checks. The profile includes `MCX_FO`, and MCX quote access succeeded.

The follow-up read-only check at 01:02:44 IST resolved `CRUDEOIL21SEP26FUT`
to token `565899`, matching the cached master. With an explicit `orderType:
LIMIT` and reference price 9815, both INTRADAY and CARRYFORWARD returned
positive margins: 4167.19 for request quantity 1 and 416718.75 for request
quantity 100. These are API input quantities, not verified tradable lot sizes
or P&L multipliers. No orders were submitted.

The earlier margin request without an explicit order type returned `AB4033`.
Because the successful check ran later with a new session, it does not isolate
the error's cause. The standard diagnostic now explicitly sends LIMIT to match
the successful request. Order acceptance remains unverified.

MCX execution is not implemented by this release. Before adding it, verify
live order entitlement, each contract's price tick/quantity/P&L units and expiry
mapping, and extend the single central ledger for futures direction, signed
reconciliation, margin reservation and session-specific exits. Angel One lists
23:15 intraday square-off for a 23:30 commodity close, so future intraday exit
logic must allow time before that broker cutoff.

Verification: Python regressions cover FETCH auth/freshness, after-hours data,
MCX batches, invalid source times, activity warm-up/reset and the exact broker
diagnostic with fake credentials and a fake read-only API. The intercepted
browser fixture covers published timestamps, calendar ranges, unscored rows,
MCX rendering, stale trade times, and existing controls on desktop/mobile.
