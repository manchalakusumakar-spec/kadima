# KADIMA checklist forecaster — observation release

This adds an actual OpenAI Responses API producer, not a fabricated forecast or a reader waiting for someone else's model. It runs separately from entry/exit processing in the engine. A second lightweight thread scores outcomes independently of model-request latency. No model output can place orders or defer exits in this release.

## Scope and checklist

Default scope is the four core indices (NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY) plus the one held underlying when it differs. A fresh held-stock snapshot is reused from the existing stock worker; no extra broker polling is added. The top-20 stock gamma snapshot is context, not 210 independent model forecasts.

Prompt version `kadima-checklist-1` follows Anticipate → Forecast → Early warning → Confirm → Enter → Protect → Exit. Its evidence checklist covers VWAP, EMA, RSI 50, higher lows/lower highs, MA55, Ichimoku, volume, nearby option OI/change, ladder PCR, fitted IV/Greeks, core-index agreement, gamma tiers and dated macro/news/event context. Scores and heat are not win probabilities.

The input collector explicitly identifies missing global/GIFT/US/Asia/DXY/FX/yield/oil feeds, weighted heavyweight/sector breadth, intraday institutional flows and order-flow measurements. Dedicated IFI monitoring and OI-velocity history are not connected. Supplied dated IMF/World Bank/BIS/FSB/ADB/central-bank news is conditional context only. This is a versioned implementation of the discussed checklist, not a claim that every checklist feed exists.

## Outputs and timing

* Every included underlying gets 12 low/expected/high PRICE scenarios at 5-minute checkpoints through 60 minutes, bias, heat -5…+5, an evidence explanation and conditional invalidation.
* Report timestamps include input capture, source spot/candle/chain times, model issuance and target times, with timezone offsets. The dashboard displays IST. A refresh does not restamp old data.
* One model batch every 300 seconds, up to 100 attempts per IST calendar day persisted across restarts. One request attempt per cycle; 45-second network timeout, 12,000 output-token ceiling. There are no live web tools in the model request.
* Fresh spot receipt must be ≤30 seconds old. Closed 3-minute candle end must be ≤240 seconds old. A complete ladder sample must be ≤30 seconds old to be included; otherwise the option input is explicitly missing. Invalid model responses, wrong/duplicate assets, nonfinite or inverted ranges, incomplete horizons and responses taking ≥120 seconds are rejected.
* Forecast production uses the engine's configured market window, weekdays and fresh-input gate. It stops new one-hour forecasts one hour before configured close (currently 14:40 IST for the existing 15:40 configuration). This release does not verify or change exchange/holiday schedules. Last forecasts remain labeled expired outside their horizon.
* News uses each article's publication time (≤24 hours); India VIX uses its own source timestamp (≤180 seconds). Macro file write time is not a fresh news/quote timestamp. Stock gamma context must be ≤120 seconds old.

A valid held contract additionally gets a separate 30-minute premium scenario. It must match the exact token/symbol/exchange in the current expiry ladder and have fresh underlying and option quotes. IV is fitted to the observed premium; Black–Scholes uses the existing 6.5% rate and remaining expiry time. Repricing uses the model's 30-minute expected underlying, fixed fitted IV, and separate ±20% IV sensitivity scenarios. Costs/slippage are not included. These are assumptions, not probability bounds. Invalid expiry/IV fit or missing exact-contract data produces an explicit unavailable state; nearby contracts are never substituted.

## Isolation and security

`kadima_forecast_observations.json` is separate from the execution adapter's `kadima_forecasts.json`. It has `mode: observation`, no executable `projections` list, and the adapter explicitly rejects observation-mode files even if copied to its input path. Existing live-order flags, trading eligibility, structural stops and ordinary trailing are unchanged. This package does not enable forecast exit deferral.

The OpenAI key is configured locally with a hidden prompt, stored as root:ubuntu mode 0640 in `/etc/kadima/forecaster.json`, and never sent to the dashboard, market-data input, journal or logs. Requests send only selected market data/headlines, not broker credentials, account balances or position quantities. They use `store: false`; that setting is not a general zero-retention guarantee. The configured model must support Responses structured outputs. Default: `gpt-5-mini`.

Official API references: [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) and [GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini). A valid OpenAI API key and outbound HTTPS to `api.openai.com` are required on AWS. Model requests can incur API charges; quality/behavior is not assumed identical to an interactive ChatGPT forecast.

## Deployment

Use the pinned commit supplied with the release. The installer checks hashes, permits maintenance only from 19:00 to 08:00 IST, requires local and broker flat/no-pending-order state, captures the existing service environment privately, backs up source, restarts previously active services, and restores source if deployment fails. No test orders are sent. This is the existing repository's maintenance deployment gate, not a request to close positions automatically.

After extracting `kadima forecaster_update`:

```bash
sudo -n python3 "$release/forecaster_update/install.py"
sudo -n python3 "$release/forecaster_update/configure.py"
sudo -n python3 "$release/forecaster_update/update_dashboard.py"
python3 "$release/forecaster_update/report.py"
```

The configuration prompt accepts the default model with Enter. Paste an **OpenAI API key**, not an Angel One credential or KADIMA bearer token, at the hidden key prompt. Blank retains an existing key or leaves the observer waiting. Settings reload within 5 minutes without another restart. Refresh the dashboard with Ctrl+F5 for the new observation panel. Existing authentication and scrolling are retained; no token is embedded in HTML.

The report is also included in the already authenticated `/dashboard_data` response. Local output files are under `/home/ubuntu/kadima-py/state/`:

* `kadima_forecast_status.json`: current observer state, checked time and validation counts.
* `kadima_forecast_observations.json`: latest validated forecast, source availability and contract scenarios.
* `forecast_journal.sqlite`: append-before-publication inputs/output and per-target outcomes.

Typical states: `waiting_for_api_key`, `outside_one_hour_forecast_window`, `waiting_for_fresh_market_inputs`, `forecast_recorded`, `provider_http_401/429`, `daily_request_limit_reached`, or sanitized cycle failure class. API response bodies are not logged. Missing-key/closed-market status does not mean the thread is hanging. The forecast panel patch makes its own HTML backup and stops without edits for an unrecognized layout.

## Validation, not claimed accuracy

Offline tests use synthetic fixtures and mocked transport. They verify the Responses request shape, rejection paths, source freshness, exact CE/PE repricing and expiry handling, durable request limits, immutable forecast records, target scoring, private writes, observer isolation and unchanged executor tests. JavaScript syntax and escaping are checked separately.

Actual observations use the first source receipt at or after a target within 30 seconds. Earlier, late, absent and stale samples never count as hits; missing targets remain unscored. The journal does not retrospectively rewrite forecasts or fill outages. Metrics are separated by asset/horizon: sample count, point MAE, unchanged-price baseline MAE and scenario-range hit fraction. Report total versus scored checkpoints to expose coverage. Overlapping forecasts are not independent samples. Option-premium scenario accuracy is not yet scored by this underlying-price tracker.

No live model call, next-session accuracy, or broker fill has been verified in this offline release. Observe several sessions and inspect coverage and errors before proposing any execution connection. Forecast-based exit authority requires a separate explicit change and review; it is not an automatic upgrade after a sample count is reached.
