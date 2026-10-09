# KADIMA: wider option-chain data for the hourly forecast

Work in ~/kadima-py (LIVE engine, newer than GitHub). Goal: give the hourly forecast
(kadima/hourly_forecast.py: chain_at / oi_features, and hourly_forecast_model.oi_tilt) a wider and
longer option-chain record, using the broker feed we already have plus NSE's official end-of-day files.
Do NOT scrape https://www.nseindia.com/option-chain or its /api/option-chain-* endpoints (blocked for
cloud IPs and restricted by NSE's terms). Use only the sources named below.

## Hard rules
- No git checkout/pull/reset/stash on the live code. Back up every file before editing to
  ~/backup-codex-optionchain-<timestamp>/.
- Never place, modify or cancel an order. Never print credentials (mask anything token/key/password-like).
- Angel One calls allowed: the market-data/quote calls the engine already uses for the chain poll
  (getMarketData / LTP / OI). Respect the existing rate limiter; the new poll must share it, never
  bypass it, and must never delay the trading engine's own polls (lower priority / skip a cycle if busy).
- Do not touch trading gates, thresholds, stops, sizing, the kill switch or the Celestial config.
- New data goes to $KADIMA_DATA_DIR/forecast.db (new tables), not into trading tables. Do not change
  the oi_snapshots schema or how the trading engine writes it.
- py_compile every changed file, run tools/test_offline.py, tools/test_hourly_forecast.py and every
  tools/test_*.py; show diff -u.
- Never delete files through an unchecked variable (no `rm -rf $VAR/*`). Restart only outside
  09:00-15:45 IST on a weekday (today is Saturday, so a restart at the end is allowed).

## Part 1 - Wide intraday chain snapshot (every 5 minutes)
- New table in forecast.db:
  chain_wide(index_name, ts, expiry, strike, opt_type, ltp, oi, volume, iv NULL, spot,
  PRIMARY KEY(index_name, ts, strike, opt_type)).
- In the HourlyForecaster thread (or a small sibling thread started/stopped with it), at every
  5-minute boundary +20 s between 09:15 and 15:30 IST, fetch ATM +/-15 strikes (31 strikes x CE/PE)
  for the nearest weekly expiry of each index in FORECAST_INDICES, in batches within the getMarketData
  limit (50 tokens per call). Use the instrument master the engine already loads; reuse the engine's
  session (no new login).
- On any error: log one line, skip that cycle, never raise into the engine.
- Retention: keep 60 sessions (prune older rows once a day at 15:35).

## Part 2 - 15:30 end-of-day full-chain save
- At 15:30-15:32 IST take one snapshot of ALL strikes of the nearest TWO expiries for each index
  (batched as above) into chain_eod(index_name, day, expiry, strike, opt_type, ltp, oi, oi_change,
  volume, spot, PRIMARY KEY(index_name, day, expiry, strike, opt_type)), oi_change vs the previous
  chain_eod day when present. Keep forever (small).

## Part 3 - Nightly NSE F&O bhavcopy (official archive)
- Extend tools/forecast_nightly.py with a `bhavcopy` step (also runnable alone:
  python3 tools/forecast_nightly.py --only bhavcopy [--date YYYY-MM-DD]).
- Source: NSE's official daily F&O bhavcopy archive on nsearchives.nseindia.com (the UDiFF
  "BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip" file; fall back to the legacy
  fo<DDMMMYYYY>bhav.csv.zip path if the first returns 404). Find the current exact URL from NSE's
  "All Reports - Derivatives" page documentation rather than guessing more paths; set a normal
  browser User-Agent, timeout 20 s, at most 2 attempts, one file per day, no other NSE URLs.
- Parse only NIFTY / BANKNIFTY / FINNIFTY option rows (and SENSEX from BSE only if a BSE equivalent
  is trivially available; otherwise skip SENSEX) into
  bhav_options(index_name, day, expiry, strike, opt_type, close, settle, oi, oi_change, contracts,
  PRIMARY KEY(index_name, day, expiry, strike, opt_type)).
- If NSE blocks the server (403/timeout/HTML instead of zip): record status 'blocked' in a
  nightly_status row, fall back to chain_eod from Part 2, and do not fail the nightly job.
- Backfill: on first run try the last 30 trading days (one request per day, 2 s apart).

## Part 4 - Use the data in the forecast
In kadima/hourly_forecast.py (keep function signatures; the model must still work if all new
tables are empty - fall back to today's oi_snapshots behaviour):
1. chain_at(index_name, ts): prefer chain_wide (wider PCR and walls) within 300 s of ts, else the
   existing oi_snapshots path.
2. oi_features: add, when available,
   - prev_day_pcr and prev_day_ce_wall / prev_day_pe_wall from bhav_options (else chain_eod) for the
     previous trading day,
   - oi_build: change in total CE OI vs PE OI over the last 30 min across ATM +/-15 (call writing vs
     put writing),
   - max_pain for the nearest expiry from the latest chain_wide snapshot.
   No look-ahead: everything strictly before t0 (previous day's EOD is fine for today's slots).
3. In kadima/hourly_forecast_model.py extend oi_tilt to use the new fields, still scaled by the
   existing learned beta_oi (no new beta, so the champion/challenger retune decides its weight).
   Keep the tilt bounded as today.
4. Panel: in the slot card's inputs line show "PCR x.xx (prev day y.yy) - CE wall / PE wall - max pain"
   when present. Display only.

## Part 5 - Tests and report
- tools/test_option_chain_data.py (offline, no broker, no network): synthetic chain_wide /
  chain_eod / bhav_options rows -> chain_at / oi_features values; empty tables -> old behaviour;
  bhavcopy parser on a small fixture CSV (both UDiFF and legacy column layouts); blocked-download
  path records 'blocked' and does not raise; no-look-ahead check.
- Run all tests, then report:
  - files changed, backup path, diff -u,
  - API calls added per day (count) and confirmation they stay inside the rate limiter,
  - result of `python3 tools/forecast_nightly.py --only bhavcopy` for the last 5 trading days
    (ok / blocked / missing per day, rows saved),
  - row counts in the new tables,
  - rollback commands for every changed file,
  - restart: sudo systemctl restart kadima (allowed now, Saturday), then
    systemctl is-active kadima and the last 20 lines of the kadima journal with credentials masked.
