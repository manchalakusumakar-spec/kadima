# Step 2 — offline replay at 52010ce

**Requested source:** `claude/intraday-trend-integration` @ `52010ceaa4f5e46673b2ce1149a66733b7dda1f2`. All 70 Python package files were retrieved at this exact commit and their Git blobs verified. Lane SHA-256: `7a45fddd0f1e33b85a739748366a8fef6cdb69e034cd1ab7e02ca2b333d953fa`. The source files were not edited.

**Result:** 19 conditional simulated trades; total net **−₹10,489.75** after the same modeled costs. These are research results under the explicit no-historical-external-claims assumption, not an exact live replay. With claims unavailable, the real lane fails closed and produces no trades.

**Step 3: STOPPED. Nothing installed. No changes to `~/kadima-py`.**

## Same data and cost model

The same exported files were reused; every checksum still matches the prior export manifest. No new live market reads or exports were needed. price_history.db has 10 recorded dates, but complete overlapping candle/OI export coverage remains only five sessions: 2026-10-05 through 2026-10-09. The target 15–20 sessions is not available in this export.

|Export|Rows|SHA-256|
|---|---:|---|
|price_metadata.jsonl.gz|524|`76d68461008caf3f718a5cb9d66e58fb965e3e12256b0c83b6da14d0fb3054aa`|
|price_minutes.jsonl.gz|88,999|`de245cbac6fc916b2c7e2a6ac49aa34901b80a0cca4f9b9ccc93ce0196cd764d`|
|candles.jsonl.gz|6,152|`c5dd0a42f70370ef6382f18cfa65fdb25f1037f02747ac02931ddcb09f959403`|
|oi_snapshots.jsonl.gz|145,992|`ca1bbcb1c4c595662011d593db1d3ab1dd5dbc827a99a87c08aa5e0c6f701369`|

The filtered `lot_sizes.json` is also unchanged from the prior replay; it contains current-cache contract lot sizes (NIFTY 65 and BANKNIFTY 30 for the simulated trades), not historical contract-note verification. Schemas remain in [schema.json](/home/ubuntu/codex_handoff/codex_reports/schema.json).

Calibration: **2026-10-05, 06, 07**. Validation: **2026-10-08, 09**. The split is unchanged. No policy numbers or thresholds were changed or tuned.

Cost function is byte-for-byte identical to the previous replay: ₹20 brokerage per executed order, two orders per completed trade; STT 0.15% of sell premium; NSE exchange charges 0.0355299% on both sides; SEBI ₹10/crore; IPFT 0.0000001%; GST 18% on brokerage/exchange/SEBI/IPFT; stamp duty 0.003% of buy premium. The earlier public tariff assumption is retained, without asserting account-specific or historical verification. Reference: [Angel One pricing](https://www.angelone.in/exchange-transaction-charges).

Spread assumption is unchanged: total spread **1% of LTP**, buy at LTP + 0.5%, sell at LTP − 0.5%. Bid/ask is absent from the exported OI samples and price-minute schema. No measured historical spread is substituted.

## How the new source is replayed

- Calls `intraday_trend.detect` on only already-closed 5-minute candles and `Lane.prepare` at observed chain-poll timestamps. No future poll, incomplete bar or day-end price is read as decision-time evidence.
- The underlying spot/time approximation is unchanged: most recent closed 1-minute close, stamped at its theoretical close time. Exact tick freshness and actual thread scheduling cannot be recovered. At equal timestamps, 1-minute closes precede 3-minute and 5-minute closes, followed by OI polls.
- The existing 3-minute indicator calculation uses EMA33/EMA83, RSI and its 150-bar lookback. The requested source checks EMA and RSI alignment without the old VWAP alignment leg. Index candle volumes are zero. The beginning of the export has no earlier indicator warmup history, a limitation retained from the first replay.
- Uses the source structure stop, shared ratchet, 45-minute no-progress exit below +5% peak, 15:10 time exit, 1 lot and source episode/re-entry rules. All policy values are read from the unchanged requested source. Exit marks are sampled OI LTP; intrapoll touches and actual LIMIT fills are not available.
- Calls the new `strike_claims` function using simulated intraday holdings. **Assumption for the comparable research scenario:** no historical external Gamma/scalping/CAS/manual holdings or candidate claims. Those time-specific records are not in the reused export. No live claim state is used to stand in for the past.
- Also runs a fail-closed control with `claims=None`. This preserves the source gate rather than pretending unavailable historical claims are known.
- Evaluates the new `celestial_exit_guard` on 746 eligible holding checks using timestamped quotes in the temporary offline database. The source spread threshold is 3%, and the unchanged assumed spread is 1%, so this scenario cannot establish historical spread-widening exits. General Celestial scalp/news/opposite-Gamma exits are not applied to intraday positions, as specified by this source.
- The observation-only reason is retained. SETUP_READY records are simulated without setting a live flag, importing the broker SDK or creating AutoExecutor. Shared cash/daily-loss admission, pending broker reservations, external/manual-position conflicts, contract-fill behavior and the separate shared Celestial trail override are not reconstructed. The shared ratchet calculation remains the same as the previous replay.
- Requires a fresh observed quote to exit. No exit is filled from a later day-end summary. Zero trades were censored in this run.

## Per-index results — conditional research scenario

|Split|Index|Trades|Wins|Losses|Net ₹|Average ₹|Worst ₹|Max realized drawdown ₹|
|---|---|---:|---:|---:|---:|---:|---:|---:|
|calibration|NIFTY|3|1|2|-2,004.26|-668.09|-1,177.43|2,234.91|
|calibration|BANKNIFTY|6|2|4|-4,381.12|-730.19|-1,841.21|4,381.12|
|calibration|ALL|9|3|6|-6,385.38|-709.49|-1,841.21|6,616.03|
|validation|NIFTY|5|2|3|-1,236.39|-247.28|-795.09|1,845.25|
|validation|BANKNIFTY|5|2|3|-2,867.98|-573.60|-1,672.07|2,988.07|
|validation|ALL|10|4|6|-4,104.37|-410.44|-1,672.07|4,833.32|

Drawdown is computed from realized completed-trade P&L, beginning at zero; it is not a continuous marked-to-market portfolio drawdown.

## Per-setup results

|Split|Index|Family|Setups|Trades|Wins|Losses|Net ₹|Average ₹|Worst ₹|Drawdown ₹|
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|calibration|BANKNIFTY|afternoon_breakout|0|0|0|0|0.00|N/A|N/A|N/A|
|calibration|BANKNIFTY|opening_continuation|5|2|0|2|-3,514.11|-1,757.05|-1,841.21|3,514.11|
|calibration|BANKNIFTY|pullback_recovery|11|4|2|2|-867.01|-216.75|-1,613.60|1,613.60|
|calibration|NIFTY|afternoon_breakout|0|0|0|0|0.00|N/A|N/A|N/A|
|calibration|NIFTY|opening_continuation|7|1|0|1|-1,177.43|-1,177.43|-1,177.43|1,177.43|
|calibration|NIFTY|pullback_recovery|22|2|1|1|-826.83|-413.42|-1,057.48|1,057.48|
|validation|BANKNIFTY|afternoon_breakout|0|0|0|0|0.00|N/A|N/A|N/A|
|validation|BANKNIFTY|opening_continuation|0|0|0|0|0.00|N/A|N/A|N/A|
|validation|BANKNIFTY|pullback_recovery|12|5|2|3|-2,867.98|-573.60|-1,672.07|2,988.07|
|validation|NIFTY|afternoon_breakout|0|0|0|0|0.00|N/A|N/A|N/A|
|validation|NIFTY|opening_continuation|8|3|0|3|-1,845.25|-615.08|-795.09|1,845.25|
|validation|NIFTY|pullback_recovery|8|2|2|0|608.86|304.43|237.24|0.00|

## Rejections and exits

Detection/admission counts are per decision/poll attempt. A setup can be rejected on repeated polls; rejection totals are not unique-setup counts. Ranked-contract counts are separate. ALL rows contain bar-level gates such as warmup or time cutoff. No rejected setup is counted as a losing trade.

### calibration / BANKNIFTY / ALL

Detection/admission rejections: {"after_last_entry": 57, "before_first_decision": 9}
Ranked-contract rejections: {}
Completed-trade exits: {}

### calibration / BANKNIFTY / afternoon_breakout

Detection/admission rejections: {"no_consolidation": 54, "outside_afternoon_window": 121}
Ranked-contract rejections: {}
Completed-trade exits: {}

### calibration / BANKNIFTY / opening_continuation

Detection/admission rejections: {"breakout_failed_before_entry": 1, "no_close_beyond_opening_range": 30, "outside_opening_window": 144, "previous_intraday_trend_exit_unconfirmed": 11, "trend_not_aligned:ema": 1, "underlying_quote_stale": 11}
Ranked-contract rejections: {"ranking:OI 56880 below floor 100000": 1, "ranking:OI 59130 below floor 100000": 1, "ranking:OI 75150 below floor 100000": 1, "ranking:OI 76350 below floor 100000": 1, "ranking:OI 76530 below floor 100000": 1, "ranking:OI 94170 below floor 100000": 1, "ranking:OI 96870 below floor 100000": 1, "ranking:delta 0.37 outside 0.45-0.6": 1, "ranking:delta 0.42 outside 0.45-0.6": 1, "ranking:delta 0.44 outside 0.45-0.6": 1}
Completed-trade exits: {"structural_stop_breached": 2}

### calibration / BANKNIFTY / pullback_recovery

Detection/admission rejections: {"breakout_extended": 5, "breakout_failed_before_entry": 4, "daily_episode_cap_reached": 2, "ema_warmup": 7, "no_recovery_close": 93, "no_shallow_pullback_to_ema": 30, "outside_pullback_window": 6, "previous_intraday_trend_exit_unconfirmed": 11, "setup_not_new_since_previous_exit": 4, "trend_not_aligned:ema": 4, "trend_not_aligned:ema,rsi": 19, "underlying_quote_stale": 16}
Ranked-contract rejections: {"ranking:OI 53490 below floor 100000": 1, "ranking:OI 55020 below floor 100000": 1, "ranking:OI 62580 below floor 100000": 1, "ranking:OI 68070 below floor 100000": 1, "ranking:OI 70710 below floor 100000": 1, "ranking:OI 70950 below floor 100000": 1, "ranking:OI 80880 below floor 100000": 1, "ranking:OI 89070 below floor 100000": 1, "ranking:OI 98940 below floor 100000": 1, "ranking:delta 0.44 outside 0.45-0.6": 2, "ranking:delta 0.61 outside 0.45-0.6": 2, "ranking:delta 0.63 outside 0.45-0.6": 2, "ranking:delta 0.64 outside 0.45-0.6": 3}
Completed-trade exits: {"dynamic_profit_trail_breached": 1, "intraday_trend_no_progress_45min": 2, "structural_stop_breached": 1}

### calibration / NIFTY / ALL

Detection/admission rejections: {"after_last_entry": 57, "before_first_decision": 9}
Ranked-contract rejections: {}
Completed-trade exits: {}

### calibration / NIFTY / afternoon_breakout

Detection/admission rejections: {"no_consolidation": 54, "outside_afternoon_window": 119}
Ranked-contract rejections: {}
Completed-trade exits: {}

### calibration / NIFTY / opening_continuation

Detection/admission rejections: {"breakout_extended": 5, "historical_lot_size_unavailable": 23, "no_close_beyond_opening_range": 21, "no_contract_meets_intraday_policy": 1, "outside_opening_window": 144, "trend_not_aligned:ema,rsi": 3, "underlying_quote_stale": 24}
Ranked-contract rejections: {"invalid_premium_stop": 1, "ranking:delta 0.04 outside 0.45-0.6": 11, "ranking:delta 0.05 outside 0.45-0.6": 10, "ranking:delta 0.06 outside 0.45-0.6": 3, "ranking:delta 0.07 outside 0.45-0.6": 5, "ranking:delta 0.08 outside 0.45-0.6": 12, "ranking:delta 0.09 outside 0.45-0.6": 4, "ranking:delta 0.11 outside 0.45-0.6": 3, "ranking:delta 0.12 outside 0.45-0.6": 3, "ranking:delta 0.13 outside 0.45-0.6": 5, "ranking:delta 0.14 outside 0.45-0.6": 9, "ranking:delta 0.15 outside 0.45-0.6": 1, "ranking:delta 0.16 outside 0.45-0.6": 3, "ranking:delta 0.18 outside 0.45-0.6": 3, "ranking:delta 0.20 outside 0.45-0.6": 1, "ranking:delta 0.21 outside 0.45-0.6": 3, "ranking:delta 0.22 outside 0.45-0.6": 5, "ranking:delta 0.23 outside 0.45-0.6": 7, "ranking:delta 0.24 outside 0.45-0.6": 1, "ranking:delta 0.25 outside 0.45-0.6": 1, "ranking:delta 0.26 outside 0.45-0.6": 3, "ranking:delta 0.27 outside 0.45-0.6": 2, "ranking:delta 0.28 outside 0.45-0.6": 2, "ranking:delta 0.31 outside 0.45-0.6": 2, "ranking:delta 0.32 outside 0.45-0.6": 3, "ranking:delta 0.33 outside 0.45-0.6": 2, "ranking:delta 0.34 outside 0.45-0.6": 3, "ranking:delta 0.35 outside 0.45-0.6": 5, "ranking:delta 0.36 outside 0.45-0.6": 4, "ranking:delta 0.37 outside 0.45-0.6": 1, "ranking:delta 0.39 outside 0.45-0.6": 4, "ranking:delta 0.40 outside 0.45-0.6": 2, "ranking:delta 0.41 outside 0.45-0.6": 1, "ranking:delta 0.44 outside 0.45-0.6": 2, "ranking:delta 0.60 outside 0.45-0.6": 2, "ranking:delta 0.61 outside 0.45-0.6": 3, "ranking:delta 0.62 outside 0.45-0.6": 2, "ranking:delta 0.63 outside 0.45-0.6": 3, "ranking:delta 0.64 outside 0.45-0.6": 1, "ranking:delta 0.65 outside 0.45-0.6": 3, "ranking:delta 0.66 outside 0.45-0.6": 6, "ranking:delta 0.67 outside 0.45-0.6": 2, "ranking:delta 0.70 outside 0.45-0.6": 2, "ranking:delta 0.71 outside 0.45-0.6": 4, "ranking:delta 0.72 outside 0.45-0.6": 2, "ranking:delta 0.73 outside 0.45-0.6": 1, "ranking:delta 0.74 outside 0.45-0.6": 1, "ranking:delta 0.75 outside 0.45-0.6": 1, "ranking:delta 0.76 outside 0.45-0.6": 4, "ranking:delta 0.77 outside 0.45-0.6": 6, "ranking:delta 0.78 outside 0.45-0.6": 3, "ranking:delta 0.79 outside 0.45-0.6": 2, "ranking:delta 0.80 outside 0.45-0.6": 3, "ranking:delta 0.81 outside 0.45-0.6": 2, "ranking:delta 0.82 outside 0.45-0.6": 3, "ranking:delta 0.83 outside 0.45-0.6": 1, "ranking:delta 0.84 outside 0.45-0.6": 3, "ranking:delta 0.85 outside 0.45-0.6": 1, "ranking:delta 0.86 outside 0.45-0.6": 9, "ranking:delta 0.87 outside 0.45-0.6": 4, "ranking:delta 0.88 outside 0.45-0.6": 3, "ranking:delta 0.89 outside 0.45-0.6": 4, "ranking:delta 0.90 outside 0.45-0.6": 6, "ranking:delta 0.91 outside 0.45-0.6": 2, "ranking:delta 0.92 outside 0.45-0.6": 5, "ranking:delta 0.93 outside 0.45-0.6": 4, "ranking:delta 0.94 outside 0.45-0.6": 6, "ranking:delta 0.95 outside 0.45-0.6": 1, "ranking:delta 0.96 outside 0.45-0.6": 3, "ranking:delta 0.97 outside 0.45-0.6": 2, "ranking:delta 0.98 outside 0.45-0.6": 2, "ranking:delta 1.00 outside 0.45-0.6": 7}
Completed-trade exits: {"structural_stop_breached": 1}

### calibration / NIFTY / pullback_recovery

Detection/admission rejections: {"breakout_extended": 4, "breakout_failed_before_entry": 8, "daily_episode_cap_reached": 18, "ema_warmup": 7, "historical_lot_size_unavailable": 26, "no_contract_meets_intraday_policy": 14, "no_recovery_close": 87, "no_shallow_pullback_to_ema": 21, "outside_pullback_window": 8, "previous_intraday_trend_exit_unconfirmed": 4, "trend_not_aligned:ema": 4, "trend_not_aligned:ema,rsi": 19, "trend_not_aligned:rsi": 1, "underlying_quote_stale": 66}
Ranked-contract rejections: {"ranking:delta 0.01 outside 0.45-0.6": 2, "ranking:delta 0.02 outside 0.45-0.6": 12, "ranking:delta 0.03 outside 0.45-0.6": 23, "ranking:delta 0.04 outside 0.45-0.6": 12, "ranking:delta 0.05 outside 0.45-0.6": 16, "ranking:delta 0.06 outside 0.45-0.6": 10, "ranking:delta 0.07 outside 0.45-0.6": 4, "ranking:delta 0.08 outside 0.45-0.6": 5, "ranking:delta 0.09 outside 0.45-0.6": 8, "ranking:delta 0.10 outside 0.45-0.6": 13, "ranking:delta 0.11 outside 0.45-0.6": 6, "ranking:delta 0.12 outside 0.45-0.6": 3, "ranking:delta 0.13 outside 0.45-0.6": 2, "ranking:delta 0.14 outside 0.45-0.6": 1, "ranking:delta 0.15 outside 0.45-0.6": 2, "ranking:delta 0.16 outside 0.45-0.6": 4, "ranking:delta 0.17 outside 0.45-0.6": 11, "ranking:delta 0.18 outside 0.45-0.6": 6, "ranking:delta 0.19 outside 0.45-0.6": 3, "ranking:delta 0.20 outside 0.45-0.6": 5, "ranking:delta 0.21 outside 0.45-0.6": 3, "ranking:delta 0.22 outside 0.45-0.6": 1, "ranking:delta 0.23 outside 0.45-0.6": 3, "ranking:delta 0.24 outside 0.45-0.6": 1, "ranking:delta 0.25 outside 0.45-0.6": 2, "ranking:delta 0.26 outside 0.45-0.6": 3, "ranking:delta 0.27 outside 0.45-0.6": 3, "ranking:delta 0.28 outside 0.45-0.6": 5, "ranking:delta 0.29 outside 0.45-0.6": 7, "ranking:delta 0.30 outside 0.45-0.6": 5, "ranking:delta 0.31 outside 0.45-0.6": 2, "ranking:delta 0.32 outside 0.45-0.6": 2, "ranking:delta 0.33 outside 0.45-0.6": 4, "ranking:delta 0.34 outside 0.45-0.6": 2, "ranking:delta 0.35 outside 0.45-0.6": 3, "ranking:delta 0.36 outside 0.45-0.6": 1, "ranking:delta 0.37 outside 0.45-0.6": 2, "ranking:delta 0.39 outside 0.45-0.6": 3, "ranking:delta 0.40 outside 0.45-0.6": 6, "ranking:delta 0.41 outside 0.45-0.6": 4, "ranking:delta 0.42 outside 0.45-0.6": 1, "ranking:delta 0.43 outside 0.45-0.6": 4, "ranking:delta 0.44 outside 0.45-0.6": 2, "ranking:delta 0.45 outside 0.45-0.6": 7, "ranking:delta 0.60 outside 0.45-0.6": 2, "ranking:delta 0.61 outside 0.45-0.6": 5, "ranking:delta 0.62 outside 0.45-0.6": 5, "ranking:delta 0.63 outside 0.45-0.6": 4, "ranking:delta 0.64 outside 0.45-0.6": 4, "ranking:delta 0.65 outside 0.45-0.6": 4, "ranking:delta 0.66 outside 0.45-0.6": 1, "ranking:delta 0.67 outside 0.45-0.6": 1, "ranking:delta 0.68 outside 0.45-0.6": 1, "ranking:delta 0.69 outside 0.45-0.6": 4, "ranking:delta 0.70 outside 0.45-0.6": 4, "ranking:delta 0.71 outside 0.45-0.6": 4, "ranking:delta 0.72 outside 0.45-0.6": 2, "ranking:delta 0.73 outside 0.45-0.6": 1, "ranking:delta 0.74 outside 0.45-0.6": 4, "ranking:delta 0.75 outside 0.45-0.6": 4, "ranking:delta 0.76 outside 0.45-0.6": 4, "ranking:delta 0.77 outside 0.45-0.6": 3, "ranking:delta 0.78 outside 0.45-0.6": 3, "ranking:delta 0.79 outside 0.45-0.6": 4, "ranking:delta 0.80 outside 0.45-0.6": 11, "ranking:delta 0.81 outside 0.45-0.6": 3, "ranking:delta 0.82 outside 0.45-0.6": 3, "ranking:delta 0.83 outside 0.45-0.6": 3, "ranking:delta 0.84 outside 0.45-0.6": 3, "ranking:delta 0.85 outside 0.45-0.6": 4, "ranking:delta 0.86 outside 0.45-0.6": 6, "ranking:delta 0.87 outside 0.45-0.6": 6, "ranking:delta 0.88 outside 0.45-0.6": 7, "ranking:delta 0.89 outside 0.45-0.6": 5, "ranking:delta 0.90 outside 0.45-0.6": 6, "ranking:delta 0.91 outside 0.45-0.6": 11, "ranking:delta 0.92 outside 0.45-0.6": 11, "ranking:delta 0.93 outside 0.45-0.6": 3, "ranking:delta 0.94 outside 0.45-0.6": 8, "ranking:delta 0.95 outside 0.45-0.6": 9, "ranking:delta 0.96 outside 0.45-0.6": 4, "ranking:delta 0.97 outside 0.45-0.6": 5, "ranking:delta 0.98 outside 0.45-0.6": 2, "ranking:delta 0.99 outside 0.45-0.6": 2, "ranking:delta 1.00 outside 0.45-0.6": 28, "stop_wider_than_policy": 2}
Completed-trade exits: {"dynamic_profit_trail_breached": 1, "structural_stop_breached": 1}

### validation / BANKNIFTY / ALL

Detection/admission rejections: {"after_last_entry": 14, "before_first_decision": 6, "invalid_bars": 34}
Ranked-contract rejections: {}
Completed-trade exits: {}

### validation / BANKNIFTY / afternoon_breakout

Detection/admission rejections: {"no_consolidation": 18, "outside_afternoon_window": 82}
Ranked-contract rejections: {}
Completed-trade exits: {}

### validation / BANKNIFTY / opening_continuation

Detection/admission rejections: {"no_close_beyond_opening_range": 24, "outside_opening_window": 76}
Ranked-contract rejections: {}
Completed-trade exits: {}

### validation / BANKNIFTY / pullback_recovery

Detection/admission rejections: {"breakout_extended": 2, "breakout_failed_before_entry": 2, "daily_episode_cap_reached": 8, "ema_warmup": 6, "no_recovery_close": 57, "no_shallow_pullback_to_ema": 7, "outside_pullback_window": 6, "previous_intraday_trend_exit_unconfirmed": 8, "setup_not_new_since_previous_exit": 7, "trend_not_aligned:ema,rsi": 10, "underlying_quote_stale": 27}
Ranked-contract rejections: {"ranking:OI 63870 below floor 100000": 1, "ranking:OI 69300 below floor 100000": 1, "ranking:OI 70500 below floor 100000": 1, "ranking:OI 75480 below floor 100000": 1, "ranking:OI 76260 below floor 100000": 1, "ranking:OI 83040 below floor 100000": 1, "ranking:OI 97170 below floor 100000": 1, "ranking:delta 0.35 outside 0.45-0.6": 1, "ranking:delta 0.36 outside 0.45-0.6": 1, "ranking:delta 0.38 outside 0.45-0.6": 1, "ranking:delta 0.41 outside 0.45-0.6": 1, "ranking:delta 0.42 outside 0.45-0.6": 2, "ranking:delta 0.43 outside 0.45-0.6": 2, "ranking:delta 0.44 outside 0.45-0.6": 3, "ranking:delta 0.45 outside 0.45-0.6": 2, "ranking:delta 0.60 outside 0.45-0.6": 1, "ranking:delta 0.61 outside 0.45-0.6": 1, "ranking:delta 0.62 outside 0.45-0.6": 1, "ranking:delta 0.63 outside 0.45-0.6": 1, "ranking:delta 0.64 outside 0.45-0.6": 1}
Completed-trade exits: {"dynamic_profit_trail_breached": 1, "intraday_trend_no_progress_45min": 3, "structural_stop_breached": 1}

### validation / NIFTY / ALL

Detection/admission rejections: {"after_last_entry": 14, "before_first_decision": 6, "invalid_bars": 34}
Ranked-contract rejections: {}
Completed-trade exits: {}

### validation / NIFTY / afternoon_breakout

Detection/admission rejections: {"no_consolidation": 18, "outside_afternoon_window": 74}
Ranked-contract rejections: {}
Completed-trade exits: {}

### validation / NIFTY / opening_continuation

Detection/admission rejections: {"breakout_extended": 9, "breakout_failed_before_entry": 1, "daily_episode_cap_reached": 15, "no_close_beyond_opening_range": 4, "outside_opening_window": 76, "setup_not_new_since_previous_exit": 7, "trend_not_aligned:ema": 3, "underlying_quote_stale": 17}
Ranked-contract rejections: {"ranking:delta 0.21 outside 0.45-0.6": 2, "ranking:delta 0.22 outside 0.45-0.6": 1, "ranking:delta 0.26 outside 0.45-0.6": 3, "ranking:delta 0.30 outside 0.45-0.6": 3, "ranking:delta 0.35 outside 0.45-0.6": 3, "ranking:delta 0.40 outside 0.45-0.6": 3, "ranking:delta 0.60 outside 0.45-0.6": 1, "ranking:delta 0.61 outside 0.45-0.6": 1, "ranking:delta 0.62 outside 0.45-0.6": 1, "ranking:delta 0.65 outside 0.45-0.6": 1, "ranking:delta 0.66 outside 0.45-0.6": 1, "ranking:delta 0.67 outside 0.45-0.6": 1, "ranking:delta 0.69 outside 0.45-0.6": 1, "ranking:delta 0.71 outside 0.45-0.6": 2}
Completed-trade exits: {"dynamic_profit_trail_breached": 1, "structural_stop_breached": 2}

### validation / NIFTY / pullback_recovery

Detection/admission rejections: {"breakout_extended": 3, "breakout_failed_before_entry": 2, "daily_episode_cap_reached": 20, "ema_warmup": 3, "no_recovery_close": 50, "no_shallow_pullback_to_ema": 15, "outside_pullback_window": 5, "previous_intraday_trend_exit_unconfirmed": 1, "trend_not_aligned:ema,rsi": 8, "underlying_quote_stale": 20}
Ranked-contract rejections: {"ranking:delta 0.21 outside 0.45-0.6": 1, "ranking:delta 0.22 outside 0.45-0.6": 1, "ranking:delta 0.26 outside 0.45-0.6": 1, "ranking:delta 0.28 outside 0.45-0.6": 1, "ranking:delta 0.32 outside 0.45-0.6": 1, "ranking:delta 0.34 outside 0.45-0.6": 1, "ranking:delta 0.38 outside 0.45-0.6": 1, "ranking:delta 0.40 outside 0.45-0.6": 1, "ranking:delta 0.45 outside 0.45-0.6": 1, "ranking:delta 0.63 outside 0.45-0.6": 1, "ranking:delta 0.65 outside 0.45-0.6": 1, "ranking:delta 0.69 outside 0.45-0.6": 1, "ranking:delta 0.70 outside 0.45-0.6": 1, "ranking:delta 0.73 outside 0.45-0.6": 1, "ranking:delta 0.74 outside 0.45-0.6": 1, "ranking:delta 0.77 outside 0.45-0.6": 1, "ranking:delta 0.79 outside 0.45-0.6": 1}
Completed-trade exits: {"dynamic_profit_trail_breached": 2}

## Historical strike claims and fail-closed control

Historical `held_by_X` / `strike_claimed_by_X` counts: **NOT RECONSTRUCTABLE**. In the simulated-only assumption scenario, no claim rejections were generated; that does not mean there were no competing-engine claims in the real sessions. The actual source implementation is present and unchanged.

|Control split|Index|strike_claims_unavailable rejection attempts|Trades|
|---|---|---:|---:|
|calibration|NIFTY|95|0|
|calibration|BANKNIFTY|44|0|
|validation|NIFTY|59|0|
|validation|BANKNIFTY|40|0|

The control produces no admitted trades because required claim evidence is unavailable. This is missing evidence, not a zero-profit performance estimate.

## Comparison with the previous pinned-source run

|Split|Old 7c67c67 trades|52010ce trades|Old net ₹|52010ce net ₹|
|---|---:|---:|---:|---:|
|calibration|8|9|-4,306.49|-6,385.38|
|validation|9|10|-607.26|-4,104.37|

This comparison holds data, costs, spread and time/spot approximations fixed. It compares the two requested source versions; no parameter was optimized using the validation days. The earlier result is retained in `02_replay_7c67c67_previous.md` and `replay_results_7c67c67_previous.json`.

## Verification and artifacts

All 70 package source hashes remain unchanged. The lane policy dictionary is unchanged. Export checksums match the prior manifest. Cost function text matches the saved prior function. Trade arithmetic was checked for all 19 completed trades. Both scenarios loaded no broker SDK and made zero session/network guard calls. All live Python hashes remain identical to Step 1.

Replay script: `/home/ubuntu/codex_handoff/tools/replay_intraday_trend.py` (updated locally for this requested source/API). No branch update, commit or publication was performed in this follow-up.
Results: `replay_results_52010ce.json`; fail-closed control: `replay_results_52010ce_claims_unavailable.json`; source verification: `replay_52010ce_source_manifest.json`; checks: `replay_52010ce_verification.json`.

Exact primary command:
```bash
PYTHONDONTWRITEBYTECODE=1 /home/ubuntu/kadima-py/venv/bin/python tools/replay_intraday_trend.py \
  --root source/replay_52010ce --data codex_reports \
  --output codex_reports/replay_results_52010ce.json \
  --expected-lane-sha256 7a45fddd0f1e33b85a739748366a8fef6cdb69e034cd1ab7e02ca2b333d953fa \
  --source-commit 52010ceaa4f5e46673b2ce1149a66733b7dda1f2 \
  --source-manifest codex_reports/replay_52010ce_source_manifest.json
```
The control uses the same command with `--claims-mode unavailable` and its separate output filename.

Policy dictionary captured from the unchanged source:
```json
{
  "id": "intraday_trend_v1",
  "interval_min": 5,
  "first_decision": [
    9,
    30
  ],
  "last_entry": [
    14,
    30
  ],
  "opening_window": [
    [
      9,
      30
    ],
    [
      10,
      30
    ]
  ],
  "pullback_window": [
    [
      9,
      45
    ],
    [
      14,
      30
    ]
  ],
  "afternoon_window": [
    [
      13,
      0
    ],
    [
      14,
      30
    ]
  ],
  "consolidation_from": [
    12,
    0
  ],
  "max_extension_ranges": 1.0,
  "max_pullback_ranges": 2.0,
  "max_consolidation_ranges": 3.0,
  "ema_period": 9,
  "technical_max_age_sec": 240,
  "chain_max_age_sec": 30,
  "spot_max_age_sec": 30,
  "context_max_age_sec": 30,
  "max_spread_pct": 1.5,
  "max_stop_pct": 35.0,
  "lots": 1,
  "max_episodes_per_underlying_per_day": 3,
  "no_progress_min": 45,
  "no_progress_gain_pct": 5.0,
  "time_exit": [
    15,
    10
  ],
  "decision_retention_days": 30
}
```
Source constant `INTRADAY_SPREAD_EXIT_PCT = 3.0` is unchanged. Shared ratchet constants remain `ACTIVATION = 0.05`, `RETAIN_PROFIT = 0.50`.

Suggestions only: collect complete historical competing-engine claims and measured spread/tick-time data, and extend the overlapping sample before treating these conditional modeled results as evidence about live deployment. No policy adjustment is proposed or applied.

Live activation performed: NO. Broker orders/logins in this follow-up: NO. Services restarted: NO. Live files changed: NO. Step 3 remains stopped.

## Detailed trade accounting (52010ce replay)

Coverage: **5 overlapping trading days**, 5–9 October 2026: **3 calibration days** (5–7 October) and **2 validation days** (8–9 October). The larger price-history export contains 10 days, but only these five have the overlapping inputs used here. Bid/ask was **assumed, not real**: bid = LTP × 0.995 and ask = LTP × 1.005 (1% total spread). The source, data, cost model and every policy number are unchanged. All 19 original trade outcomes are identical after adding reporting fields.

Only calibration days inform the diagnosis below. Validation rows are descriptive accounting and are not used for tuning or diagnosis.

### Per setup and index after costs

| Split | Index | Setup | Trades | Wins | Losses | Net ₹ |
|---|---|---|---:|---:|---:|---:|
| calibration | NIFTY | opening_continuation | 1 | 0 | 1 | -1,177.43 |
| calibration | NIFTY | pullback_recovery | 2 | 1 | 1 | -826.83 |
| calibration | NIFTY | afternoon_breakout | 0 | 0 | 0 | 0.00 |
| calibration | BANKNIFTY | opening_continuation | 2 | 0 | 2 | -3,514.11 |
| calibration | BANKNIFTY | pullback_recovery | 4 | 2 | 2 | -867.01 |
| calibration | BANKNIFTY | afternoon_breakout | 0 | 0 | 0 | 0.00 |
| validation | NIFTY | opening_continuation | 3 | 0 | 3 | -1,845.25 |
| validation | NIFTY | pullback_recovery | 2 | 2 | 0 | 608.86 |
| validation | NIFTY | afternoon_breakout | 0 | 0 | 0 | 0.00 |
| validation | BANKNIFTY | opening_continuation | 0 | 0 | 0 | 0.00 |
| validation | BANKNIFTY | pullback_recovery | 5 | 2 | 3 | -2,867.98 |
| validation | BANKNIFTY | afternoon_breakout | 0 | 0 | 0 | 0.00 |

### Exit counts and net after costs

| Split | Index | Exit reason | Trades | Net ₹ |
|---|---|---|---:|---:|
| calibration | NIFTY | Structure stop | 2 | -2,234.91 |
| calibration | NIFTY | Trail | 1 | 230.65 |
| calibration | NIFTY | No-progress 45 min | 0 | 0.00 |
| calibration | NIFTY | 15:10 time exit | 0 | 0.00 |
| calibration | BANKNIFTY | Structure stop | 3 | -5,127.71 |
| calibration | BANKNIFTY | Trail | 1 | 926.28 |
| calibration | BANKNIFTY | No-progress 45 min | 2 | -179.69 |
| calibration | BANKNIFTY | 15:10 time exit | 0 | 0.00 |
| calibration | ALL | Structure stop | 5 | -7,362.62 |
| calibration | ALL | Trail | 2 | 1,156.93 |
| calibration | ALL | No-progress 45 min | 2 | -179.69 |
| calibration | ALL | 15:10 time exit | 0 | 0.00 |
| validation | NIFTY | Structure stop | 2 | -1,569.08 |
| validation | NIFTY | Trail | 3 | 332.69 |
| validation | NIFTY | No-progress 45 min | 0 | 0.00 |
| validation | NIFTY | 15:10 time exit | 0 | 0.00 |
| validation | BANKNIFTY | Structure stop | 1 | -1,114.84 |
| validation | BANKNIFTY | Trail | 1 | 445.91 |
| validation | BANKNIFTY | No-progress 45 min | 3 | -2,199.05 |
| validation | BANKNIFTY | 15:10 time exit | 0 | 0.00 |
| validation | ALL | Structure stop | 3 | -2,683.92 |
| validation | ALL | Trail | 4 | 778.60 |
| validation | ALL | No-progress 45 min | 3 | -2,199.05 |
| validation | ALL | 15:10 time exit | 0 | 0.00 |

### Every simulated trade

Times include the date and are IST (UTC+05:30). Entry is the assumed ask; exit is the assumed bid. Peak premium is the highest **observed LTP** from entry through exit, not a tick-level maximum or assumed ask. It may be below entry because entry includes half the assumed spread. The policy peak remains seeded at entry ask as in the original replay; the new observed peak field does not alter exits. Premiums are ₹ per option unit; net includes the recorded lot quantity and all modeled costs.

| Split | Index | Entry time IST | Setup | Strike | Entry ₹ | Exit time IST | Exit ₹ | Peak LTP ₹ | Exit reason | Net ₹ |
|---|---|---|---|---:|---:|---|---:|---:|---|---:|
| calibration | BANKNIFTY | 2026-10-06T09:41:26.569815+05:30 | opening_continuation | 55200 | 844.9035 | 2026-10-06T10:04:50.159192+05:30 | 786.9953 | 854.7000 | Structure stop | -1,841.21 |
| calibration | BANKNIFTY | 2026-10-06T10:35:17.271653+05:30 | pullback_recovery | 55200 | 841.8382 | 2026-10-06T11:20:27.184838+05:30 | 829.7305 | 849.2000 | No-progress 45 min | -469.61 |
| calibration | BANKNIFTY | 2026-10-06T11:50:15.005606+05:30 | pullback_recovery | 55200 | 834.2505 | 2026-10-06T12:35:27.053688+05:30 | 847.4913 | 851.7500 | No-progress 45 min | 289.92 |
| calibration | BANKNIFTY | 2026-10-07T09:40:13.281424+05:30 | opening_continuation | 55000 | 865.1542 | 2026-10-07T09:42:36.834207+05:30 | 812.9150 | 875.9000 | Structure stop | -1,672.90 |
| calibration | NIFTY | 2026-10-07T09:40:25.287035+05:30 | opening_continuation | 22650 | 190.4475 | 2026-10-07T09:42:45.834716+05:30 | 173.4783 | 191.4500 | Structure stop | -1,177.43 |
| calibration | BANKNIFTY | 2026-10-07T10:45:03.028997+05:30 | pullback_recovery | 55300 | 801.1860 | 2026-10-07T11:31:20.754190+05:30 | 835.6010 | 881.0000 | Trail | 926.28 |
| calibration | NIFTY | 2026-10-07T11:36:18.873666+05:30 | pullback_recovery | 22750 | 181.7040 | 2026-10-07T12:03:30.692747+05:30 | 166.5630 | 189.3500 | Structure stop | -1,057.48 |
| calibration | BANKNIFTY | 2026-10-07T12:05:07.338699+05:30 | pullback_recovery | 55300 | 861.8880 | 2026-10-07T12:08:40.564572+05:30 | 811.6215 | 862.0500 | Structure stop | -1,613.60 |
| calibration | NIFTY | 2026-10-07T12:15:01.241732+05:30 | pullback_recovery | 22750 | 185.3220 | 2026-10-07T12:34:45.264419+05:30 | 190.0450 | 201.8000 | Trail | 230.65 |
| validation | NIFTY | 2026-10-08T09:45:24.228846+05:30 | opening_continuation | 22500 | 157.1820 | 2026-10-08T09:49:21.448983+05:30 | 154.0260 | 165.5000 | Trail | -276.17 |
| validation | NIFTY | 2026-10-08T09:55:12.503025+05:30 | opening_continuation | 22500 | 152.2575 | 2026-10-08T09:58:29.370562+05:30 | 141.0910 | 151.5000 | Structure stop | -795.09 |
| validation | NIFTY | 2026-10-08T10:10:09.415333+05:30 | opening_continuation | 22500 | 154.1167 | 2026-10-08T10:11:41.858785+05:30 | 143.2800 | 153.3500 | Structure stop | -773.99 |
| validation | BANKNIFTY | 2026-10-08T10:35:17.495583+05:30 | pullback_recovery | 55200 | 814.1002 | 2026-10-08T11:20:21.459206+05:30 | 796.0000 | 844.9500 | No-progress 45 min | -647.07 |
| validation | BANKNIFTY | 2026-10-08T11:30:20.926123+05:30 | pullback_recovery | 55100 | 870.9832 | 2026-10-08T12:15:48.380286+05:30 | 818.7855 | 866.6500 | No-progress 45 min | -1,672.07 |
| validation | BANKNIFTY | 2026-10-08T13:00:11.427379+05:30 | pullback_recovery | 55000 | 827.3160 | 2026-10-08T13:32:45.569063+05:30 | 845.7500 | 878.9500 | Trail | 445.91 |
| validation | BANKNIFTY | 2026-10-09T10:50:01.182282+05:30 | pullback_recovery | 55300 | 794.0002 | 2026-10-09T10:54:22.035532+05:30 | 760.2297 | 790.0500 | Structure stop | -1,114.84 |
| validation | NIFTY | 2026-10-09T10:50:09.184631+05:30 | pullback_recovery | 22500 | 138.1875 | 2026-10-09T11:21:48.026511+05:30 | 144.9715 | 153.5000 | Trail | 371.62 |
| validation | NIFTY | 2026-10-09T12:50:26.452602+05:30 | pullback_recovery | 22500 | 128.6400 | 2026-10-09T13:00:39.626786+05:30 | 133.3300 | 141.9000 | Trail | 237.24 |
| validation | BANKNIFTY | 2026-10-09T12:25:27.428911+05:30 | pullback_recovery | 55200 | 826.2105 | 2026-10-09T13:10:37.932683+05:30 | 833.7603 | 846.0000 | No-progress 45 min | 120.09 |

### Diagnosis using calibration days only

The three calibration days produced 9 trades, 3 wins and 6 losses, net ₹-6,385.38 after modeled costs.
Calibration structure stop: 5 trades, net ₹-7,362.62.
Calibration trail: 2 trades, net ₹1,156.93.
Calibration no-progress 45 min: 2 trades, net ₹-179.69.
Calibration 15:10 time exit: 0 trades, net ₹0.00.

This short calibration sample shows losses under the stated assumptions; it does not establish a stable performance estimate. No afternoon-breakout trade or 15:10 exit occurred, so these paths have no trade-outcome evidence in the calibration sample. Measured spreads and historical competing-engine claims remain missing. No policy number was changed and no validation-based adjustment was made.

Step 3 remains stopped. No installation, live-tree change, broker login/order or service restart was performed in this follow-up.
