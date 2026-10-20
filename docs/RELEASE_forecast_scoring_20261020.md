# Release plan: forecast scoring integrity (2026-10-20)

Forecast-only. One file changes on the server: `kadima/hourly_forecast.py`. No change to Gamma Blast,
CAS, Bull/Bear Shield, Angel One execution, trend rules, risk or trading settings.

## What changes

| Before (live hf-2.0) | After |
|---|---|
| 60 s after a target, an **unclosed** candle (`closed_only=False`) is stored as the actual and scored. | Stored as `provisional`, shown, **never scored**. Upgraded to the closed value if the bar closes later. |
| After 15 min (`FINALIZE_GIVE_UP_SEC`) the slot is finalized and scored with whatever actuals exist. | Slot is marked `incomplete`: no score, no recalibration, no self-audit tuning. |
| Score needs only the endpoint actual. | Score needs **every** target to have a closed candle starting exactly at the target bar (`closed_5m` or `closed_1m`). |

The original forecast rows (`forecast/upper/lower`) are never touched. The trend rules (shadow) read
only live slots' future bands, which are unchanged.

Schema: one nullable column `hourly_forecast_points.actual_source`, added on first connect with
`ALTER TABLE ... ADD COLUMN`. Existing rows are kept. Rollback does not need to remove it (old code ignores it).

**Memory cost:** none measurable. No new process, thread, polling loop, import or dependency. One short
text value per 5-minute point (about 75 rows a day). Safe with swap full, but swap must still be fixed separately.

## Known consequence to check first

If the 15:25 bar never gets `closed=1` (no tick after 15:30), the last slot of each day will be
`incomplete` instead of scored. Check this read-only before installing:

```bash
sqlite3 -readonly /var/lib/kadima/kadima.db \
  "SELECT start_ts, closed FROM candles WHERE token='99926000' AND interval IN (1,5)
   AND (start_ts LIKE '%T15:25:%' OR start_ts LIKE '%T15:29:%') ORDER BY start_ts DESC LIMIT 10;"
```
(Use the real `KADIMA_DATA_DIR` if it is not `/var/lib/kadima`.) If `closed` is 0 there, the last slot
will show incomplete. That is the honest result; closing the final bar at session end is a separate
change to `candles.py`, which is shared with trading and out of this release's scope.

## Gates before production (all must be true)

1. CI green on the exact release commit.
2. Read-only on EC2: `sha256sum ~/kadima-py/kadima/hourly_forecast.py` equals
   `c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870` (the installer also refuses otherwise).
3. Market closed (after 15:45 IST, so no slot is live) and no open positions.
4. Explicit approval from the operator for steps 3 to 5 below.

## Stages

**Stage 0. Read-only (no approval needed).**
```bash
cd ~ && git -C ~/kadima-release fetch origin claude/forecast-scoring-integrity-20261020 2>/dev/null \
  || git clone --branch claude/forecast-scoring-integrity-20261020 https://github.com/manchalakusumakar-spec/kadima ~/kadima-release
git -C ~/kadima-release checkout --detach <RELEASE_SHA> && git -C ~/kadima-release rev-parse HEAD
~/kadima-py/venv/bin/python ~/kadima-release/tools/install_forecast_scoring_20261020.py ~/kadima-py --check
free -m; systemctl is-active kadima kadima-dashboard
```
Expect `state ready`. (The release clone sits outside `~/kadima-py` and is never imported by the service.)

**Stage 1. Back up (approval).**
```bash
TS=$(date +%Y%m%d_%H%M%S); mkdir -p ~/backups/forecast-scoring-$TS
cp -p ~/kadima-py/kadima/hourly_forecast.py ~/backups/forecast-scoring-$TS/
sqlite3 /var/lib/kadima/forecast.db ".backup '$HOME/backups/forecast-scoring-$TS/forecast.db'"
sha256sum ~/backups/forecast-scoring-$TS/*
```

**Stage 2. Install (approval).**
```bash
~/kadima-py/venv/bin/python ~/kadima-release/tools/install_forecast_scoring_20261020.py ~/kadima-py
~/kadima-py/venv/bin/python -m py_compile ~/kadima-py/kadima/hourly_forecast.py
```
Expect `installed kadima/hourly_forecast.py <release hash>`.

**Stage 3. Restart and health (approval, after hours only).**
```bash
sudo systemctl restart kadima && sleep 20
systemctl is-active kadima && journalctl -u kadima --since "-2 min" --no-pager | tail -40
sha256sum ~/kadima-py/kadima/hourly_forecast.py
sqlite3 -readonly /var/lib/kadima/forecast.db "PRAGMA table_info(hourly_forecast_points);" | grep actual_source
free -m
```
Pass: service active, no traceback, release hash on disk, column present, memory no worse than before.

**Stage 4. Watch the next session (read-only).** After each slot:
```bash
sqlite3 -readonly /var/lib/kadima/forecast.db "SELECT slot,status,mae_pts FROM hourly_forecasts
  WHERE day=date('now','localtime') AND index_name='NIFTY' ORDER BY slot;"
sqlite3 -readonly /var/lib/kadima/forecast.db "SELECT slot,k,ts,actual,actual_source FROM hourly_forecast_points
  WHERE day=date('now','localtime') AND index_name='NIFTY' ORDER BY slot,k;"
```
Pass: every `final` slot has only `closed_5m`/`closed_1m` sources; anything else is `incomplete`.

## Rollback (tested in CI: install, re-run, rollback, refuse unknown file)

```bash
~/kadima-py/venv/bin/python ~/kadima-release/tools/install_forecast_scoring_20261020.py ~/kadima-py --rollback
sha256sum ~/kadima-py/kadima/hourly_forecast.py   # must be c702310c...
sudo systemctl restart kadima && systemctl is-active kadima
```
If the installer itself is unavailable: `cp -p ~/backups/forecast-scoring-<TS>/hourly_forecast.py ~/kadima-py/kadima/`
then restart. The extra column can stay; old code ignores it. Slots marked `incomplete` stay
unscored after rollback, which is the safe direction.

Trigger rollback on: service not active, any traceback from `hourly_forecast`, forecasts not generated
at the next slot start, or memory clearly worse than before.

## Not in this release (PR #43)

PR #43 (`codex/nifty-learning-event-reforecast-v1`, head `4b7762b`) adds research modules under
`forecast_precision/` that nothing in the live service imports. Installing it would change nothing
visible. Its base also carries hf-2.0.1 (a different `hourly_forecast.py`, heavyweight tilt, band-break
updates) that is **not** on the server. The 5-minute milestone panel and the ≥5-point reforecast with
a publication receipt need a separate integration into the live worker and dashboard, built on this
release.
