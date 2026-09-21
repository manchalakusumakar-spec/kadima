# Guarded Gap-and-Go live rollout

This overlay upgrades the reviewed stock-fast release with the Gap-and-Go live
worker. It does **not** arm trading during source installation.

## 1. Plan and verify source upgrade

Run from a separately extracted copy of this reviewed release, not from the live
tree:

```bash
sudo -n python3 gap_live_update/install.py
sudo -n python3 gap_live_update/install.py --broker-check
```

The broker check is read-only. Installation requires the existing KADIMA
Celestial rollout to be paused, kill unlatched, local positions/intents flat,
broker positions/orders flat, exact reviewed source lineage, verified service
owners/entrypoints, and unchanged dependencies.

## 2. Install source

```bash
sudo -n python3 gap_live_update/install.py --broker-check --apply
```

The installer updates only the reviewed Gap source set and restarts the universe
scanner and engine. It does not set `KADIMA_GAP_LIVE` and does not resume
auto-trading.

## 3. Arm guarded live Gap trading

If stock fast momentum is already intended to remain live:

```bash
sudo -n python3 tools/activate_execution.py --apply --enable-fast --enable-gap
```

If Gap is the only optional live lane being requested:

```bash
sudo -n python3 tools/activate_execution.py --apply --enable-gap
```

The activation helper preserves an existing reviewed Fast flag, adds
`KADIMA_GAP_LIVE=1`, performs flat-book preflight checks before and after
service stop, verifies source identity, verifies runtime flags and reconciliation,
requires fresh news coverage, and only then requests the existing authenticated
KADIMA auto-resume endpoint.

## Live constraints

- One Gap lot maximum.
- One position per underlying.
- No averaging down or strike-ladder additions.
- Bull Shield v2.1 two-completed-5m confirmation remains mandatory.
- Gamma must be CONFIRMED >=70 with CE/OI direction agreement.
- Nearest FUTSTK confirmation is required when an active future exists.
- Fresh symbol news and macro-event suppression must be clear.
- Gap entry is revalidated immediately before order submission.
- Existing capital guard, daily-loss limit, kill latch, reconciliation and LIMIT
  order ledger remain mandatory.
- Underlying structure/VWAP failure requests a normal shared-executor exit.

A one-lot position cannot execute an exact 40% partial exit without violating
exchange lot-size rules. This first live version therefore does not pretend to
perform that fractional-lot action.

No claim is made that the strategy is profitable or that a LIMIT order will fill.
