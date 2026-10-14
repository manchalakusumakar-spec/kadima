# PR42 follow-up — event-driven reforecast and self-learning

**Release status: SOURCE CANDIDATE ONLY, DISABLED, NOT INSTALLED, NOT LIVE-VALIDATED.**
The work adds pure forecast-only Python modules, a lightweight no-NumPy
event validator, a bottom-of-chart
learning-panel renderer, an illustrative preview and automated tests. It does
**not** install a KADIMA engine hook, attach to the current Celestial forecast,
subscribe to Angel One, poll options, place broker orders, modify Gamma/CAS,
retrain live weights or change any production configuration.

## Behavior

- **Anytime precision correction (new):** Every new actual at an exact issued
  five-minute target is eligible for an immediate >=5-point error check,
  including 13:40 between 13:30 and 13:45 quarter-hour checks.
  For an intrabar observation such as 13:37, a source-verified fresh NIFTY
  tick may be checked against the *linearly interpolated* immutable path
  (adjacent issued points only). Intrabar >=5-point deviation must persist
  across at least three independently verified ticks spanning >=2 seconds,
  all on the same side of the threshold. This interpolation is a
  **trigger diagnostic, NOT a scored original five-minute prediction**.
  An already-requested active forecast cannot spam unlimited intrabar requests.
  Both new checks operate only on the bounded forecast observation worker and
  still require a future genuine target and independent publication receipts.
  The 15-minute check below stays as a fallback, not as a waiting gate.
- **Scheduled precision check:** At a genuine 15-minute checkpoint compare the
  active *issued* forecast's price for that exact timestamp with a timestamped
  completed actual. An absolute error **>=5 NIFTY points** requests a new
  forward prediction, regardless of whether the previous forecast direction
  was correct. An error of 4.999 points does not request a revision.
- **Market shock anytime:** A confirmed price/volume, price/breadth, price/OI,
  price/gamma-pressure or exceptional volume/options-pressure event can
  request a reforecast **between 15-minute checkpoints**. Extreme prices can
  also trigger after three producer-verified independent ticks over at least
  two seconds. There is a bounded 30-second **shock-only debounce**, not a
  15-minute wait. Thresholds in TriggerPolicy are uncalibrated research
  examples and **disabled by default**. Volatility/time-of-day baselines
  supplied by an audited producer must cover distinct prior sessions. Gamma
  pressure is **forecast context, never a Gamma order signal**.
- **Causal safety:** No request unless a genuinely issued prior forecast and
  a future five-minute target exist. Future/late/missing actuals or stale,
  unlabeled anomaly evidence are excluded. A reforecast request is not called
  a published forecast. A revision requires a separate recorded publication
  receipt issued after the request but before the next future target.
- **Learning:** After-market deterministic per-horizon residual-bias challengers
  use **ORIGINAL** completed, source-verified forecasts only, earlier sessions
  to train, and later untouched sessions to test. Offsets must improve mean
  absolute error without worsening below-five hit counts or 90th-percentile
  error. If evidence is insufficient, no corrected candidate is produced.
  A qualified offset can be applied only to a **future shadow alternative**;
  it never overwrites originals or promotes production weights.
- **Five-minute chart milestones (new):** `web/forecast_milestones.js` + `web/forecast_milestones.css`
  render every issued forecast target as a visible, numeric, timestamped
  marker and table row. At each target display original forecast, exact-time
  verified actual when available, signed deviation, and strict >=5-point
  alert status. Future observations remain pending, never zero. The +/-5 target
  band, source-verified issue times, and selected revision identity are
  maintained. Each timestamp can be inspected via SVG title and the scrollable
  table remains accessible on mobile. Intrabar interpolated alerts are
  deliberately not scored as issued five-minute milestones. Read-only renderer
  and preview only: production Celestial mounting and API binding are still
  outstanding and must be separately reviewed.
- **Bottom panel:** `web/learning_panel.js` is a read-only renderer that accepts
  the bounded output of `panel_feed.panel_snapshot`. It displays operational
  mode, <5 target, independently scored original accuracy, learning state,
  and timestamped triggers/receipts. It does not fabricate a publication
  when an event was merely requested. The component attaches to a host element
  passed by the eventual forecast UI integration. It never polls credentials,
  changes orders, or shows unverified numbers as live results. The preview is
  **synthetic UI data only**.

## Required upstream data and outstanding integration

1. Verify the exact AWS-loaded source identity and composed overlays. The
   earlier PR42 host receipt found `constituent_radar.py` absent, no verified
   shared cache writer lock, and no independent OI/Greek time stamps. None of
   these is fixed by the present patch. Do not pretend that the 213-symbol
   stock-option scanner equals the official weight-adjusted all-50 radar.
2. A time-of-day volatility/volume baseline with valid as-of, publication and
   source identities is required before enabling event detection. Do not
   convert price/volume raw ticks to z-scores by guessing the baseline.
3. Reuse the existing forecast worker and its bounded observation queue;
   never evaluate full indicators or fit models on tick/order callbacks. The
   event gate is pure and cheap; the actual forecaster must run asynchronously
   against a coherent, timestamped packet. Coalesce repeated triggers.
4. Capture the original forecast *as actually published*, including its
   publication time and future target IDs. Reforecasts receive new immutable
   identities. UI should remain original+actual with ±5 target band by
   default; a separate version chooser can show a selected revised path.
5. Implement an authenticated **read-only** data endpoint or existing
   forecast-response extension and mount the renderer *below* the chart and
   accuracy assessment, not inside a trading-control surface. Do not reuse
   bearer control tokens in a new JavaScript polling loop without review.
6. Obtain genuinely source-verified live samples, then run the original,
   last-price, trend and learning candidate on the exact same decision packet
   with future-target actuals. Validate separately by horizon and regime on
   later sessions before promotion. The target is **strict <5 per point**;
   no guarantee of achieving it. Net Error aggregate formula remains unset.
7. Review memory and CPU on AWS; the earlier host had very limited available
   memory and swap. Train away from the quote-processing process.

## Example research-only API (no runtime hook exists)

```python
from precision_lab.reforecast_events import EventGate, TriggerPolicy
from precision_lab.self_learning import learn_next_forecast_bias
from precision_lab.panel_feed import panel_snapshot

# OFF by default. Enable only in isolated, tested, synthetic shadow fixtures.
gate = EventGate(TriggerPolicy(enabled=False))

# The external forecaster must consume a REQUEST_REFORECAST result through
# its existing bounded observation queue. Do not call orders, restart engines,
# or report a revision until a genuine publication receipt has been stored.
```

## Tests

```sh
cd forecast_precision
python -m pip install -r requirements.txt
OPENBLAS_NUM_THREADS=1 python -m unittest discover -s tests -v
node --test tests/test_learning_panel.cjs tests/test_forecast_milestones.cjs
```

The Python tests cover strict >=5 triggers at any five-minute target,
confirmed intrabar deviation, precise issue/actual time matching, interpolation
never counted as measured forecast accuracy, intrabar shocks, stale/future
inputs, dedupe, scheduled timing, immutable publication receipts, no
retroactive learning, session holdout and strict original-only panel metrics.
Node tests cover the DOM rendering, unissued vs issued statuses, bounding,
accessibility and HTML-injection resistance.

**Production decision:** do not install or restart trading services or change
forecast display based solely on this PR. Prepare a separate reviewed, source-
matched installer, rollback, live-data binding and approval before staging.
