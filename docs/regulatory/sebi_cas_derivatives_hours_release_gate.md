# SEBI CAS / derivatives-hours release gate

**Status: BLOCKED — no code or production cutoff change authorized by this note.**  
Review date: 2026-10-16 (IST)

## Source verified

SEBI circular dated 2026-01-16:

- Reference: `HO/47/11/11(3)2025-MRD-POD2/I/2765/2026`
- Title: *Introduction of Closing Auction Session (CAS) in the Equity Cash Segment and certain modifications in the Pre-Open Auction Session*
- Official source: https://www.sebi.gov.in/legal/circulars/jan-2026/introduction-of-closing-auction-session-in-the-equity-cash-segment-and-certain-modifications-in-the-pre-open-auction-session_99122.html

The published circular specifies an equity cash CAS from 15:15 to 15:35 and states that the equity-derivatives segment continues to operate until 15:40. These are the timings stated in this verified source; confirm subsequent exchange implementation notices and amendments before relying on them operationally.

## Not verified

A purported SEBI final circular dated 2026-10-15, including new 15:45 regular derivatives hours, a 15:30 expiry-day index-derivatives cutoff, VWAP-based expiry settlement changes, and 2026-12-07 / 2027-02-08 effective dates, was not independently located in SEBI's official circular listings during this review.

Do not encode those claims, treat a proposal as final, or use a search snippet/media article as sufficient evidence. Obtain the primary SEBI circular, its annexures, and relevant NSE/NSE Clearing implementation circulars before changing trading behavior.

## Required evidence before implementation

1. Record the official SEBI circular URL, circular number, publication date, paragraphs and annexures supporting each rule.
2. Obtain the implementing NSE/NSE Clearing notices and confirm the exact effective date, market segment, expiry-day distinction, holiday handling, and treatment of existing orders/contracts.
3. Keep cash CAS timing, index-derivatives trading cutoff, stock-derivatives cutoff, and settlement-price calculation as separate settings. Do not apply cash-auction restrictions to F&O by analogy.
4. Add deterministic IST boundary tests for regular sessions, expiry sessions, special sessions, holidays, delayed/missing exchange data, and broker-reported open orders/positions.
5. Preserve Gamma Blast, CAS signal/entry logic, Shield, Angel One execution, capital/risk controls, stops, reconciliation, and existing dashboard behavior unless a separately reviewed requirement explicitly calls for a change.
6. Deploy only after exact-head CI, source/host identity checks, backup/rollback review, and operator approval. A GitHub PR or passing offline tests do not prove AWS deployment or live-session correctness.

## Current action

This note documents the source-verification gate only. It does not alter runtime code, market hours, settlement logic, trading permissions, services, or AWS state.
