# Guarded MCX live rollout

This release adds one-lot long-option trading for:

- CRUDEOIL
- GOLD
- NATURALGAS
- SILVER
- COPPER

The live lane does **not** short futures. It reads the nearest active FUTCOM
contract for direction and requires both futures and near-ATM OPTFUT five-minute
price/volume/OI confirmation before buying CE or PE.

## Safety boundary

- Separate `KADIMA_MCX_LIVE=1` opt-in.
- Explicit rupee capital cap via `KADIMA_MCX_CAPITAL_INR`; no default.
- One MCX auto position maximum.
- One lot maximum.
- Existing global kill, daily-loss limit, broker reconciliation, macro-event
  suppression and durable order/fill ledger remain mandatory.
- Fresh MCX FULL quotes and a five-minute observation window are required.
- Futures move >=0.35%, volume-rate spike >=1.5x, and positive OI build.
- Selected option premium move >=5%, volume-rate spike >=1.5x, positive OI
  build and spread <=1.5%.
- Structural option stop is modelled from the five-minute futures structure.
- Dynamic profit trailing uses the existing KADIMA long-option protection.
- MCX positions are exempt from the equity 15:38 force-close and instead close
  by 23:25 IST.
- Monitoring/entry window ends at 23:30 IST.
- Existing manual carry-forward exposure remains governed by the reviewed
  reconciliation rules.

These thresholds are heuristics, not probabilities or profit guarantees.

## Install source

Run from a separate reviewed checkout/worktree:

```bash
sudo -n python3 mcx_live_update/install.py
sudo -n python3 mcx_live_update/install.py --broker-check
sudo -n python3 mcx_live_update/install.py --broker-check --apply
```

Installation does not enable live MCX trading.

## Activate

Example with a ₹15,000 MCX capital ceiling:

```bash
sudo -n python3 tools/activate_execution.py --apply --enable-mcx --mcx-capital 15000
```

The activation helper preserves already-reviewed Fast/Gap flags present in the
existing activation environment. Global auto-resume still requires healthy
reconciliation and news gates.

Do not run MCX activation while another source installer is still running.
