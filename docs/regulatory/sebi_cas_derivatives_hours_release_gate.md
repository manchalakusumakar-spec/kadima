# SEBI CAS / derivatives-hours release gate

**Status: RULE VERIFIED; implementation remains gated on exchange/broker readiness.**  
Review date: 2026-10-17 (IST)

## Primary source verified

SEBI circular dated **2026-10-15**:

- Reference: `HO/47/11/11(3)2025-MRD-POD2/I/23843/2026`
- Title: *Review of certain aspects of the Closing Auction Session, market timings and settlement methodology for derivatives contracts*
- Official HTML: https://www.sebi.gov.in/legal/circulars/oct-2026/review-of-certain-aspects-of-the-closing-auction-session-market-timings-and-settlement-methodology-for-derivatives-contracts_105293.html
- Official PDF: https://www.sebi.gov.in/sebi_data/attachdocs/oct-2026/1792079571420.pdf

The seven-page primary circular has been opened and checked. Relevant provisions are paragraphs 4.2, 4.3, 4.5, 4.8, 4.9, 4.11 and 6.

## Confirmed rule and transition dates

### Effective 2026-12-07 — all modifications except CTS-to-CAS order carry-forward (paragraphs 4.8)

- Equity cash CAS: **15:30–15:40 IST**. Reference-price/transition phase 15:30–15:31; limit and market order entry 15:31–15:36; limit-only entry 15:36 until a random close between 15:38 and 15:40.
- Index derivatives: trade until **15:45** on ordinary days; **expiring index-derivative contracts stop at 15:30 on expiry day**.
- Stock derivatives: trade until **15:45**, including expiry days.
- Cash post-close: **15:50–16:00**.
- CAS reference price: cash-security VWAP during the final 15 minutes of continuous trading; if no trade in that interval, use the day's LTP; if no trade all day, use prior close (adjusted close/base price for corporate actions).
- During cash CAS, only limit and market orders are allowed. New iceberg and stop-loss orders are not allowed.
- Expiry settlement: index derivatives use the underlying index calculated from constituent-security VWAPs during the final 30 minutes of continuous cash trading. Stock derivatives use the underlying stock's VWAP over the same 30-minute period across all stock exchanges. Exchanges must disseminate indicative values/VWAP continuously in that interval.
- Stock-futures price-band flexing is not applicable from 15:30–15:45; the band aligns with CAS during that interval.
- CAS orders are subject to order-level margin, with the circular's specified exception for unmodified CTS orders carried into CAS.

### Effective 2027-02-08 — paragraph 4.8

- Unexecuted continuous-session limit orders generally carry into CAS, except stop-loss orders and orders outside CAS price bands.
- Eligible unexecuted iceberg orders carry forward as fully disclosed limit orders unless the participant opted out at original placement.
- Transitioned iceberg orders have lower time priority than other carried-forward limit orders. Carried-forward orders otherwise outrank new CAS limit orders; modification changes time priority.

## Implementation boundary

SEBI paragraph 5 directs exchanges and clearing corporations to amend their SOPs within seven days. In this review, the SEBI primary circular is verified, but a separate implementing NSE/NSE Clearing circular and Angel One's exact order/session behavior have **not yet been verified**. Do not claim exchange/broker readiness based on the SEBI circular alone.

Before changing runtime code or deploying:

1. Obtain the NSE/NSE Clearing implementation notices and verify the exact effective dates, instrument scope, special sessions/holidays, order transition and price-band behavior.
2. Confirm Angel One's supported order types, cutoff behavior, exchange rejection handling, pending-order and fill reconciliation, and whether any broker-specific square-off policy is earlier.
3. Keep cash CAS timing, regular index-derivatives cutoff, expiry-day index cutoff, stock-derivatives cutoff and settlement calculation as separate settings.
4. Add deterministic IST boundary tests for regular/expiry/special sessions, holidays, random CAS close bounds, stale/missing VWAP or constituent data, order carry-forward/priority, and broker-reported open orders/positions.
5. Never use the last displayed index tick as a substitute for the specified constituent-VWAP settlement calculation. Fail closed on incomplete or stale settlement inputs.
6. Preserve Gamma Blast/CAS signal and entry logic, Shield, Angel One execution policy, capital/risk controls, stops, reconciliation and dashboard behavior unless a separately reviewed requirement calls for a change.
7. Require exact-head CI, source/host identity checks, backup/rollback review and explicit operator approval. A PR or offline tests do not prove AWS deployment or live-session correctness.

## Current action

This is a documentation/source-verification update only. It does not alter runtime code, session gates, settlement logic, trading permissions, services or AWS state.
