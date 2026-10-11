# KADIMA preflight: AUTO-TRADE READY: NO

The requested task was fetched from origin/hourly-forecast without changing the
live checkout. The checker and offline verification are complete. Broker checks
and full live sizing verification remain incomplete: the engine keeps its
authenticated session in memory, and a new Python process would log in through
SESSION.ensure(), outside the permitted position-book/order-book/RMS-only calls.
No such login, broker request, order, reset, control change or restart was made.

## Full results

| Check | Result | Evidence / required action |
|---|---|---|
| 1. Service environment | PASS | FAST_MOMENTUM_LIVE, CELESTIAL_ENABLED and SESSION_AUTOTRADE are 1. API token present, masked. All three EnvironmentFiles read. |
| 2. Order placement flag | PASS | ORDER_PLACEMENT_ENABLED=True. |
| 3. Persistent controls | FAIL | auto_trade_enabled=False, kill_latched=True, threshold=65, revision=35. User controls left unchanged. |
| 4. Kill file | PASS | /var/lib/kadima/KILL absent. |
| 5. Next session permit | WARN | Monday 2026-10-12 09:16 IST is allowed by the calendar. Policy, lease and boot files valid/readable; renewal timer active. A permit for Monday cannot exist validly now: leases expire after 150 seconds. Recheck Monday. |
| 6. Execution ledger | PASS | Zero nonterminal local orders. This does not prove the broker order book is empty. |
| 7. Local positions | PASS | Zero active/pending positions. Five historical closed rows retain quantities, but load_open_positions excludes them. No rows changed. |
| 8. Broker books | FAIL | No current authenticated position/order capture available. Broker flatness is unverified. --broker-json accepts a timestamped capture no older than 150 seconds; unsuccessful responses never count as flat. |
| 9. Funds/sizing | FAIL | RMS unavailable. Last stored ATM CE one-lot buffered costs: BANKNIFTY 55300 CE, lot 30, premium 811.75 -> Rs 25,210.93; NIFTY 22500 CE, lot 65, premium 137.90 -> Rs 9,279.46. These are historical estimates, not Monday quotes. Structural stop, bid/ask and live cash are required for complete risk_budget/portfolio_capacity sizing. |
| 10. Daily loss | PASS | At check time IST date 2026-10-10; actual exit_manager.realized_pnl_today=0; limit Rs 10,000. Query uses the IST day, including external reconciled fills. |
| 11. Index news | PASS | BANKNIFTY, NIFTY and SENSEX return None at next session 09:30. News-health record exists, last timestamp 2026-10-10 00:30:24 IST. Future breaking news is not predicted. |
| 12. Credentials/IP | PASS | Required credentials present, masked. session.py IP and EC2 IMDSv2 IP both 18.61.12.184. Authentication and SmartAPI dashboard registration not independently verified. |
| 13. Services/disk | PASS | kadima and nginx active; root free 2.40 GiB, 86.7% used. Largest-directory inventory is in the raw output; some private directories were unreadable. |
| 14. Timezone | PASS | Engine config and checker use Asia/Kolkata explicitly. |
| 15. BANKNIFTY replay | FAIL | Both recorded SETUP_READY setups now return only auto_paused and kill_latched from current fast_momentum.live_blocks. No fresh_news_coverage_required. Full executor eligibility still needs current session, reconciliation, quotes, quality and funds. |

The current risk fraction is 15%, not 3%; allocation bands are still in place.
STARTING_CAPITAL_INR remains a dead constant; available_equity uses live RMS.
There is no MAX_RISK_PER_TRADE_INR=750 cap in risk_budget. No sizing change was
made without verified evidence that a code defect blocks one lot. Missing
broker evidence is not a reason to replace risk rules or assume cash.

## BANKNIFTY setups

| Contract | Suggested IST | Score | Current lane blockers |
|---|---|---|---|
| BANKNIFTY27OCT2655100CE | 2026-10-09 09:30:21.468847 | 74.41 | auto_paused, kill_latched |
| BANKNIFTY27OCT2655200CE | 2026-10-09 09:33:26.764796 | 89.15 | auto_paused, kill_latched |

## Manual stop: diagnosis and verification

The retained records contain no managed Position for your NIFTY 22250 PE, so
check_exits had nothing to protect. The original reconciliation code stopped
before adoption on an unresolved SELL for a different SENSEX position; original
CARRYFORWARD handling would also only observe a valid long. Your manual buy's
product, quantity and entry basis were not recorded, so its exact adoption
classification cannot be proven. Neither the late-day window nor a connection
to the kill latch is demonstrated.

The instrument cache identifies NIFTY13OCT2622250PE as token 44597, lot 65.
There are zero exact-contract/token audit events on 9 October and no positions
row. Its 510 stored option samples run from 09:15:39 at 86.30 to 15:39:36 at
23.80 (range 22.85–95.00), not a measured return from your unknown entry.
The old SENSEX unresolved SELL generated 3,043 events from 09:00:19 to 15:39:55.
auto_arm_enabled records revision 33 at 09:05:01.959223. Revision 35 is now
latched, but no retained transition event proves the kill time, cause or a
squareoff. The exact original code lines and full evidence are in
docs/manualstop-20261009T191938Z.md; the refinement report supersedes its
implementation notes.

Parts B/C were already implemented before this preflight and have been
reverified: MANUAL_PROTECTED adoption, product persistence and matching SELL
product, 30% hard stop, existing 50% peak-gain retention after +10%, manual
protection inside the late-day window, carryforward EOD exclusion, same-contract
pending-order handling, durable warning deduplication and UI effective stops.
The existing stop/trail policies for owned strategy positions remain unchanged.
The manual suite exercises 24 scenarios including the seven requested cases.
Protection is engine-monitored limit exits, requiring verified quantity and
fresh quotes; it is not a broker-hosted stop or guaranteed loss cap.

## Files, backup, tests and diffs

New in this task: tools/preflight_autotrade.py and tools/test_preflight_fixes.py.
Evidence artifacts: tools/preflight.diff, tools/preflight_tests.log,
tools/preflight_output.txt and this report. No existing engine source file was
changed in this preflight. Backup/manifest:
/home/ubuntu/backup-codex-preflight-20261009T195730Z/.

Both new Python files and all previously changed manual-protection Python files
compiled. All ten tools/test_*.py scripts passed with temporary databases,
network calls disabled and broker submission mocked, including test_offline,
test_manual_protection and test_preflight_fixes. Full diff -u for each new source
file is tools/preflight.diff. Prior engine diffs are tools/manual_protection.diff
and tools/manual_protection_refinement.diff. No live schema migration was run.

## Your remaining checklist

1. Verify Angel One positions, pending orders and RMS cash. Close/protect any
   manual holding or explicitly retain it; same-underlying carryforward and
   same-contract external orders can still block new entries/adoption. Supply a
   fresh authenticated capture to complete automated checks 8/9.
2. Rotate Angel One API key, MPIN/TOTP secret and KADIMA_API_TOKEN exposed earlier;
   update their private service environment values and client integrations.
3. After broker-flat verification, use Celestial Reset kill, then Resume. These
   remain user actions; this task has not changed the controls.
4. Restart to load the already-installed manual protection changes, outside
   09:00–15:45 IST on weekdays. Then recheck adoption/stop status and morning
   permit renewal. At this report's time it is Saturday in IST.

Restart command (not executed):

```bash
sudo systemctl restart kadima
```

Morning checker, from ~/kadima-py:

```bash
python3 tools/preflight_autotrade.py
```

Recheck at 09:16 too: an entry permit is not yet granted at 09:00. Where host or
private-file access is unavailable, the checker fails rather than reporting a
false PASS. With a fresh broker capture:

```bash
python3 tools/preflight_autotrade.py --broker-json /path/to/fresh-books.json
```

Rollback this preflight's two new source files (no existing sources to copy
back); retain the evidence reports. Restart only in the same permitted window:

```bash
cd /home/ubuntu/kadima-py
rm -- tools/preflight_autotrade.py tools/test_preflight_fixes.py
sudo systemctl restart kadima
```

If you also want to roll back all earlier manual-protection source changes,
restore the original sources and remove the new helper/test as follows. This
keeps the live database and its additive columns/tables; do not restore an old
database over subsequent trading records.

```bash
cd /home/ubuntu/kadima-py
python3 - <<'PY'
from pathlib import Path
import shutil
initial = Path('/home/ubuntu/backup-codex-manualstop-20261009T191938Z')
refinement = Path('/home/ubuntu/backup-codex-manualstop-20261009T193405Z-refinement')
for name in ('config.py','storage.py','angel_orders.py','auto_executor.py',
             'celestial_adoption.py','owned_watchdog.py','control_api.py','trade_audit.py'):
    shutil.copy2(initial/'kadima'/name, Path('kadima')/name)
shutil.copy2(refinement/'kadima/external_reconciliation.py',
             Path('kadima/external_reconciliation.py'))
for name in ('kadima/manual_protection.py','tools/test_manual_protection.py'):
    Path(name).unlink(missing_ok=True)
PY
sudo systemctl restart kadima
```
