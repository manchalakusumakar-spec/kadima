# NIFTY 50 breadth (shadow)

Records NIFTY 50 weighted breadth once a minute beside the hourly forecast. **It never changes a forecast.**
It uses the prices `kadima-universe-scanner` already writes to `~/kadima-py/state/universe_snapshot.json`:
no broker calls, no new process, and well under 1 MB of memory.

## Weights file (required; nothing records without it)
`$KADIMA_DATA_DIR/nifty50_weights.json`, taken from the latest NSE NIFTY 50 factsheet. Symbols must match
the scanner's names (the instrument master `name`, e.g. `HDFCBANK`, `M&M`, `BAJAJ-AUTO`):
```json
{"as_of": "2026-09-30", "weights": {"HDFCBANK": 0.0, "ICICIBANK": 0.0, "...": 0.0}}
```
Fill in the real percentages for all 50 names. The file is refused when it is older than 45 days or has fewer than 45 names.

## Install (after hours, after #47 and #48)
```
~/kadima-py/venv/bin/python tools/install_nifty_breadth_shadow_20261020.py ~/kadima-py --check
~/kadima-py/venv/bin/python tools/install_nifty_breadth_shadow_20261020.py ~/kadima-py
sudo systemctl restart kadima
```
Rollback: run the same installer with `--rollback`, then restart.
Turn it off without a restart: `echo '{"mode":"off"}' > $KADIMA_DATA_DIR/nifty_breadth.json`.

## Decide after 1-2 weeks
```
~/kadima-py/venv/bin/python -c "from kadima import hourly_forecast as H, nifty_breadth as B; print(B.shadow_report(H.conn(), days=10))"
```
Only consider giving breadth a weight in the forecast once there are at least 200 samples and the hit rate
(the breadth sign vs the forecast surprise over the next 15 minutes) is clearly above 0.55. That would be a separate, reviewed change.
