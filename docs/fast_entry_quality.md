# Fast-entry quality: first refinement patch

Base reviewed: `main` at `c53365deddcca84ffee4484ef8246c14dc6cc450`.
This is a source patch, not proof of the version deployed on AWS. The separate
two-lot index scalper in draft PR #17 is not included in this base.

## Problem and scope

The existing fast score uses option-premium momentum and incremental option
volume. Its minimum qualifying inputs already score 68.75, above the default
65 threshold. Other gates exist, but weak breakout candles and a five-minute
whipsaw ending slightly in the chosen direction can still pass. A structural
stop at the breakout/hold extreme also has no explicit local noise buffer.

This patch adds independent entry filters to the shared `fast_momentum.setup`
used by both index and stock fast lanes. Those lanes call the same setup during
execution revalidation. No score is reinterpreted as a win probability.

## Rules and initial thresholds

All five values are in `kadima/config.py`. They are **experimental hypotheses**,
not calibrated thresholds or demonstrated improvements in profit or stop-outs.

| Check | Definition | Initial value |
| --- | --- | --- |
| Five-minute direction efficiency | Signed net change / sum of absolute close-to-close changes over the last three completed five-minute bars | At least 0.35 |
| Breakout body | Candle body in trade direction / full candle range | At least 0.50 |
| Breakout close location | Close's distance from adverse candle extreme / full range | At least 0.75 |
| Entry extension | Spot's distance past breakout level / baseline mean true range | At most 1.00 |
| New structural-stop buffer | Distance beyond the original breakout/hold extreme, in baseline mean true ranges | 0.25 |

The baseline is the five one-minute candles immediately before the breakout.
Mean true range uses each candle's high/low and the prior baseline close; the
first baseline candle uses high minus low because no prior close is supplied.
It excludes breakout and hold candles to avoid letting a large impulse inflate
its own entry allowance. This short-window estimator is **not Wilder ATR(14)**.
The original breakout, hold, maximum extension and data-quality rules still run.

CE buffering subtracts from the underlying stop; PE buffering adds to it. Both
are long-premium trades. The existing option model maps that underlying level
to a premium stop, then existing sizing and final broker-funds checks reduce
quantity or reject an unaffordable trade. The buffer does not enlarge any risk
budget. Existing reward/risk checks must still pass after stop mapping.

This is new-entry stop placement only. Open-position stops, structural updates,
profit trailing, time exits, reconciliation, duplicate prevention, daily-loss
controls, session gates and live-enable settings are not modified. Existing
protection can subsequently tighten the initial stop; the buffer is not a
promise that it will persist throughout a trade.

Accepted setup rows include `entry_quality`: version, measured values, raw
underlying stop, buffer and threshold values. These fields travel with the
existing fast candidate/signal payload. Rejected rows surface stable reasons:
`fast_choppy_five_minute_context`, `fast_weak_breakout_candle`,
`fast_entry_overextended`, or a fail-closed invalid-input/policy reason.
No new REST polls, subscriptions, retained market histories or worker threads
are introduced.

## Validation and deployment boundary

Run offline from the repository root:

```sh
python -m unittest discover -s tools -p test_fast_entry_quality.py -v
python tools/test_offline.py
python tools/test_audit_fixes.py
python -m compileall -q kadima tools/test_fast_entry_quality.py
```

The new suite blocks socket connections and covers CE/PE symmetry, weak and
choppy setups, extension boundaries, scale invariance, baseline gaps, stale or
unclosed data, invalid policy values, both lane integrations, premium-stop
mapping, risk-sized rejection, existing stop monotonicity and executor rejection
of invalidated fast context. The original offline suites use mocked broker paths.

Before live rollout, reconcile this patch with the installed source and any
unmerged overlays, including PR #17 if installed. Do not replace an unknown AWS
checkout with this branch. No deployment or live trading activation is included.

Replay timestamped legacy suggestions, rejected candidates and actual fills
using only information available at each decision. Compare the frozen baseline
and this patch on chronological held-out trading days, with the same rupee risk
budgets and actual spreads, fees and fill assumptions. Count filtered winners
as well as avoided losses. Group overlapping trades by day/episode rather than
treating every candidate poll as independent evidence. Missing quote/fill data
must remain missing, not become a simulated successful fill.

Measure net expectancy, drawdown, trade count, adverse/favourable excursion,
stop-out-then-recovery within the original holding horizon, and forgone winners.
Separate initial-stop, trailing-profit and time exits. A lower stop-hit rate
alone is insufficient: wider planned stops can make individual losses larger,
and filters may reject profitable fresh breakouts from consolidation. Tune on
training days only and freeze thresholds before evaluating held-out days.

Trade-log replay, score recalibration, target-room filters, retest/continuation
separation and adaptive trailing/time exits remain subsequent work. This patch
does not claim a measured increase in win rate or profit.
