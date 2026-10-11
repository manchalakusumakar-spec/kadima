# Manual protection refinement

The existing stops and dynamic trails for KADIMA's own trade lanes remain in place. This change strengthens manual-position integration and recovery. It supersedes the implementation notes in the earlier manual-stop report; the historical diagnosis remains unchanged.

| Existing policy | Integration retained |
|---|---|
| Shield/Gamma and other ordinary owned options | Existing structural stops, trade_policy.ratchet, Celestial's dynamic retention, and optional forecast-trail decisions |
| CAS blast | Existing cas_executor exit rules, including its own activation and peak giveback |
| CAS premium lane | Existing cas_engine protection and exit rules |
| Manual long index/F&O-stock options | The requested 30% hard stop; existing trade_policy.ratchet after +10%; persisted tighter stops and original product preserved |

No changes were made to trade_policy.py, celestial.py, exit_manager.py, cas_engine.py, cas_executor.py, forecast_trail.py, risk-setting values, gamma sizing or auto-executor sizing.

Refinements:

- Signal and Position adoption commit in one SQLite transaction. A simulated failure after writing the position rolls both records back; executor memory is updated only after the commit.
- A tightened manual stop now applies before trailing activation. Entry 100 with a tightened stop 95 requests an exit at 94 instead of waiting for the initial 70 stop.
- External manual scale-ins use the configured 30% floor instead of the generic 5% floor. Existing tighter protection never moves down. Other kinds retain their existing scale-in calculation.
- Broker-evidenced partial manual SELL fills update remaining quantity, lot count and deployed entry basis together without loosening the stop.
- Manual adoption and verification can run with automatic Celestial rollout disabled when manual protection is enabled and the contract master is available. Automatic entry controls, the kill latch and same-contract reconciliation safeguards remain binding.
- None/zero/negative/non-finite/boolean manual quotes cannot update peaks/stops or authorize an exit. Invalid protection inputs fail with an explicit warning. Existing owned-lane calculations are untouched.
- Warning episodes are persisted in manual_protection_warnings. Repeated identical failures are deduplicated across engine recovery; a full alert queue leaves the warning retryable. A changed failure or recovered/reopened holding can produce a new warning. Queue acceptance is not proof of Telegram delivery; this uses the existing alert dispatcher, not a new durable messaging outbox.
- Unverified manual quantities remain untraded and produce a warning instead of silently appearing protected. No broker-flat or fill assumptions were introduced.
- Queued exits stay binding after price rebounds or an unfilled SELL is cancelled; the existing execution ledger controls retries and prevents concurrent duplicate intents. All tests use mocked transport.
- The top20 positions response exposes protection_status and protection_active for manual positions: monitoring, broker_unverified, quote_unavailable, invalid_protection_inputs, opted_out, exit_due, or exit_pending. Protection_active is true only for monitoring with verified broker ownership, valid protection inputs and a fresh cached quote.
- Manual protection evaluations are audited once per position per minute, with an extra event when the exit reason changes. Failure of these new telemetry events cannot prevent the protective exit request.

The protection remains **engine-monitored SELL limits**, not stop orders hosted at the broker. It requires a running engine, a usable instrument master, broker-verified ownership and usable quotes. It does not guarantee a fill or cap loss at exactly 30%.

Validation: all seven changed/new Python files compile. All nine tools/test_*.py scripts, including tools/test_offline.py, passed with isolated temporary databases, network/broker transport disabled and submission mocked. tools/test_manual_protection.py now exercises 24 scenarios, including all original seven cases, persistent-warning recovery, queue-full retry, atomic adoption failure, invalid quotes, rollout-off protection, scale-ins, partial manual SELLs, tightened stops, stop monotonicity, cancelled SELL/rebound handling, UI state and telemetry failure. No live orders, API calls, configuration resets or service restart occurred. A read-only live check still showed revision 35, auto_trade_enabled=False and kill_latched=True; the new warnings table had not been created in the live database.

Files changed: kadima/storage.py, auto_executor.py, celestial_adoption.py, external_reconciliation.py, control_api.py, tools/test_manual_protection.py. New helper: kadima/manual_protection.py. This report and the refinement diff/test-log artifacts are also new.

Backup: `/home/ubuntu/backup-codex-manualstop-20261009T193405Z-refinement/`.

Diff: `tools/manual_protection_refinement.diff`. Tests: `tools/manual_protection_refinement_tests.log`.

Restart only after 15:45 or before 09:00 IST:

```bash
sudo systemctl restart kadima
```

The following rolls back **only this refinement**, retaining the initial manual-protection fix and its product persistence. Run outside the same market-time window. It restores source files, not an old database; additive schema tables may remain:

```bash
(
set -e
cd /home/ubuntu/kadima-py
python3 - <<'PY'
from pathlib import Path
import shutil
backup = Path('/home/ubuntu/backup-codex-manualstop-20261009T193405Z-refinement')
files = ['kadima/storage.py','kadima/auto_executor.py','kadima/celestial_adoption.py',
         'kadima/external_reconciliation.py','kadima/control_api.py','tools/test_manual_protection.py']
for name in files:
    shutil.copy2(backup/name, Path(name))
Path('kadima/manual_protection.py').unlink(missing_ok=True)
PY
sudo systemctl restart kadima
)
```
