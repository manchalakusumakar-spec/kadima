# KADIMA Celestial — implementation draft

Built from PR #1 head `e1d42a175b2e3bf29886b16474fa79f57aaedecd`.
This is a separate, stacked draft. The user installed backend and dashboard
commit `cb482d77961af34ea65d7f9ff8af316bb2d37810` on EC2 on 16 September 2026
at 07:02 IST. No merge, test broker orders, credential changes or activation
of Celestial were performed.

## Confirmed installation — 16 September 2026, 07:02 IST

The supplied terminal transcript reports successful backend installation and
dashboard upgrade. All 33 backend files match the reviewed release and all four
systemd services are active/running. The installer passed its flat broker/local
preflight; the subsequent local inventory has zero open positions and pending
execution intents. These are observations at installation, not a continuing
guarantee of account flatness.

Celestial rollout and automatic entries remain false; threshold is 65. The
existing broker-order gate is true, so legacy trading settings still matter.
Backend backup: `/home/ubuntu/kadima-executor-backup-dp8l88yo`.
Dashboard backup: `/var/backups/kadima-dashboard-upgrade-xa7jf5ev`.

The first inventory incorrectly reported `celestial_api: false` after reading
its configuration successfully: unset reconciliation/news health is JSON null,
and the reporter attempted `.get()` on that null value. The reporter now records
API reachability separately from unavailable worker health. Two regressions
cover null health and invalid snapshots/health flags. This is a diagnostic-only
fix and needs no engine or dashboard restart. Authenticated browser display,
market-session feed capacity, fresh news coverage and unattended live readiness
remain unverified.

## What is implemented

### Dashboard restoration follow-up

The first Gamma Desk page replaced panels that should have been retained.
The dashboard now combines its Gamma Top 20 and order controls with the previous
Five Pillars, session P&L/capital/risk ledger, entry/stop/target position view,
Master Checklist, observed five-minute price history, macro/events, news,
F&O universe, forecast observation and data coverage panels. They use the
existing authenticated gateway routes and refresh independently of Celestial's
rollout flag. Market/positions poll every four seconds; macro/universe every
20 seconds. Failed feeds retain explicit last-known labels. News and macro
scrolling can be paused and respects reduced-motion preferences.

There is one Gamma Top 20 table. When Celestial is active and its API responds,
it uses server-ranked contracts and the existing candidate-ID execution path.
When Celestial is off or its snapshot is unavailable, it displays a labelled,
read-only legacy watchlist: up to 20 directional index/stock signals ranked
by available source freshness then Gamma score. It does not infer strikes,
manufacture probabilities, enable BUY or claim full-universe coverage from
that legacy feed. Existing legacy position exits remain routed through their
original owned-position exit endpoint. No trading flags or backend code change.

Install this frontend update with `dashboard_live/upgrade.py --apply` from
the reviewed checkout. Its manifest accepts the exact currently deployed HTML
blob `c3d510005ba0b99a0c176aabc2823b9b19420f12`. The updater backs up the two
dashboard files and restarts only the dashboard gateway; it does not require
the backend maintenance installer. The new page still requires EC2 installation
and user verification after sign-in. Offline browser fixtures cover the added
panels, live refresh, fallback ranking limit, off-mode read-only behavior,
failure labels and mobile width as well as existing controls.

| Requirement | Implementation / boundary |
|---|---|
| One Gamma Top 20 list | Shared SQLite ranking used by dashboard, BUY and auto-entry. One best strike per underlying/direction. Expiry first, then score; never padded with weak rows. |
| +5% premium radar | Measured five-minute window, minimum full warm-up, reset after gaps/day rollover/cumulative-volume reset. Latest-minute volume rate versus prior four minutes. This is a detection threshold, not a guaranteed profit. |
| Contract/expiry mapping | Active OPTIDX/OPTSTK contracts from master expiry dates, not hardcoded weekdays. Unknown index spot mappings reported unresolved. Fresh master required when new mode is selected. |
| Threshold dropdown | 65/70/75/80/85/90; default 65. Durable validated config, compare-and-set revision and immutable mode/display limit. Score is not a win probability. |
| Autonomous entry bridge | Explicit candidate identity, current Top 20 membership and revalidation before submission. Retains freshness, alignment, structural stop, liquidity, capital, slot and ledger checks. No raw browser order parameters. |
| Duplicate prevention | One durable candidate signal per contract/day plus existing uncertain-order/partial-fill ledger. This conservative cooldown can skip later independent moves in the same contract. |
| Pause/resume/kill | Serialized with entry. Pause blocks auto entries but not protective monitoring. Kill latches all entries off and requests managed exits, including cancellation/final-fill handling for pending BUYs. Kill reset requires verified flat account. |
| External adoption | Periodic broker reconciliation adopts only verified, whole-lot, long INTRADAY NFO/BFO options with a known current contract and valid broker buy basis. Source tag and initial 5% engine-managed stop persist. It does not open an order on adoption. |
| SL/trailing controls | Stop prices and retained profit may only tighten. +5% activates trailing; retained peak gain is at least 50%, 60% at +10%, 70% at +20%, 75% at +35%, 80% at +75%. No upper target or compulsory partial booking. |
| News scoring interface | Validated, source-attributed, expiring events; direction-adjusted CE/PE scores; duplicate events cannot stack; hard BLOCK veto. Coverage is per symbol and expires after five minutes. |
| Dashboard | Top 20, open/adopted trades, status, threshold and controls at the existing `/dashboard.html` source path. No unfiltered universe/headline feed. Session-cookie authentication and same-origin allowlisted gateway; broker bearer token remains server-side. |
| Audit/postmortem | Existing signals, execution intents/fills, persisted positions and rejection logs retained. Candidate source, selected strike and score snapshot are stored with the durable signal. |

## Follow-up implementation

- `full_universe_scanner.py` replaces the rotating 20-stock shortlist **when
  Celestial is selected**. It discovers all active OPTSTK roots with a unique
  NSE cash token, retains every resolvable root (up to a refusal ceiling of
  512), and batches spot plus nearest-expiry ATM/adjacent CE/PE quotes. Seven
  stocks fit in one 49-token request. It shares the existing cross-process
  SmartAPI request budget. Actual cadence, missing mappings and successful
  90-second coverage appear in status. Candidate eligibility still expires
  after 30 seconds. It does not subscribe to every strike or every expiry.
- Contract lookups now use an in-memory index and retain fractional strike
  steps such as 2.5. A complete option sample is validated before any quote
  changes; partial/malformed responses cannot refresh the ladder timestamp.
- `news_collector.py` automatically starts with the new stock scanner. It
  fetches NSE announcements, board meetings, corporate actions and financial
  results RSS, mapping legal company names through NSE's equity list. All
  required sources and mappings must pass before coverage becomes healthy.
  Results filing alone is neutral; only explicit, unambiguous text rules
  produce directional adjustments. Existing source-attributed ingestion is
  retained for a more complete licensed news source.
- `celestial_exit.py` adds persisted completed-one-minute EMA9 monitoring,
  fresh opposite VWAP/Shield/Gamma/OI-direction evidence, spread above 3%,
  and active news BLOCK exits. Missing evidence is unknown, not an invented
  opposite signal. Stops/trails continue while indicators warm after gaps.
- `external_reconciliation.py` reconciles subsequent one-sided manual
  scale-ins, reductions and full exits of a verified long intraday option
  when terminal order-book deltas exactly explain the broker net quantity.
  Quantity, weighted basis, tightened protection and incremental realized P&L
  are committed together. Revised or mixed-side histories remain unresolved.
- `tools/deployment_report.py` provides a sanitized, read-only EC2 inventory;
  `dashboard_live/upgrade.py` checks known hashes, backs up and atomically
  replaces the existing gateway and HTML, preserving credentials and trading
  configuration. Only the gateway is restarted. Default is check-only.
- A GitHub workflow runs Python regressions and the fully intercepted
  desktop/mobile Playwright fixture. Workflow creation is not a passing run.

## Important unfinished work — NO-GO for unattended live deployment

- All-root nearest-expiry entry-ladder scanning is implemented, but **actual
  broker throughput, memory headroom and complete market-session coverage are
  not validated on AWS**. Existing notes record an OOM event on the host.
  Missing mappings, incomplete ladders and slow cycles can leave gaps. Every
  listed option strike/expiry is outside this REST scanning scope.
- **Public RSS freshness/coverage remains a release blocker.** Read-only
  source probes on 16 September 2026 IST retrieved the four NSE feeds, but the
  financial-results feed lacked a build timestamp and contained 24 August
  publications. That does not establish current publication coverage. The
  collector must report this as unavailable. RSS snapshots can roll at
  midnight and omit prior-day filings on first startup. BSE announcements,
  exchange broadcast messages, general business/sector/macro news and full
  historical bootstrap are not supplied by the NSE stock RSS adapter.
  Index news coverage therefore still requires a verified source. Generic
  Marketaux top-ten headlines are not treated as full corporate coverage.
- Sector impact and event-risk numeric models are not supplied; there is no
  fabricated bonus or penalty. A verified catalyst BLOCK still vetoes entry.
- Shorts, futures, carry/delivery products, malformed broker responses,
  external pending orders, mixed-side manual episodes, and unexplained manual
  size changes remain visible but **unmanaged/unresolved**. New entries are blocked.
  Confirmed unaffected quantities can still be protected; a malformed book or
  external pending order disables quantity-dependent exits until reconciled.
  Extending adoption to arbitrary instruments needs a separate risk/ledger design.
- Stops are **engine-managed, not broker-hosted protective orders**. Feed/API
  failure, process failure, broker rejections and unfilled LIMIT orders can
  delay or prevent exits. A kill request does not prove an account is flat.
  No fill chasing or broker-hosted protection was added. The new EMA/spread/
  reversal/BLOCK checks supplement structural, trailing, daily-loss and
  end-of-session exits. Loss of an OI observation is not proof of reversal;
  missing feed/API data can still prevent evaluation and fills.
- Initial adoption basis is the broker's reported buy average; prior manual
  realized P&L before initial adoption is not reconstructed. Later verified
  external fill deltas are accounted for. Account-wide realized/unrealized
  loss reconciliation still requires live validation.
- Exact exchange/broker session, expiry cutoffs, static-IP registration,
  market-session resource headroom are unverified. Installer process ownership
  checks passed during the reported EC2 installation.
  Existing session/cutoff values were not asserted correct or changed.
- Real browser rendering and live dashboard/gateway wiring must be verified
  before release. The browser fixture is fully intercepted and never uses the
  real dashboard or a live trading account.
  GitHub CI at `4f0ca45d85ce38debe559bc3ee77b47d0978a9c0` passed the automated
  desktop/mobile Playwright checks and captured screenshots:
  [verified run](https://github.com/manchalakusumakar-spec/kadima/actions/runs/35013522130).
  Local Chromium download failed. Visual inspection of those screenshots and
  authenticated live dashboard interactions are still unverified.

No system can guarantee catching every move, a +5% minimum capture, no losses,
or unlimited realized profit. Fees, slippage and gaps remain relevant.

## Activation and deployment boundary

`KADIMA_CELESTIAL_ENABLED` defaults to `0`. With it absent, legacy entry and
management continue under the existing deployed settings; the new dashboard's
mutation controls are disabled. Setting it to `1` switches index discovery and
entry ownership to Celestial and enables its adoption policy. Persisted
`auto_trade_enabled` still defaults to `false`. Do not set the flag against a
live account as an installation smoke test: adoption itself can lead to exits.

The existing `ORDER_PLACEMENT_ENABLED` broker gate is independent and unchanged.
Do not enable either live mode or automatic entries until the NO-GO items above
are resolved or an explicitly narrower, tested scope is approved.

The consolidated `executor_fix/install.py` manifest includes the complete new
backend dependency set and exact reviewed source hashes. It retains maintenance,
flat-account preflight, unknown-version refusal, backup and rollback safeguards.
It does not deploy dashboard files or enable the mode. Review
`docs/release_readiness.md` before any installation; do not sequence historical
incremental installers.

For a previously installed dashboard gateway, review/install the new gateway
and HTML together while preserving its private configuration and password.
The historical `dashboard_live/install.py` is a fresh-install path. The new
`dashboard_live/upgrade.py` is the conservative in-place path; the user ran it
successfully on EC2 as recorded above. Actual deployed hashes and process
ownership must still pass its checks on subsequent upgrades.

### First EC2 action: read-only report

Run these commands in the existing EC2 terminal. They fetch the review branch
into a separate temporary checkout and print an inventory. They do not change
the running checkout or restart services. Do not paste credentials.

```bash
git -C /home/ubuntu/kadima-py fetch origin codex/celestial-autonomous-gamma
kadima_review_dir=$(mktemp -d /tmp/kadima-review-XXXXXX)
git -C /home/ubuntu/kadima-py archive FETCH_HEAD | tar -x -C "$kadima_review_dir"
sudo -n python3 "$kadima_review_dir/tools/deployment_report.py"
```

Review the report before using the backend maintenance installer. It must
identify the real engine owner; the historical PM2 dashboard installation
and systemd engine installer must not be assumed to refer to the same live
process. Unattended activation remains NO-GO while blockers remain.

### Reviewed EC2 inventory — 16 September 2026, 01:01 IST

The supplied report identified all 33 backend paths as accepted versions:
12 already matched the release, 12 were older reviewed files and nine new
modules were absent. The engine, stock scanner, macro watcher and dashboard
systemd services were active; PM2 also reported `kadima-bridge` online.
The local ledger reported zero open positions and zero pending execution
intents. This does **not** establish that the broker account is flat.
Celestial was disabled and its API was not installed.

The installed gateway matched reviewed blob
`2fe07e07086493a86ed0d9df5a34a7e950c40d16`. A read-only HTTPS retrieval of the
actual `/dashboard.html` returned 32,809 bytes with Git blob
`ec46796858d2597c216fe48b49fbb565bae81c9e`, exactly matching the report. Review
against the earlier dashboard source found only gold colour substitutions,
a `kadima-gold-polish` CSS block for shadows, borders, focus/hover and reduced
motion styling, and trailing whitespace removal. No script or API behaviour
changed. That exact installed HTML hash is now accepted by the updater.

The backend installer now verifies the engine's working directory and
`run.py` entrypoint before stopping services. Its broker preflight requires
an explicit, finite zero net quantity for every position row; missing fields
cannot be mistaken for a flat account. The dashboard updater allows up to
30 seconds for a restarted backend connection to become available, while
rejecting authentication or incompatible API responses immediately.

### Source installation after inventory review

An attempted EC2 installation stopped at `broker_positions_not_flat` before
source replacement. The user subsequently confirmed all four original services
active/running. The installer now checks local and broker exposure **before
stopping any service**, and repeats that check after stopping to catch intervening
changes. A broker failure at the first check leaves services and source alone.
It does not infer zero exposure from an expired contract date or a screenshot.
Broker checks authenticate and read position/order books; they send no orders.

Run during **19:00–08:00 IST**, using the reviewed checkout above. The backend
step checks local and broker exposure, backs up source, and restarts the three
backend services. The next step updates the two dashboard files with its own
backup and gateway restart. The sequence stops at the first failure; a backend
success followed by a dashboard failure is a partial installation, not a
completed release. Each installer has its own source rollback. Neither changes
the rollout flag, broker gate, credentials or stored trading configuration.

```bash
sudo -n python3 "$kadima_review_dir/executor_fix/install.py" &&
sudo -n python3 "$kadima_review_dir/dashboard_live/upgrade.py" --apply &&
sudo -n python3 "$kadima_review_dir/tools/deployment_report.py"
```

Check the final report for matching backend hashes, active services and a
responsive Celestial API. Sign in to the dashboard again and verify the
display and disabled rollout status. Do not use live order buttons as a test.
Source installation is not approval to activate unattended trading.

## APIs (authenticated control service, port 8081)

| Method / path | Meaning |
|---|---|
| GET `/api/kadima/top20` | Ranked candidates, managed positions and explicit source/coverage status |
| GET / POST `/api/kadima/config` | Read / persist allowed fields; POST accepts optional revision |
| POST `/api/order/buy` | `{candidate_id}` only; exact server-ranked contract, existing sizing gates |
| POST `/api/order/sell`, `/api/order/exit` | `{signal_id}`; reduce-only owned long exit |
| POST `/api/order/sl`, `/api/order/modify-sl` | `{signal_id, price}`; tighter engine stop only |
| POST `/api/order/trailing-sl` | `{signal_id, retain}`; fraction 0.50–0.95, cannot loosen |
| POST `/api/order/cancel` | `{order_id}`; tracked pending orders only |
| POST `/api/order/squareoff-all` | Pause auto and request managed exits; report unresolved exposure |
| POST `/api/kadima/pause`, `/api/kadima/resume` | Change auto-entry status, not monitoring status |
| POST `/api/kadima/kill`, `/api/kadima/reset-kill` | Latch off + exit requests / verified-flat reset |
| POST `/api/kadima/news` | Trusted collector ingestion; not exposed by the browser gateway |

The dashboard uses `/dashboard-api/celestial` for its GET snapshot and
`/dashboard-api/celestial/{kadima-or-order/path}` for allowlisted writes.
The existing read-only LLM API is not given these mutation endpoints.

### Collector contract

POST body keys: `source`, `observed_at` (timezone-aware, at most five minutes old),
`covered_symbols` (explicit symbols successfully checked), and `events`.
Each event must have exactly `id`, `symbols`, `score` (-20/-10/0/10/20), `block`
(boolean), `url` (HTTPS source), `published_at`, and `expires_at` (within seven
days of publication). Stable event IDs must identify the same event across
repeated fetches. Positive underlying news assists CE and penalizes PE;
negative news is reversed for long puts. BLOCK applies to either direction.

An empty successful batch means the collector checked those symbols and found
no active events. Never submit an empty-success batch after a provider failure.
Collector mapping, authorization and coverage must be independently validated;
the API cannot prove an external collector actually checked a source.

## Offline verification

Validation performed locally through the reporting fix: 166 Python tests passed,
including real authenticated FastAPI/control-gateway requests with
broker boundaries mocked or login forbidden. All Python source compiled; actual
engine/control/scanner imports passed with socket connections forbidden, and
dashboard JavaScript syntax is checked separately. The 157-test backend release
also passed GitHub's desktop/mobile browser fixture at the commit linked above.
Six further installer regressions cover process ownership, malformed position
quantities, bounded startup waits and immediate auth/schema refusal. Inspect
GitHub workflow status at the actual release commit. The user-reported AWS
installation and broker-flat preflight are recorded above; no live order or
market-session validation is claimed.

```bash
python3 -m pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
python3 -m compileall -q kadima dashboard_live
```

The HTTP tests use real FastAPI routing with broker login forbidden. The existing
execution tests replace broker boundaries. Browser fixture: install Playwright
and its Chromium runtime, then run `node tests/test_celestial_dashboard.cjs`.
All browser requests are intercepted at `kadima.test`; no live order is sent.
