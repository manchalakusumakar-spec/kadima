# Stock execution and actual forecast connection

## Stock path

`stock_universe_scanner` → durable SQLite `stock_handoff` row → engine `StockExecutionWorker` → existing `AutoExecutor` → durable order ledger → broker-confirmed fills.

The scanner remains a separate process and never imports an executor or places orders. It publishes only its real complete chain, score, direction, underlying quote and broker `avgPrice` (session average price/VWAP input). Actual nearest-expiry time replaces the old hard-coded two-day expiry. Non-confirmed results, failed requests and demotion invalidate eligibility. Confirmed episodes retain their signal ID across polls and process restarts; an actual tier/direction/day transition creates a new episode.

The worker runs in the existing engine process and shares its executor. It retrieves closed three- and five-minute broker candles for each fresh candidate, outside the exit-monitor thread and executor lock. It rejects malformed, non-finite, out-of-order, missing intraday or stale candles. Forming candles are excluded. It computes EMA/RSI from closed history, uses the broker session-average price for VWAP alignment, and supplies the same structural-stop and entry gates as index entries.

It preserves the original chain/underlying receipt times, verifies contracts against the current master, and rechecks the durable signal immediately before submission. Expired/replaced signals cannot be refreshed by merely reading the handoff. Pending index or stock orders reserve the same single slot. There is no second executor or order path. Open stock positions continue receiving structural updates even after leaving the scanner shortlist; ordinary premium trailing continues through the independent monitor.

The existing scanner still gamma-scores its bounded shortlist of 20; this patch does not score all F&O chains simultaneously. The worker's historical cache holds one underlying. Historical requests use a 1.1-second local spacing; this is a conservative workload choice, not an assertion about the broker's maximum quota. Existing SESSION quote scheduling remains shared with other services. Slow/unavailable data results in missed entries, not relaxed checks.

Historical request structure follows the [official SmartAPI Python SDK](https://github.com/angel-one/smartapi-python#usage). Broker historical bars/session average prices still need comparison with live charts in the next session. Tests use fixtures, not live orders.

## Forecast connection: adapter ready, producer missing

No actual next-30-minute KADIMA forecasting model or producer was found in the repository. The earlier `forecast_trail.py` only validates and consumes projections; the IV-based entry-return estimate is not a 30-minute forecast. This change does not fabricate one.

`forecast_connection.py` reads private local output from `KADIMA_FORECAST_PATH`, defaulting to `/home/ubuntu/kadima-py/state/kadima_forecasts.json`. A real producer must write this versioned JSON atomically (temporary file plus rename):

```json
{
  "schema_version": 1,
  "projections": [
    {
      "token": "EXACT_OPTION_TOKEN",
      "exchange": "NFO",
      "signal_id": 123,
      "generated_at": "2026-09-14T10:00:00+05:30",
      "market_data_at": "2026-09-14T10:00:00+05:30",
      "target_at": "2026-09-14T10:30:00+05:30",
      "expected_premium": 125.0,
      "model_id": "ACTUAL_PRODUCER_VERSION"
    }
  ]
}
```

This is schema documentation with synthetic example values, not an actionable forecast. `expected_premium` must be the actual model's option-premium prediction for that exact open position. Do not insert a target, signal score, win probability, or underlying-index price here. The adapter does not perform unverified unit conversions.

Every monitor cycle checks position identity, input and generation times, horizon, numeric values and duplicates. Missing, stale, wrong-contract or malformed output clears accepted projections, so a previous optimistic forecast cannot persist indefinitely. Outputs older than 60 seconds are unusable. The existing `FORECAST_TRAIL_ENABLED` flag is preserved; it must be explicitly configured when the real producer is connected. Structural/daily/EOD protection is not deferred by forecasts.

Connection status is exposed under `/dashboard_data` → `forecast_connection`, with `status`, `accepted`, `checked_at` and `trailing_enabled`. State changes emit `forecast_connection_status` audit events. Missing producer output is reported as `producer_output_missing`, not connected.

Run `python3 connection_update/diagnose_forecast.py` on AWS to list candidate existing producer source files and function names without printing credentials/source content. The actual producer file, service or output is needed to finish this connection and enable forecast-based deferral.

## Deployment and verification

Use the pinned `connection_update/install.py` package. It preserves trading flags and credentials, requires the existing maintenance window and a flat local/broker account, validates file hashes, backs up changed files and restarts prior active services. Source rollback is automatic on installation failure; the database is not rolled back. Installing this update allows qualified stock signals to enter through the central executor when the existing live-order flag is already enabled.

Offline tests cover durable episodes, stale/invalidation gates, closed candle validation, contract metadata, shared stock/index slots, a stock handoff reaching one pending order, forecast identity/freshness/horizon rejection, plus the prior stop/risk and confirmed-fill regressions. No test orders were submitted to a broker. Installation success still requires next-session observation of real data and fills.
