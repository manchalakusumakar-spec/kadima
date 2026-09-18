# Live inputs and score-based probability

This patch adds read-only display features to the authenticated dashboard:

- Gamma Top 20 refreshes every 5 seconds, using actual qualifying directional candidates. Missing slots stay empty. Legacy source scores older than 30 seconds leave the current ranking.
- Score-based probability displays the **existing Final score as a percentage** in Celestial, and the **existing Gamma score as a percentage** in the legacy ranking. The source score remains visible. It is a heuristic confidence estimate, not a calibrated win rate; no new weights or trading thresholds are introduced.
- Existing selected strike, LTP/filled entry, managed stop, T1/T2 display scenarios and measured-pace holding estimates remain in each row. Missing contract selection or engine plans stay unavailable; the UI does not manufacture trade instructions. Index strikes require a `gamma.best_contract` export, which older engines do not provide.
- Forecasts display actual target times in IST, low/expected/high scenarios, and current VWAP/EMA20/EMA50 reference levels. Current reference levels are labelled separately from forecast input timestamps. A missing forecast is never replaced with a fabricated curve. HTTP 429 remains an upstream quota/rate-limit issue requiring provider recovery.
- Market/position panels refresh every 4 seconds, scanner and macro snapshots every 5 seconds. These are dashboard receipt cadences, not promises about upstream quote or publication latency. FII/DII remain daily reported data. No new source for previously missing global indices, DXY or GIFT is implied.
- An independent bounded public NSE RSS collector checks every 60 seconds, backs off on failures, and exposes publication time, retrieval time and source health. Existing Marketaux headlines retain their configured 30-minute budget. News in this new feed is display context and does not change Celestial entry eligibility or news scoring.

Public sources are listed by NSE at https://www.nseindia.com/static/rss-feed . Polling is not an exchange real-time feed SLA.

## Install on the dashboard host

Use the `live_inputs_update` directory from this change. The upgrader checks exact anchors and fails before writing if the installed page is incompatible. It preserves other installed dashboard additions and backs up changed files. It does not replace engine code.

```sh
sudo mkdir -p /opt/kadima-live-inputs
sudo cp live_inputs_update/news_worker.py /opt/kadima-live-inputs/news_worker.py
sudo cp live_inputs_update/kadima-live-news.service /etc/systemd/system/
sudo python3 live_inputs_update/upgrade.py --html /var/www/html/dashboard.html --gateway /opt/kadima-dashboard/gateway.py
sudo python3 live_inputs_update/upgrade.py --html /var/www/html/dashboard.html --gateway /opt/kadima-dashboard/gateway.py --apply
sudo systemctl daemon-reload
sudo systemctl enable --now kadima-live-news.service
sudo systemctl restart kadima-dashboard.service
```

This restarts the dashboard gateway only; users may need to sign in again. It does not restart or reconfigure trading. The news service has a 96 MB memory cap and 10% CPU quota. The authenticated `/dashboard-api/live_news` route reads only its fixed JSON path. No credential is added to browser code or the news worker.

Verify after installation: sign in; confirm Public news shows each source check and publication times; verify the Score-based probability column and score mapping; verify stale rows lose current plans/probability; inspect an available forecast for IST checkpoints and reference levels. Confirm news is readable by the gateway service user under the host's systemd StateDirectory policy. Check `systemctl status kadima-live-news.service kadima-dashboard.service` if unavailable. Do not report the feed as live until these checks pass.

## Rollback

Restore the timestamped `dashboard.html.before-live-inputs-*` and `gateway.py.before-live-inputs-*` backups, then restart `kadima-dashboard.service`. Stop and disable `kadima-live-news.service`. Never use this rollback to overwrite unrelated later changes.

## Validation

```sh
python3 -m unittest discover -s live_inputs_update -p 'test_*.py' -v
node live_inputs_update/test_live_view.cjs
node tests/test_gamma_plan.cjs
```

Tests cover existing-score mapping, invalid/stale/future inputs, forecast target times, missing forecasts, escaping, feed parsing and budgets, failure retention/backoff, deduplication, expiry, gateway authentication placement, and idempotent transformations. Live provider delivery and AWS deployment must be verified on the host; local fixture checks cannot establish those.
