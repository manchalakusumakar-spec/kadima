# KADIMA NIFTY precision — draft cache integration

Status: **DRAFT / NOT INSTALLED / NO AWS APPLY / NO TRADING AUTHORITY**.
This directory is additive on `claude/forecast-v21` commit
`15a05c25497f3cb93b6f5ec61025de6824737ce4`. No pre-existing repository file,
strategy, shared indicator, service, database, dashboard or trading flag changes.
This is a first integration checkpoint, not a completed live replacement.

## What is implemented

- Imports the earlier standalone `precision_lab` candidate, immutable research
  journal, whole-session walk-forward evaluation and its 55 tests.
- `precision_lab.cache_adapter.capture(...)` reads the native index state,
  bounded closed candle rows and all-50 constituent radar without importing the
  trading engine or calling Angel One. Requires an explicit dated weight
  manifest matching the radar's members, weights, source and validity dates.
  By default all 50 current/prior endpoints must be fresh. Five heavyweights
  cannot silently replace all-50 coverage.
- Computes forecast-local EMA20/50, Wilder RSI14, MACD12/26/9 and return features.
  These smoothing/seeding choices are explicit **new research definitions**,
  not claimed to be the recovered historical winning configuration.
- Optional OI/Greeks inputs require independent feature timestamps and
  expiry/contract scope. A fresh price or successful `_last_poll` is not proof
  that each OI/Greek value is fresh. A VWAP feature needs an identified
  volume-bearing instrument and a declared mapping. Absent inputs stay absent.
- `comparison.py` adds last-price and causal-trend baselines. Champion comparisons
  require identical snapshot hash, original issue time, anchor and target list.
  Legacy forecasts lacking this binding are rejected instead of being relabelled.
- Separate 5,10,...,60-minute predictions, strict `abs(actual-forecast) < 5`,
  immutable originals, separately identified revisions, and no signed-error
  cancellation. The +/-5 zone is a target, not a probability band. Net Error's
  aggregate formula remains unspecified and therefore unset.

## Native cache integration boundary

```python
from precision_lab.cache_adapter import capture
packet = capture(engine, read_bars, manifest,
                 source_id=verified_source_id, config_id=nonsecret_config_hash,
                 clock=aware_ist_clock, enrichments=timestamped_optional_features)
```

`read_bars(token, limit, until)` must return at most 180 locally cached rows with
`interval,start_ts,open,high,low,close,closed`, and must not import a broker or
initialize/migrate the trading database. The native source columns were read in
`kadima/storage.py` at the pinned base. The callback is dependency injection;
its actual host implementation and invocation hook are **not installed here**.
Run only on a bounded non-order-processing path after source-matched review.

The index state's `last_updated` is labelled as local receipt time. This adapter
**does not fabricate exchange event time**. Native radar exchange timestamps are
read under its lock; cache-capture time is used as conservative availability.
SDK decoding, cache thread-safety on the composed host, feed gaps and true market
coverage still need runtime verification. A complete stored candle is not an
independent attestation that no exchange ticks were lost.

The radar source keeps <=62 five-second samples per stock. That supports a
bounded five-minute contribution, **not 50 complete EMA/RSI/chart histories**.
This PR does not claim full indicator/option-chain history for each constituent.
Do not backfill absent historical features with future or current information.

No runtime hook is added while the installed source/overlays are unknown. The
newer forecast and the separate all-50 branch remain intact; we do not copy one
entire hourly module over the other. Next integration must bind the existing
champion and candidate to the same packet and journal both before publication.

## Local validation

```sh
cd forecast_precision
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

At this checkpoint: **98 local tests passed** (55 imported + 43 new). Tests use
synthetic/native-shape fixtures. No actual-market backtest, verified live accuracy,
AWS resource measurement or host deployment is claimed. Passing software tests
is not evidence that the model has achieved the five-point accuracy objective.

The PR workflow additionally requests the existing hourly, manual-buy, audit,
OI-confirmation, Shield, timing, CAS and offline suites in isolated temporary
homes with sockets disabled. Failures remain visible; no `continue-on-error`.
Those existing suites are not claimed passed until their exact-head CI completes.
Source scope verification rejects every modification outside the new directory
and two specifically named workflows; all pre-existing source is unchanged.
This is byte-preservation, not a substitute for eventual live-integration parity.

## Release preparation, not an installer

`release_plan.py --release-sha <exact 40-character SHA>` checks the checked-out
commit, additive scope and source blobs, then prints the unfulfilled AWS gates.
`--apply` always refuses. The manual workflow has `contents: read` only, no AWS
credentials, no OIDC token permission, no SSM call and no service command.
The 'Run workflow' UI needs the workflow on the default branch first; do not
merge the whole captured-source branch into main merely to make the button appear.
A separate reviewed workflow-only change can be considered later.

Remaining release gates:
1. Read-only installed/loaded source, service/interpreter and overlay identity.
2. A valid dated NIFTY50 manifest; per-feature freshness and SDK timestamp checks.
3. Host reader/hook plus independently timestamped OI/Greeks mapping; no extra polls.
4. Same-packet champion binding, immutable live publication, actual-price capture,
   full composed trading-preservation tests and bounded resource measurements.
5. Source-matched installer/backups/health checks/forecast-only rollback and an
   authorized, narrowly scoped AWS transport. Then live observation, not trading.

Do not run older auto-trading installers for this forecast. No root password,
TOTP, dashboard token or broker credential belongs in commits or workflow inputs.
No automatic promotion is implemented.

## Pinned source references

- Forecast base: `15a05c25497f3cb93b6f5ec61025de6824737ce4`.
- All-50 branch reference: `b2902137d75bce0815b1d48cede6f30222757b61`.
- Native radar blob: `9e7c4f4b77fc6ca3a7ba323cf8fa419f1e86ed70`.
- Native storage blob: `5875846967be1d2c1887ee99d5ad8d1cea20d326`.
- Newer hourly blob: `afccdc5f8d772456e40509e087a743ad336d3b9c`.

Action pins were resolved from the official actions repositories on 12 Oct 2026:
checkout v6 `d23441a48e516b6c34aea4fa41551a30e30af803`;
setup-python v6 `ece7cb06caefa5fff74198d8649806c4678c61a1`.

Official deployment background (not evidence of configured AWS access):
https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
https://docs.github.com/en/actions/reference/security/secure-use
https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html

The original `KADIMA_NIFTY_Forecast_Integration_Handoff.md` is retained in the
conversation as the earlier research audit. This README records the additional
first integration work; source IDs do not establish current AWS-loaded identity.
