# Checklist forecaster deployment

See [the implementation and operation guide](../docs/checklist_forecaster.md).

Run `install.py` with `sudo -n` in the guarded maintenance window, then `configure.py` to enter an OpenAI API key privately, and `update_dashboard.py` for the observation panel. `report.py` reads results without credentials. All forecasts are observation-only. This does not change order flags or allow forecast-based exit deferral.
