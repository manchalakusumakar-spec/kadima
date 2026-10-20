# Release plan: 5-minute milestone chart and 5-point reforecast (2026-10-20)

Forecast display only, stacked on PR #47 (scoring integrity). No change to Gamma Blast logic or
settings, CAS, Bull/Bear Shield, Angel One execution, trend rules, risk or trading settings.

## What changes

| Area | Change |
|---|---|
| `kadima/hourly_forecast.py` | New table `hourly_forecast_revisions`. Each time a **closed** candle sits 5 points or more from the path in force (the original, then the latest revision), the worker publishes a revised path: the original shifted through that close, with a 16-hex SHA-256 receipt. Provisional prices never trigger one. No cap: every 5-point miss adds a line. The last milestone never triggers one. Revisions are never scored, never used for calibration or self-audit, and the original rows are untouched. `view()` also returns each milestone's 5-minute bar (open/high/low/close/closed), read-only. |
| `web/hourly_forecast.html` | New chart: original dashed line and band, candles (provisional bars dashed), every reforecast (latest bold with its ±5 trigger lines, earlier ones faded), R-labels with receipts. The current-hour card spans the full width. |
| `web/celestial_forecast_embed.js` | Frame height now follows the forecast content, so the blank space below the panel goes away (it used `scrollHeight`, which can grow but never shrink). Gamma Blast section moved directly above Trade Audit on screen (moves the existing nodes; handlers and polling unchanged). |
| `tools/install_celestial_embed.py` | Backs up the current `forecast.html` and `celestial_forecast_embed.js` before copying; script tag bumped to `v=5`. |

Memory cost: no new process, thread, polling loop or dependency. One row per reforecast (a few a day).

## Gates before production

1. PR #47 installed and its first session checked (its own release plan).
2. CI green on the exact release commit.
3. Market closed (after 15:45 IST) and no open positions.
4. Explicit approval from the operator.

## Stages

**Check (read-only)**
```bash
~/kadima-py/venv/bin/python ~/kadima-release/tools/install_forecast_milestones_20261020.py ~/kadima-py --check
```
Expect `state ready` (over the PR #47 release or the live base).

**Install (approval)**
```bash
~/kadima-py/venv/bin/python ~/kadima-release/tools/install_forecast_milestones_20261020.py ~/kadima-py
cd ~/kadima-release && sudo python3 tools/install_celestial_embed.py
sudo systemctl restart kadima && sleep 20 && systemctl is-active kadima
journalctl -u kadima --since "-2 min" --no-pager | tail -40
sqlite3 -readonly /var/lib/kadima/forecast.db ".schema hourly_forecast_revisions"
```
Then hard-refresh Celestial: Gamma Blast sits above Trade Audit, no blank space below the forecast.

**Watch (read-only)**
```bash
sqlite3 -readonly /var/lib/kadima/forecast.db "SELECT slot,rev,trigger_ts,deviation_pts,receipt
  FROM hourly_forecast_revisions WHERE day=date('now','localtime') ORDER BY slot,rev;"
```

## Rollback
```bash
~/kadima-py/venv/bin/python ~/kadima-release/tools/install_forecast_milestones_20261020.py ~/kadima-py --rollback
# panel files: restore the *.bak-<stamp> copies in /var/www/html, or
sudo cp -p /var/www/html/forecast.html.bak-<stamp> /var/www/html/forecast.html
sudo cp -p /var/www/html/celestial_forecast_embed.js.bak-<stamp> /var/www/html/celestial_forecast_embed.js
sudo systemctl restart kadima
```
The revisions table can stay; older code ignores it.
