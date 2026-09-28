# GitHub deployment inventory — 27 September 2026

Verified against GitHub PR metadata, exact-head workflow runs and ancestry comparisons to PR20. AWS is not connected; GitHub checks are not evidence of installation or live fills.

## Manual holding confirmed — 28 September 2026, 14:50 IST

The operator confirmed the replacement `NIFTY29SEP2622800PE`, NFO CARRYFORWARD,
token73896, quantity65 is manually managed. The 14:48:21 IST broker observation
showed zero pending orders, one nonzero position and historical BANKEX absent.
The startup helper now admits only that exact holding or an empty position book;
post-start checks require it remain external and not automatically managed.
The former CE exception is retained solely in immutable historical repair
validation. No broker order, runtime source or historical ledger is changed.
Engine startup, peripheral startup, live health and activation remain pending
until AWS execution is confirmed. Entries remain paused during recovery.

## Latest supplied recovery result — 28 September 2026

The recovery command at `8e04eadb4ea7d74d98586b205b9b3820743616ff` passed the
dependency and recovery-record gates, then blocked at initial fresh broker
admission: `fresh_broker_readiness_required`, `state=blocked`,
`activation=false`, `start_requested=[]`. It did not claim a recovery attempt or
request a service start. Its generic message does not identify an order,
position or broker API failure. The helper now retains the validated, bounded
broker receipt in this failure report. The updated recovery command can check
again and continue the same authorized paused start if all gates pass.
Engine/peripheral runtime verification and activation remain pending; the
historical repair remains committed.

## Earlier supplied result — 28 September 2026, 13:16 IST

The operator's screenshot shows the corrected startup failed at
`engine_dependencies_changed` after retaining the paused environment override.
`start_command_invoked=false` and `start_requested=[]`: this attempt did not
start the engine. Historical repair remains committed. Dependency comparison is
corrected to use relation sets, with a separate one-attempt recovery path that
requires the exact recorded before-start failure and current reviewed dependency
sets. The recovery is prepared but has not run on AWS. Dashboard, scanner, macro,
runtime health and trading activation remain pending.

## Earlier host observation — 28 September 2026, 09:29 IST

The engine remains inactive with PID 0. Paused startup stopped before mutation
because the guard omitted the host's exact root mount dependency `-.mount`.
The host reports `Requires=sysinit.target system.slice -.mount` and
`RequiresMountsFor=/home/ubuntu/kadima-py`; all other dependency groups are empty
except `Conflicts=shutdown.target`. The startup guard now admits that exact
root mount only under `Requires`. Historical repair remains committed; runtime
startup and activation remain pending.

## Earlier operator update — 28 September 2026, 08:58 IST

The operator reports `Historical ledger repair committed; all services remain
stopped` from the pinned repair command. The reviewed expired BANKEX ledger case
is therefore reported repaired; the startup command independently verifies its
durable status, archive and exact database outcomes. The repair command requires
zero pending broker orders before committing. A new broker observation is still
required before startup.

The engine-only paused startup command is prepared in
[REPAIRED_PAUSED_START.md](../momentum_recovery/REPAIRED_PAUSED_START.md).
Engine API health, remaining service startup and trading activation are pending.

## Earlier operator update — 28 September 2026, 08:34 IST

The operator reports cancelling the outstanding broker order. Fresh complete
broker books must confirm zero pending orders; cancellation is not yet verified
from AWS output. The guarded stopped repair is prepared in
[STOPPED_HISTORY_REPAIR.md](../momentum_recovery/STOPPED_HISTORY_REPAIR.md).
It rechecks the broker, backs up SQLite and reconciles only the reviewed expired
BANKEX case while preserving the manual NIFTY holding and paused controls.
It has not yet run on AWS; service startup and runtime verification remain pending.

## Latest host observation — 28 September 2026, 08:24 IST

The operator completed the virtual-environment permission correction: Ubuntu
ownership is unchanged, mode changed from 0664 to 0644, and config contents were
verified unchanged. Readiness then verified the installed PR24/UI sources,
effective service context, account binding, exact historical BANKEX case and
persisted paused controls. All five services passed the final stopped checks.
The reviewed watchdog cron hold was verified without another crontab write.

Both broker books were read successfully. The known operator-managed NIFTY
carryforward (qty 130) was accepted, and historical BANKEX exposure was absent.
However, **one nonterminal broker order remains** (`order_rows=1`,
`pending_orders=1`, `readiness=false`). Its identity was not printed by the
initial reader; the follow-up adds bounded order details. This is a broker-book
observation distinct from the historical unresolved local SELL intent. Their
relationship is not established. No broker order was changed or cancelled.
Historical ledger repair, restart, runtime health and activation remain pending.

**Earlier 01:00 IST follow-up:** the operator ran readiness commit
`08a0612ebe896ab2f055e515c86bf99db5935d9a`. All five units were observed inactive;
service-property parsing blocked before broker access. No ledger repair,
restart or activation occurred. The check found one direct Ubuntu cron entry
that can start the EOD watchdog despite its currently stopped state. A corrected
reader and exact, backed-up hold of that reviewed cron line are prepared; host
execution and complete broker-book observation remain pending. Installed source
status below is unchanged.

**00:34 IST source installation:** the operator supplied successful output from maintenance commit
`87497b9a68044e749b517255c356e12ea4961ae3`. The exact final PR24 source release
`8837cd6dc04e16123e94d8b78a3a716f83ed022c` (including PR20) installed 21 backend
and composed UI files. All five services were left stopped: engine, universe
scanner, dashboard, macro-watch and EOD watchdog. The earlier PR19 source update
had installed successfully at 00:13 IST. These are operator-provided host results.

No broker order, historical repair, restart or activation was performed by either
source installer. PR24 runtime/API/worker health remains unverified while stopped.
The operator manages the manual NIFTY carry-forward holding externally.

| Component | Current status |
|---|---|
| PR19 momentum / missed-move recovery | Source installed; startup and activation pending |
| PR20 trade audit, persistent Gamma plans, filters/colours/CSV | Source installed; runtime verification pending |
| PR20 daily price references and minute history | Source installed; new capture pending startup |
| PR24 continuous trade learning / forecast and strike review | Source installed; learner startup/heartbeat pending |
| Custom dashboard including new panels | Source installed; dashboard service stopped |
| Historical BANKEX ledger repair | Operator reports committed at 08:58 IST; durable archive rechecked by startup |
| Service startup and API/data checks | Pending |
| Trading activation / automatic session deployment | Pending; no deployment timer was armed by maintenance |
| PR7 separate CAS engine | Not deployed; integration pending |
| PR17 two-lot / ten-point index scalper | Not deployed; integration pending |
| PR18 dynamic S/R, Fibonacci, pivot and CAS stop adapter | Not deployed; production bindings/calibration incomplete |
| PR16 memory protection | Branch not deployed; existing swap is present |
| PR14/15 standalone Move Capture | Not separately deployed; PR19 integrated momentum path is installed |
| PR5/9 complete FETCH/MCX and live-input branches | Full integration unverified/pending; existing UI customizations preserved |
| PR4 general updates-anytime installer | Separate branch pending; current stopped-service maintenance works |
| PR23 audit RAG | Not deployed; previously deferred |

Lottery, Shock latency redesign, CAS PeakLock, advance T-1/intraday signals,
NIFTY one-minute Supertrend selling, and email/WhatsApp audit delivery are not
established as deployment-ready. Their details remain below. Existing base
lineage PRs are not counted as fresh pending installs merely because they remain
open in GitHub. Open PR heads were checked again at 00:40 IST and are unchanged.

The installed UI preserves custom news, timed forecasts, score probability and
stock controls. Authentication/write handlers remain preserved. Source hashes:
gateway `f45a083d859d286d09cf251f34b9e15c096227ab`, page
`3816a160f46420aaf28cb7f8de8579fdd3cf31e5`.

The next procedure is [STOPPED_READINESS.md](../momentum_recovery/STOPPED_READINESS.md).
It reads stopped service configuration, the exact private historical case and
complete broker books without repairing, starting services or enabling trading.
The known manual holding can be observed without requiring account flatness.

Startup review found that Auto Trade pause does not disable reconciliation or
all exit/cancel paths. The standalone EOD watchdog has a direct broker-order
path and must remain stopped pending trigger/configuration review. Do not start
all five services together or force engine startup solely for a learning heartbeat.
The old active-process repair guards remain unchanged; they are not bypassed.

## Operator observation — 27 September 2026, 23:18:26 IST

The operator ran PR22's entry-pause command and supplied its output. This is
operator-provided host evidence, not a direct AWS session by this reviewer.

| Item | Observed state |
|---|---|
| Auto Trade / Stock Live | Both false; stock revision 14; kill false |
| Engine / scanner / dashboard | All active; engine kept running |
| Session-auto timer | Disabled and inactive |
| 28 September deployment service/timer | Not found; no 09:15 attempt is armed by these units |
| API position count | Zero; broker flatness explicitly unverified |
| Runtime deployment | None performed by the preparation command |

The reported `run.py`, stock-fast, stock execution, stock API and news collector
hashes equal the five exact predecessor pins in PR19's focused installer.
The executor is the preserved MCX/session/portfolio composition
`7cfe6cf21a520ba967d6a672b2f54a99bbe81321`; engine, socket feed and FIFO limiter
also match their reviewed hashes. Full dependency/config/process admission must
still run on the host; this static comparison does not approve every host file.

**Earlier UI blocker (resolved by source composition):** gateway `059f182fce93c71269e92501cc00af4f710f83ec` and dashboard
`4c9d221436b7675eee5fda80d1546311273f6832` do not match the reviewed plain/stock
panel versions or the PR20/PR24 predecessor manifests. GitHub blob lookup also
did not resolve these installed variants. Their actual source must be reviewed
and preserved before composing the UI update. The manual runner now refuses an
unknown UI before any historical repair or source installation. The read-only
broker readiness command optionally exports exactly these two files privately.

Two newer GitHub requests were found after the original inventory:

| PR | Current source / verification | Deployment treatment |
|---|---|---|
| [#23](https://github.com/manchalakusumakar-spec/kadima/pull/23) | Standalone audit RAG, `9231e59ea7788c9a691f725cde6c3b152ff397ce`; PR reports 12 offline tests | Separate additive tooling; dashboard/plugin unwired, runtime/model evaluation pending; not included in the manual command |
| [#24](https://github.com/manchalakusumakar-spec/kadima/pull/24) | Continuous trade learning, `8837cd6dc04e16123e94d8b78a3a716f83ed022c`; exact-head CI 36319864991 passed | Stacked on PR20; advisory evidence/forecast/strike review, no automatic rule changes. Source installed in the 00:34 operator result; runtime/heartbeat pending |

No reboot, repair, runtime source update, broker action or activation was performed
by the pause command. The repair-and-restart deployment still needs current broker books,
historical ledger admission, matched sources and the normal post-install checks.

## Previous after-flat deployment scope (not the current stopped workflow)

The manual command attempts the existing reviewed historical repair, then PR19 momentum recovery, then PR20 trade audit / price history / dashboard. It preserves the installed MCX/session/portfolio executor and leaves global Auto Trade and Stock Live paused. It does not activate a strategy or merge PRs.

The repair remains evidence-specific and requires the real account to have zero nonzero broker positions and no pending orders. Closing pending orders alone is insufficient. An account-bound historical record must still be unresolved exactly as reviewed. The private evidence file is not committed.

## All existing pull requests

| PR | Scope | Head | GitHub CI | Relationship / deployment treatment |
|---|---|---|---|---|
| [#1](https://github.com/manchalakusumakar-spec/kadima/pull/1) | Review dashboard release: partial-fill protection, Shield 65 and complete installer | `e1d42a175b2e3bf29886b16474fa79f57aaedecd` | No returned PR-triggered run | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#2](https://github.com/manchalakusumakar-spec/kadima/pull/2) | Draft: Celestial all-root stock ladders, controls and release tooling | `b986b3903019c6619c230ea18033db27b9c0f4b0` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#3](https://github.com/manchalakusumakar-spec/kadima/pull/3) | Unify Shield 65 confirmation and trace Angel One dispatch | `b256364525979113872ecfc9c5acf6ec6a3d9f95` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#4](https://github.com/manchalakusumakar-spec/kadima/pull/4) | Allow KADIMA server updates 24/7 with exposure checks | `c98d2111f46b1b144da4ac95f86b00d79370c899` | success | Separate/diverged branch; not composed into this deployment |
| [#5](https://github.com/manchalakusumakar-spec/kadima/pull/5) | Fix FETCH stock coverage and timestamps; add MCX market and Gamma Blast watch | `69bb0e7706cd5e834bca9f3db7629bc7e715df90` | success | Separate/diverged branch; not composed into this deployment |
| [#6](https://github.com/manchalakusumakar-spec/kadima/pull/6) | Add experimental fast-momentum lane and execution diagnostics | `b506c918583df5842d8569a450944c5b98e5211a` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#7](https://github.com/manchalakusumakar-spec/kadima/pull/7) | Add separate CAS premium-awakening engine and staged execution | `5759aa7177becd4db323b98321077b868fd32c35` | success | Separate/diverged branch; not composed into this deployment |
| [#8](https://github.com/manchalakusumakar-spec/kadima/pull/8) | Repair AWS execution diagnostics and add guarded activation | `bde4c2952b54996b6f3be9a891df3167a00f20e7` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#9](https://github.com/manchalakusumakar-spec/kadima/pull/9) | Live input freshness, timed index projections and score-based probability | `e1c82b2cbe70aab41f765d80ed3e440bbd213fb8` | success | Separate/diverged branch; not composed into this deployment |
| [#10](https://github.com/manchalakusumakar-spec/kadima/pull/10) | Stock fast momentum: guarded live execution, 5/10/15-minute policies and dashboard controls | `7529526deb7dd8bc46880fc2fd4d19a59c073e20` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#11](https://github.com/manchalakusumakar-spec/kadima/pull/11) | Gap-and-Go: paper-only state machine and replay harness | `f4b5ce7f3c6b72f2e2ad370b7f8d74152fe97435` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#12](https://github.com/manchalakusumakar-spec/kadima/pull/12) | Gap-and-Go: guarded live execution through shared KADIMA executor | `40b8f459a0b8e5874554b277a92bbc7492e0eefe` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#13](https://github.com/manchalakusumakar-spec/kadima/pull/13) | MCX: guarded one-lot live commodity options lane | `9a0726a25bf6e0cf85ca65ef4e2bb5cd6d4012a0` | success | Already in PR20 ancestry; preserve/review installed lineage, do not reinstall the old PR |
| [#14](https://github.com/manchalakusumakar-spec/kadima/pull/14) | Move Capture: isolated spot-first impulse shadow engine | `b4ed72e9cc168b269578129c271af0ba27d9d5ad` | success | Separate/diverged branch; not composed into this deployment. PR19 provides a separate in-process momentum path; do not start a second feed blindly |
| [#15](https://github.com/manchalakusumakar-spec/kadima/pull/15) | Move Capture: guarded auto-trade option confirmation path | `77f393cfd20c36ddef236dcec14f8a27279c7f8b` | success | Separate/diverged branch; not composed into this deployment. PR19 provides a separate in-process momentum path; do not start a second feed blindly |
| [#16](https://github.com/manchalakusumakar-spec/kadima/pull/16) | Memory admission protection, lower refresh peaks, and guarded 1 GiB swap installer | `26ac9ec16044e1275eeaa83b959d981c5fff380d` | success | Separate/diverged branch; not composed into this deployment. GitHub reports base conflicts; existing host swap was observed working |
| [#17](https://github.com/manchalakusumakar-spec/kadima/pull/17) | Add 2-lot index scalper with 10-point premium target | `d065a9732656138adab5f7f71afbe7f589d0c6f4` | success | Separate/diverged branch; not composed into this deployment |
| [#18](https://github.com/manchalakusumakar-spec/kadima/pull/18) | Draft dynamic stops: structure zones, manual CAS and durable execution adapter | `00d6bc5b26dc1730fa2dc6f2b2a4c0d2eed797cb` | success | Separate/diverged branch; not composed into this deployment. Execution adapter remains disabled and production bindings/calibration are incomplete |
| [#19](https://github.com/manchalakusumakar-spec/kadima/pull/19) | Recover missed stock momentum signals and add guarded spot-led auto-entry | `bd294a7048749258fbaa2cc786f20a441a2a8073` | success | Source installed on AWS; services stopped, activation pending |
| [#20](https://github.com/manchalakusumakar-spec/kadima/pull/20) | Persist trade audit and daily price history for Gamma and momentum | `d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce` | success | Source installed through final PR24 at 00:34 IST; runtime checks pending |
| [#21](https://github.com/manchalakusumakar-spec/kadima/pull/21) | Guard historical reconciliation and schedule one paused deployment attempt | `7e6424ac79dacc791e05e85e65c891e193b81655` | success | Pinned repair component; existing scheduled runner is not armed by this manual command |

## Corrections to the earlier chat inventory

- PR20 already implements persistent >=60 Gamma audit capture, first plans, actual fill comparisons, first observed T1/T2/stop outcomes, requested colors, filters and CSV export. Source installation is now confirmed by the 00:34 operator output; runtime collection remains unverified. It also provides daily references and minute history; coverage is observed feeds/selected ladders, not every listed option.
- MCX is PR13; PR11 is the Gap paper state machine. PR14/15 are the standalone Move Capture line. PR19 is the separate momentum-recovery integration chosen for this host lineage.
- PR1/2/3/6/8/10/11/12/13 are already ancestors of PR20. Ancestry does not establish their current AWS activation or every installed file; the guarded installers still require the exact reviewed predecessor/overlays.
- PR4/5/7/9/14/15/16/17/18 diverge from PR20. A complete replacement of AWS with any branch would discard or conflict with other work. They require explicit composition; this command does not claim to deploy them.
- The new PeakLock design differs from PR18's manual CAS no-price-stop policy. Those policies must be reconciled before activating a common exit path.

## Requests not established as deployable by this review

Lottery (score 40 / INR 3,000), Shock latency/threshold redesign, CAS PeakLock, advance T-1/intraday move framework, NIFTY one-minute Supertrend selling, and automated email/WhatsApp audit delivery are not made deployable by this maintenance command. Existing PR20 audit views do not implement message delivery. No new trading thresholds are enabled here.

## Earlier after-flat host checks (retained; not bypassed by source maintenance)

- Global Auto Trade paused; Stock Live off; no kill reset; broker positions and pending orders verified by the pinned account-bound repair.
- Existing reviewed historical evidence still present and unchanged; ordinary weekday API restoration must have occurred. The manual command does not select the special weekend-unrestored mode.
- Current source/overlays, process identity and configuration match the reviewed installers. Unknown edits stop the rollout.
- Both session-auto and the prior scheduled-deployment workers must be inactive. After the initial broker preflight, their timers are disabled and remain disabled; running workers are never killed.
- No repeated attempts after repair/source mutation begins. Inspect the durable stage status if a later stage fails.
- Final services, authenticated audit/price endpoints and local ledger checks must pass. Actual quote capture, resource usage and subsequent live activation remain separate host verification.
