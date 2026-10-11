# Option-chain forecast data — Part 5 report

Implemented Parts 1–4 and verified Part 5. Restarted kadima at
2026-10-10 02:37:38 IST (Saturday); systemctl reports active and the control API
health endpoint returns ok=True. The last 20 journal lines were displayed with
credential-related lines masked and saved in tools/option_chain_restart.log.
Persistent controls are unchanged: auto_trade_enabled=False, kill_latched=True,
revision=35. No order endpoint was invoked by this work.

## Changes

Existing source files changed:

- kadima/rate_limiter.py — background grants use the same SQLite quote budget,
  timeout=0, no FIFO ticket, and skip when foreground calls are active/queued.
- kadima/hourly_forecast.py — starts/stops the sibling collector, creates the
  forecast-only tables, prefers recent wide data, adds previous-session/build/
  max-pain features and exposes frozen inputs to the panel.
- kadima/hourly_forecast_model.py — includes the new OI features in the bounded
  tilt; still uses the existing learned beta_oi and champion/challenger logic.
- tools/forecast_nightly.py — adds bhavcopy maintenance and the standalone
  --only bhavcopy / --date interface, returning before any broker history code.
- web/hourly_forecast.html — displays PCR/previous PCR, walls and max pain.
- /var/www/html/forecast.html — deployed copy of that panel; its original
  content matched the backed-up source exactly.

New source files: kadima/forecast_chain_data.py, kadima/forecast_bhavcopy.py,
tools/test_option_chain_data.py. New evidence artifacts: this report,
tools/option_chain_data.diff, tools/option_chain_tests.log and
tools/option_chain_restart.log. The earlier generic preflight test runner also
updated tools/preflight_tests.log on its first validation run; final results
are kept in the dedicated option-chain test log.

Backup folder: /home/ubuntu/backup-codex-optionchain-20261009T202514Z/.
It contains the five original source files, the served panel under
deploy/forecast.html, an online SQLite backup of forecast.db, and intermediate
implementation copies. Trading gates, thresholds, stops, sizing, Celestial
config and oi_snapshots schema/writers were not changed.

Full diff -u for every changed/new source file and the deployed panel:
tools/option_chain_data.diff (877 lines).

## Collection and API budget

The service's FORECAST_INDICES is currently its default NIFTY only. Wide
collection is attempted at the 76 five-minute boundaries from 09:15 through
15:30, starting in the +20-second window. A full 31-strike CE/PE ladder takes
two <=50-token FULL quote calls: at most 152 scheduled wide calls per session.
The current master lists 424 contracts in NIFTY's nearest two expiries
(13 and 19 October), requiring 9 EOD batches. Current-master planned maximum:
161 additional quote calls per session. EOD counts change with the master;
general formula is 152 per enabled index plus ceil(two-expiry contracts/50).
Unavailable strikes, no session/fresh spot, busy budgets, partial responses or
missed deadlines reduce successful coverage.

The collector shares the existing authenticated SESSION.api and instrument
master. It never calls ensure(), logs in or refreshes authentication. Every
batch passes through SHARED_QUOTE.try_low_priority using the same budget file
and spacing as ordinary engine quotes. It does not queue, wait for a budget
lock or hold an engine lock while doing network I/O; pending or in-flight
foreground work causes a skip. Successful background grants still consume the
shared budget: a request that arrives after such a grant remains subject to
normal quote spacing. A broker request already sent cannot be pre-empted, so
absolute zero added latency for every future arriving poll is not guaranteed.
This is priority-based best-effort collection, not a proof of zero contention.
No new live Angel quote calls occurred during this closed-market validation.

Snapshots commit only after all requested batches succeed and before their
deadline. Wide timestamps reflect completion, not the earlier boundary. EOD
saves all strikes of the two nearest listed expiries; OI change is against the
matching contract on the previous saved EOD day, otherwise NULL. Weekly-expiry
indices use their nearest weekly contract; indices without weekly listings
necessarily use the nearest listed expiry, never an invented weekly contract.
Wide retention is 60 observed sessions per index, pruned once at 15:35. EOD and
bhav_options are retained indefinitely. No trading-table data was inserted.

## Official archives and backfill

NSE's All Reports–Derivatives page identifies the UDiFF report and the legacy
format's discontinuation. Its official format spreadsheet supplies the ISO
column layout:

- https://www.nseindia.com/all-reports-derivatives
- https://www.nseindia.com/static/resources/forms-formats-members
- https://nsearchives.nseindia.com/web/mediaattachment/2026-06/UDiFF_trade_and_Bhavcopy_file_formats_20260630115803.xlsx

The official archive URL was validated by successful downloads:

https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20261009_F_0000.csv.zip

The downloader requests only this daily archive template, with the published
legacy historical/DERIVATIVES/YYYY/MON/foDDMMMYYYYbhav.csv.zip fallback on 404.
Timeout is 20 seconds; at most two attempts per invocation; normal re-runs cache
successes and same-day blocked/missing results. No option-chain endpoint,
cookie warmup or alternative host was used. The initial 30-session attempt
exposed an incorrect lot-size conversion in my parser; that was corrected and
tested against real rows. One validation retry per date stayed within two
archive requests for each date, two seconds apart. The inspected first-date
CSV was re-ingested locally rather than requested a third time. All 30 dates
now have ok status. Scheduled subsequent jobs fetch one latest session; the
standalone --only path makes no broker history/login calls.

NSE UDiFF TtlTradgVol is preserved as published in contracts; the official
format labels it Total Traded Qty, whereas legacy explicitly names CONTRACTS.
No unverified lot-size conversion or cross-source volume calculation is used.
Actual expiry is preferred when provided. Only NIFTY/BANKNIFTY/FINNIFTY index
options are parsed. SENSEX bhavcopy was skipped; no BSE archive was added.
403/timeouts/non-ZIP/invalid archives record blocked without crashing the job;
previous-session features use chain_eod when official data is unavailable.

Commands run for each of the five latest trading days:
python3 tools/forecast_nightly.py --only bhavcopy --date YYYY-MM-DD

| Trading date | Result | Stored rows |
|---|---|---:|
| 2026-10-05 | ok | 3,285 |
| 2026-10-06 | ok | 3,287 |
| 2026-10-07 | ok | 3,217 |
| 2026-10-08 | ok | 3,257 |
| 2026-10-09 | ok | 3,311 |

These final commands reused the successful backfill and made no repeat archive
requests. Row totals in /var/lib/kadima/forecast.db after restart:

| Table | Rows |
|---|---:|
| chain_wide | 0 |
| chain_eod | 0 |
| bhav_options | 92,773 |
| nightly_status | 31 |

The 31 status rows are 30 successful daily archives plus the backfill marker.
Wide/EOD rows are zero because it is Saturday and the engine waits for its
market-session start. Those future live captures have not been broker-tested.

## Tests

All seven changed/new Python files compiled. All eleven tools/test_*.py scripts
passed, including test_offline.py and test_hourly_forecast.py, with isolated
temporary data directories and real broker/network transport disabled.
The new suite covers both CSV layouts, published volume and actual expiry,
wide preference, empty-table legacy fallback, previous-session bhavcopy/EOD
selection, OI builds, max pain, strict no-look-ahead including t0 itself,
blocked-response persistence and cached retries, 50-token batching, atomic
partial-cycle rejection, 60-session retention and unchanged EOD history,
shared-budget lock/queue contention and in-flight foreground priority.
The inline panel JavaScript also passed node --check. Full output:
tools/option_chain_tests.log.

The 30-minute OI build compares identical expiry/strike/side ladders. If ATM
rolls or the expiry changes and the recorded ladders differ, it omits the build
rather than treating different contracts as OI changes. Max pain is computed
only over the recorded wide ladder, not every listed strike. Previous-day
features require the actual previous trading session; they do not silently
label older archived data as yesterday.

## Restart and rollback

Executed, outside the weekday market window:

```bash
sudo systemctl restart kadima
systemctl is-active kadima
```

Result: active; control API health ok. Last 20 masked journal lines:
tools/option_chain_restart.log. The same runtime guard used for this restart
rejects weekday 09:00–15:45 IST. Use that window restriction for rollback too.

Restore all original sources and the served panel, remove only this task's
three new source files, and restart. This retains the new forecast tables and
data harmlessly; do not overwrite forecast.db with an old snapshot and lose
subsequent forecasts.

```bash
(
set -e
cd /home/ubuntu/kadima-py
python3 - <<'PY'
from pathlib import Path
import shutil
backup = Path('/home/ubuntu/backup-codex-optionchain-20261009T202514Z')
for name in ('kadima/rate_limiter.py','kadima/hourly_forecast.py',
             'kadima/hourly_forecast_model.py','tools/forecast_nightly.py',
             'web/hourly_forecast.html'):
    shutil.copy2(backup/name, Path(name))
for name in ('kadima/forecast_chain_data.py','kadima/forecast_bhavcopy.py',
             'tools/test_option_chain_data.py'):
    Path(name).unlink(missing_ok=True)
PY
sudo cp /home/ubuntu/backup-codex-optionchain-20261009T202514Z/deploy/forecast.html /var/www/html/forecast.html
sudo systemctl restart kadima
systemctl is-active kadima
)
```
