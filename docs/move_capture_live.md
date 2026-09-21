# Move Capture — guarded live execution extension

## What changes from the shadow detector

The spot detector remains an isolated WebSocket process. It still cannot place,
modify or cancel broker orders.

When `KADIMA_MOVE_CAPTURE_LIVE=1`, fresh hot **NFO stock** candidates are
published into a durable SQLite handoff. A new worker inside the existing main
KADIMA process consumes that handoff, performs option-side confirmation, and
calls the existing serialized `AutoExecutor.evaluate_and_enter()` path.

This makes Move Capture live-order capable without creating a second broker
writer.

## Live path

```
~210 NSE F&O underlying spot WebSocket
        |
normalized impulse score >= trigger
        |
fresh durable Move Capture handoff
        |
engine-side MoveCaptureExecutionWorker
        |
ATM +/-2 option ladder, nearest expiry
        |
two fresh option samples >=2.5 seconds apart
        |
liquidity/spread/delta/OI/volume through existing strike selector
        |
premium + volume + order-book confirmation
        |
existing fast-momentum executor contract
        |
existing reconciliation / news / kill / session / capital / daily-loss /
lot-size / duplicate / broker-order gates
        |
bounded LIMIT order
```

## Entry evidence

A candidate is not submitted from spot impulse alone.

The engine-side worker requires:

- fresh Move Capture episode and direction;
- stock scope only (indices remain with the existing index strategies);
- active NFO stock mapping and current nearest expiry;
- two complete option samples 2.5–12 seconds apart;
- the same selected option token across the confirmation samples;
- existing strike-selector tradeability rules (OI, volume, spread and delta);
- option premium increase of at least 0.25% between confirmation samples;
- positive cumulative option-volume increment;
- bid and ask not materially falling (within 0.5%);
- structural spot invalidation level from a 50% retracement of the detected
  impulse;
- `KADIMA_MOVE_CAPTURE_LIVE=1`;
- the existing fast/live master gate, global auto-trade gate, kill latch,
  current score threshold, and fresh symbol news coverage.

The 0.25% option confirmation and 50% spot-retracement rules are explicit
starting hypotheses. They are not probabilities or claims of profitability.

## Existing executor behavior retained

The live extension intentionally reuses the existing `fast_momentum` executor
contract instead of adding another direct order path. The Move Capture context
is tagged with `source_strategy=move_capture`.

This preserves:

- serialized order submission;
- startup/broker reconciliation;
- execution-ledger de-duplication;
- current capital and lot sizing;
- daily-loss enforcement;
- contract/expiry validation;
- fresh option quote check immediately before submission;
- bounded LIMIT entry pricing;
- structural stop conversion from spot to option premium;
- existing dynamic/profit-trail monitoring;
- five-minute no-progress protection for the reused fast-momentum position kind;
- the deployed multi-position portfolio overlay, provided deployment does not
  replace the AWS `auto_executor.py`.

## Important deployment rule

**Do not copy or checkout the whole Git branch onto AWS.**

The production host already has a newer multi-position `auto_executor.py`
overlay than the base source in this feature branch. The Move Capture live
installer must copy only the reviewed Move Capture files plus `run.py`, after
verifying expected hashes. It must not replace `auto_executor.py`, risk
configuration, scheduler files, Gamma Blast or CAS code.

## Gamma and CAS isolation

No Gamma Blast source or threshold is changed.

No CAS source or threshold is changed.

The Move Capture spot service is a separate failure domain. Its only production
interaction is the handoff database. The central engine worker can stop or fail
without changing Gamma/CAS scoring logic.

## What is still not the final target

This live-ready version still uses the existing executor's bounded LIMIT entry.
It does **not** yet implement repeated marketable-limit modification/chasing.

It also uses recent same-session volume-rate normalization in the detector,
rather than a calibrated historical time-of-day baseline.

Those improvements can be layered later without changing the spot discovery
architecture.
