# Forecast-conditioned profit trailing (prepared, disabled)

User rule: for long CE and PE positions, exit on the profit trail only if
(a) the ratcheted stop is breached and (b) projected additional profit over
the next 30 minutes is strictly less than the stop-risk amount.

## Exact amounts

The existing activation and giveback settings in exit_manager.py are retained.
No new stop percentage is introduced.

stop = peak - (peak - entry) * existing_giveback_fraction
risk_inr = (peak - stop) * remaining_qty
projected_profit_inr = max(0, forecast_expected_premium - current_premium) * remaining_qty

The risk reference is peak-to-stop giveback. Current-minus-stop becomes
zero or negative after a breach and cannot implement the requested comparison.
Equality holds. A declining premium does not lower the stop.

## Integration

auto_executor.py calls forecast_trail.evaluate only for activated trails and
only when config.FORECAST_TRAIL_ENABLED is explicitly True. It defaults to False
via getattr; installing these files alone does not enable the rule.
The enabled path persists the peak with existing position storage.
set_profit_projection accepts an in-process Projection for an open position.
It rejects mismatched contracts. Forecasts are removed on close and are not
restored after restart; a forecast producer must submit new data.

A valid Projection identifies signal_id, exchange and token, names its model,
and contains timezone-aware generated_at, market_data_at, target_at and a
nonnegative expected_premium. Target is exactly 30 minutes after generation.
Generation and input data must both be no older than 60 seconds and not in
the future.

Missing, stale or invalid forecasts do not count as zero predicted profit.
In enabled mode they defer this trailing exit and generate an audit reason.
Thus an unavailable forecast can leave a breached trail unexecuted.
Initial stop and swing logic, daily-loss rules, EOD rules, existing no-auto-exit
window and manual exits retain their current behavior. This is not a maximum
loss guarantee.

## Not supplied / not activated

The repository has no confirmed 30-minute expected option premium producer.
The current entry estimate is an IV/delta move over time to expiry, not a
30-minute forecast, and is deliberately not reused as one.
No trend model, target premium or probability is invented by this change.
A producer and its validation must be selected before enabling the feature.
Do not set FORECAST_TRAIL_ENABLED until that integration is complete.
No production restart or order has been executed for this work.

The existing broker close path is retained; this change does not establish
whether an exit order was filled. Fill reconciliation is a separate concern.

## Verification

From the repository root:
PYTHONPATH=. python3 -m unittest discover -s tests -p test_forecast_trail.py -v

Tests exercise both conditions, equality, gaps, quantity scaling, ratchet,
stale/mismatched/missing forecasts, default disabled behavior and daily/EOD
priority. Executor checks use stubs; no broker calls or trade orders occur.
