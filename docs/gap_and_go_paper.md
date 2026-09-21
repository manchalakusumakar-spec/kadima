# Gap-and-Go paper lane

This draft implements only the observation/replay/paper stages of the NSE F&O
Gap-and-Go handoff. It is intentionally unable to place a broker order.

## Safety boundary

- No call to `AutoExecutor.evaluate_and_enter`.
- No call to `execution_ledger.submit`.
- No call to `angel_orders.place_limit_order`.
- No AWS, service, scheduler, account-control or broker-setting mutation.
- `KADIMA_GAP_PAPER` defaults to off.
- Even when the prefilter is explicitly enabled, it only publishes
  `PAPER_PREFILTER` observations from the existing full-universe FULL quote.
- The pure detector can reach `CONFIRMED`; only the in-memory `PaperBook`
  can turn that into a rehearsal position.

Production trading behavior therefore remains unchanged unless a future,
separately reviewed live-execution change is written and activated.

## State machine

`WATCH -> GAP_DETECTED -> IGNITION -> PULLBACK_HOLD -> ORB_BREAK -> CONFIRMED -> ENTRY`

Only `CONFIRMED` is paper-entry eligible. Earlier stages never dispatch.

Current paper hypotheses:

- 09:15-10:30 IST only.
- Gap-up >= 1% for initial replay; this threshold is deliberately calibration
  data, not a permanent production rule.
- First completed 5m/10m bullish impulse.
- Same-time-of-day relative volume >= 2x.
- Gap retains at least 50% of its opening gap.
- Price above session VWAP and VWAP not materially falling.
- First pullback <= 50% of opening impulse.
- Pullback average volume <= 80% of impulse average volume.
- Closed-bar break above the opening range/pullback high.
- Planned underlying reward/risk >= 2R, with no known resistance inside 2R.
- Existing Bull Shield confirmation must be true.
- Gamma/OI confirmation must be true.
- Futures price/OI must confirm when futures evidence is available.
- Fresh news and macro gates must be clear.
- Fresh nearby CE wall >=150% of its comparison level within 50 points rejects
  a new entry; 120-150% is currently recorded as a penalty for replay analysis.

These are starting hypotheses. They are not calibrated probabilities or
profitability claims.

## Data path

The existing `full_universe_scanner.py` already obtains one FULL spot quote in
the same batch used for every resolved NFO stock root. When
`KADIMA_GAP_PAPER=1`, that exact response is passed to
`gap_and_go.observe_full_quote`; no second universe-wide quote request is
added. The prefilter uses only open, previous close, LTP, session average price
and cumulative volume. Missing or malformed fields fail closed.

The deeper detector is pure and consumes closed 5-minute bars plus explicit
confirmation evidence. `gap_baseline.py` computes prior-session close and the
median same-time slot volume. `tools/replay_gap_and_go.py` accepts local JSON
fixtures and has no network/order capability.

## Strike policy

`ranked_atm_or_itm_calls` reuses KADIMA's existing strike ranker and further
restricts the Gap lane to CE contracts at the current ATM strike or exactly one
strike ITM. It therefore preserves liquidity/spread/delta/OI ranking rather
than selecting a contract by expected profit.

## Items intentionally not connected yet

The following remain release blockers before any live order path can be
considered:

1. Build the stock-specific adapter that supplies the existing Bull Shield v2.1
   confirmation without weakening its two-completed-5m-bar invalidation rules.
2. Attach current Gamma/OI evidence and nearest FUTSTK confirmation.
3. Turn macro calendar context into a tested fail-closed entry suppression gate.
4. Replay representative gap winners and failed gaps, then run out-of-sample
   sessions with realistic option costs/slippage.
5. Run live-market paper observation with no broker submission and compare
   detections against actual subsequent movement.
6. Implement/test lot-aware partial exits for the preserved rule:
   fresh CE OI wall >=150% within 50 points -> book 40%. The current reviewed
   PR #10 source does not provide that exact partial-exit rule.
7. Only after those gates, add a separately reviewed `kind='gap_and_go'`
   shared-executor adapter behind a new default-off live flag.

