# Hourly forecast vs actual (KADIMA Celestial)

Display-only hourly path forecasts for NIFTY, with human-clicked CE/PE buy
buttons. Built 2026-10-09 on branch `hourly-forecast`.

## What it does

- **7 slots a day:** 09:15–10:15, 10:15–11:15, 11:15–12:15, 12:15–13:15,
  13:15–14:15, 14:15–15:15, 15:15–15:30.
- **At each slot start** the engine forecasts the slot as 5‑minute points
  (12 per hour, 3 for the last slot) with a band. The forecast is then frozen.
- **During the slot** the actual price is filled in every 5 minutes from the
  engine's own closed candles, plus a live dot at the current spot.
- **When the slot ends** it is scored: direction hit, average error (points),
  share of actuals inside the band, end error.
- **Every slot is stored** in `$KADIMA_DATA_DIR/forecast.db` and can be
  reviewed by date on the panel.
- **Forecast % shown:** projected move (% and points), direction probability,
  band width, and today's / 30‑day scorecard.

## How the forecast is made (`kadima/hourly_forecast_model.py`)

All inputs are from before the slot start; there is no look‑ahead (tested).

1. **Pooled regression** of the next slot's move on the market's current
   shape (returns over 1/3/6/12 bars, day move, gap, place in the day's range),
   fitted on every past session near the same time of day.
2. **Analog paths:** the 25 most similar past moments, weighted by
   similarity, recency and time of day, supply the path shape.
3. **Technical tilt:** EMA 9/21 and RSI 14 on 5‑minute closes, price vs the
   session average and, live only, the Bull Shield direction and score.
4. **Volatility band:** realized 5‑minute volatility blended with ATM implied
   volatility (solved from the engine's own option-chain poll), shaped by the
   measured time‑of‑day profile.

**Self‑tuning.** After every scored on‑time slot the coefficients, band width
and probability sharpness are refitted from the model's own record, with a
prior worth 30 slots — so fewer than ~30 resolved slots barely move it (your
"30 resolved occurrences" rule). Late slots (engine restarted mid‑slot) are
shown but excluded from tuning and from the scorecard.

## Trade buttons

- CE dropdown: ATM, ATM+1, ATM+2 strikes. PE dropdown: ATM, ATM−1, ATM−2.
  Live premiums shown. Lots 1 to `MAX_LOTS_PER_SIGNAL`.
- Every click asks for confirmation, then calls
  `POST /control/api/forecast/buy`, which runs
  `auto_executor.forecast_manual_buy()`: session gate, reconciliation, kill
  switch, market hours, signal cutoff, daily loss limit, carry‑forward and
  external‑contract checks, portfolio cash/loss capacity, fresh LIMIT at
  LTP + 2.5%, the durable execution ledger, then the normal software stop and
  trail. Stop = structure‑based premium stop when a confirmed swing exists,
  otherwise 30% below.
- Switch off: `FORECAST_MANUAL_TRADE_ENABLED = False` in `config.py`.
- `FORECAST_AUTOTRADE_ENABLED` is reserved for a future auto mode; nothing
  reads it.

## Adding SENSEX / BANKNIFTY later

Set `KADIMA_FORECAST_INDICES=NIFTY,SENSEX,BANKNIFTY` in the systemd
environment and restart. The panel takes `?index=SENSEX`.

## Deploy to EC2

The live checkout (`~/kadima-py`, branch `kadima-live-backup-20260915`) is
newer than GitHub `main`. Do **not** check this branch out over it. Apply it
as a patch, after market hours.

```bash
cd ~/kadima-py   # with the same Python venv kadima.service uses
git fetch origin hourly-forecast
mkdir -p ~/backup-hf && cp kadima/{auto_executor,config,control_api,engine}.py ~/backup-hf/

# 1) new files, copied as-is
git checkout origin/hourly-forecast -- kadima/hourly_forecast.py kadima/hourly_forecast_model.py \
    tools/forecast_backtest.py tools/test_hourly_forecast.py tools/test_hourly_forecast_live.py \
    tools/test_forecast_manual_buy.py web/hourly_forecast.html docs/hourly_forecast.md

# 2) edits to existing files, as a patch against the live versions
git diff c53365d origin/hourly-forecast -- kadima/auto_executor.py kadima/config.py \
    kadima/control_api.py kadima/engine.py > ~/hf.patch
git apply --check ~/hf.patch && git apply ~/hf.patch
#   If --check fails, stop: the live file differs from main around that hunk.
#   Send the error and the hunk will be re-based on the live file.

# 3) verify
python -m py_compile kadima/*.py
python tools/test_hourly_forecast.py
python tools/test_forecast_manual_buy.py

# 4) tune on real history (read-only on kadima.db), then save
python tools/forecast_backtest.py --index NIFTY
python tools/forecast_backtest.py --index NIFTY --save

# 5) panel page, next to the existing dashboard
sudo cp web/hourly_forecast.html /var/www/html/forecast.html

# 6) restart and check
sudo systemctl restart kadima
sleep 5 && systemctl is-active kadima
curl -s -H "Authorization: Bearer $KADIMA_API_TOKEN" \
  "http://127.0.0.1:8081/api/forecast/hourly?index=NIFTY" | head -c 400
```

Open `https://<your sslip.io host>/forecast.html` (or link it from
Celestial). It asks once for the control token and keeps it in the browser.

Rollback: `cp ~/backup-hf/*.py kadima/ && sudo systemctl restart kadima`.

## Files

| File | Purpose |
|---|---|
| `kadima/hourly_forecast_model.py` | Pure model maths, scoring, calibration |
| `kadima/hourly_forecast.py` | Engine thread, `forecast.db`, panel JSON, strike offers |
| `kadima/auto_executor.py` | `forecast_manual_buy()` with all entry gates |
| `kadima/control_api.py` | `/api/forecast/hourly`, `/days`, `/buy` |
| `kadima/engine.py` | Starts/stops the forecaster with the engine |
| `kadima/config.py` | Trade-button switches |
| `web/hourly_forecast.html` | Celestial panel: 7 charts, scorecard, trade buttons |
| `tools/forecast_backtest.py` | Walk-forward backtest and tuning on real candles |
| `tools/test_*` | Offline tests (no broker) |
