# KADIMA synchronisation report — return to ChatGPT

Prepared 11 Oct 2026 (IST) by Claude (session `session_012BA2DRLtbdqZpVrKKuSEkd`) in response to
*KADIMA — Claude synchronisation handoff* (10 Oct 2026). Scope followed: **intraday trend only**;
Gamma, CAS, Bull/Bear Shield and the 5/10/15-minute fast scalping lane unchanged.

**Selected integration branch / full commit:** `claude/intraday-trend-integration` @
`7c67c672b5d485af336893433ffe52ba2cf893eb` (source), draft PR
[#39](https://github.com/manchalakusumakar-spec/kadima/pull/39) into `fix/parallel-orders-retry`.
This report is added in a later documentation-only commit on the same branch; it changes no source.

Status words used below: *proposed* → *integrated* (in the branch) → *tested* (offline) →
*committed* → *deployed* → *activated*. This work reaches **committed** only.

## Source identities
| Item | Repository / path | Branch | Full commit or blob | Evidence / status |
|---|---|---|---|---|
| Supplied review baseline | manchalakusumakar-spec/kadima | `main` (historical review only) | `c53365deddcca84ffee4484ef8246c14dc6cc450` | Not a deployment attestation. Is the merge-base of all later work below. |
| Claude current development (before this task) | manchalakusumakar-spec/kadima | `hourly-forecast` | `acc54c82cf9be5ecbba48522add566cd969a15e6` | Descends from c53365d (13 commits: hourly forecast, preflight checker, manual-stop protection, wider chain data). |
| Newest lineage head found | manchalakusumakar-spec/kadima | `fix/parallel-orders-retry` | `de33ef36a3b8e1da64f5a24fe64e1b08f0a0de9b` | Committed by the trader 10 Oct 18:08 UTC from another Claude session; direct child of acc54c8. Parallel entries, delivery-failure retry, unknown-order timeout, previous-day row expiry. |
| **Integration baseline (selected)** | manchalakusumakar-spec/kadima | `fix/parallel-orders-retry` | `de33ef36a3b8e1da64f5a24fe64e1b08f0a0de9b` | Newest head of the single lineage; its shared execution changes must not be bypassed by a competing branch. |
| Integration head | manchalakusumakar-spec/kadima | `claude/intraday-trend-integration` | `7c67c672b5d485af336893433ffe52ba2cf893eb` | Draft PR #39. |
| Effective AWS runtime | EC2 `~/kadima-py` (KADIMA-BRIDGE) | Do not infer | — | **UNVERIFIED.** No read-only server access in this session. Prior notes say the live checkout runs a local branch with uncommitted overlays and anchor-installed patches, so no GitHub commit (including de33ef3) is asserted to be deployed. |

`kadima/fast_momentum.py` is byte-identical in c53365d, acc54c8, de33ef3 and the integration head:
Git blob `7eb165d7ae89be70133b0a8a4d4efeed964a7d70` (the handoff's reviewed original).

**Dirty/uncommitted source and preservation plan.** Before this task my workspace held two untracked
draft files on a local `autotrade-unblock` branch (based on acc54c8): `kadima/lead_router.py` and
`tools/install_autotrade_unblock.py`. They were never applied to any module or server. They touch
Gamma/Top 20 rules and CAS routing (out of this scope) and their ledger repair is superseded by
de33ef3. Preserved unmodified as `wip/autotrade-unblock-deferred` @
`521a60c7e6830de64a88ca5fe7c6b304e46ebecf` (labelled do-not-merge). No dirty tracked paths remained.

**Other existing developments that must remain intact:** everything in de33ef3 (parallel orders,
ledger retry/timeouts, hourly forecast + panel + nightly tools, preflight checker, manual-stop
protection, Celestial, Gamma, Shield, fast, gap-and-go, MCX, stock lanes); the ~50 other `codex/*`,
`cas-*`, `deploy/*` branches are untouched (several are parallel lineages, e.g. CAS executor modules
exist only on `cas-*` / `codex/cas-*` / `host/live-cas-source-20260930`, not in this lineage).

## Reconciliation matrix
| Item | Current implementation evidence | Classification | Resolution / files |
|---|---|---|---|
| Independent intraday trend lane | No `intraday_trend` identity in any of the 52 remote branches that existed before this task. Existing intraday-type lanes: Bull/Bear Shield (indicator confluence + OI, CONFIRMED only), Gap-and-Go (opening gap, CE only). `move_capture` (codex/move-capture-*) is a ~16-min spot impulse lane that dispatches as `fast_momentum` — scalping, not intraday. | COMPATIBLE_TO_PORT (no lane existed; new lane built on existing streams and the one shared order writer) | **Integrated, tested, committed** in 7c67c67: `kadima/intraday_trend.py` (new), hooks in `engine.py`, gated special context in `auto_executor.py`. Observation-only unless `KADIMA_INTRADAY_TREND_LIVE=1`. |
| Fast-momentum selection patch | Baseline file = reviewed original blob 7eb165d; patch would apply cleanly. Handoff suite re-run here in scratch (not in repo): our untouched file fails 12/20 (gap still present); the handoff's patched text passes 20/20, its SHA-256 `763060ee…` matches. | DEFERRED_REFERENCE_ONLY | Not applied. Its principle (try ranked contracts in order instead of rejecting on the first) is reused **inside the intraday lane only**, where every ranked contract gets a recorded reason. |
| Decision capture and original-horizon reporting | Lane: append-only `intraday_trend_decisions` rows (SETUP_OBSERVED / DISPATCH / SETUP_EXPIRED / SETUP_FAILED) with policy id `intraday_trend_v1:<hash>`, module SHA-256, ranked-contract evidence, entry/stop/T1/T2 and the explicit exit policy; immutable episode → signal ids. `kadima-audit/tools/analyze.py` horizon-blind “wins” not examined or changed here. | Lane capture: COMPATIBLE_TO_PORT (done). Analyzer fix: DEFERRED_OUT_OF_SCOPE | Lane: integrated. Analyzer: separate repo and separate task; audit commit `72592595…` not re-verified in this session. |
| Intraday fresh-setup re-entry | Shared duplicate protection (`execution_ledger.seen`, de33ef3 semantics) unchanged. Gamma episodes unchanged. | COMPATIBLE_TO_PORT (lane-only policy) | Integrated: new episode per closed setup bar; requires no open/pending intraday_trend position on that underlying, setup bar after the last confirmed exit, ≤3 episodes/underlying/day, fresh data, executor risk headroom. |
| Celestial intraday display | Celestial dashboard HTML is served from EC2 `/var/www/html`; the version in production is not in this lineage. | API: COMPATIBLE_TO_PORT (done). HTML panel: BLOCKED_MISSING_EVIDENCE | Read-only `GET /api/kadima/intraday-trend` (rows, decisions, open intraday positions; Gamma/CAS rows not included or relabelled). Panel deferred until the live dashboard source is identified. |
| Same-strike engine priority (trader, 11 Oct) | Rule: before 15:15 Gamma > Scalping > Intraday; CAS leads from 15:15 (intraday has no new setups after 14:30 and exits by 15:10, so it never meets CAS). Trader chose: intraday steers clear (option D); a strike intraday already holds stays with intraday ("strike is active, profit is captured"); 2 positions per index kept. | Intraday part: COMPATIBLE_TO_PORT (done). Gamma-over-Scalping: DEFERRED_OUT_OF_SCOPE (changes the scalping lane). | Integrated in `intraday_trend.strike_claims()`: intraday skips any contract held by any engine or currently selected by Gamma (eligible Celestial row) or Scalping (eligible fast row), records `held_by_X` / `strike_claimed_by_X`, takes its next-ranked contract; re-checked right before submission; unreadable claims block intraday. Read-only toward other engines. |
| Gamma / CAS preservation | No change to `gamma_blast_scanner.py`, `celestial*.py`, `trade_policy.py`, `shield_confirmation.py`, `fast_momentum.py`, `execution_ledger.py`, `portfolio_capacity.py`, `capital_guard.py`, `risk_budget.py`, `market_session_gate.py`, `killswitch.py`, `cas_blast_scanner.py`. Shared `auto_executor.py` edits are all gated on `trend is None` / kind `intraday_trend`. | ALREADY_PRESENT (preserved; parity tested) | 23-scenario dispatch/exit parity byte-identical vs de33ef3 (see Verification). CAS executor modules are not in this lineage; live CAS **UNVERIFIED**. |
| Parallel orders / ledger deadlock fix | Present in de33ef3 (delivery-failure retry, 60 s unknown-order release, previous-day expiry). | ALREADY_PRESENT | Used as baseline; my earlier draft ledger fix superseded. |
| Earlier Claude proposals from 10 Oct review (news never gates; Top 20 eligible-first slot; threshold 60; engine-priority router; 15:15 INTRADAY cutoff for all lanes; manual-position scoping) | Draft only, on `wip/autotrade-unblock-deferred`. | CONFLICT with this task's scope (touch Gamma / CAS / shared admission) → DEFERRED_OUT_OF_SCOPE | Not integrated. Need their own approval and parity review. |
| Legacy `_score_gamma` episode re-use (handoff §2B) | Unchanged. | DEFERRED_OUT_OF_SCOPE | Diagnostic context only; no Gamma change. |

## Proposed versus completed changes
**Exact files changed and why** (`git diff --numstat de33ef3 7c67c67`):

| File | +/− | Base blob → new blob | SHA-256 (new) | Why |
|---|---|---|---|---|
| `kadima/intraday_trend.py` | +532 | new → `3ba50343b86386885dc62f4d522753def38bd530` | `8e8fb53873fdb7b13a46081c19a9b17e7b8a62b4e397ca29c44c57ebd29f8a46` | The lane: setups, admission, policy, records, gates. Broker-free. |
| `kadima/auto_executor.py` | +30 −13 | `21f1257fbcd94bc62c5f45b288e34eb872d921e0` → `52e85dd7fd6f1372132a90fc1a9bb37539e9fb23` | `ca8f33597b7d41736ff4741ed64a63766175d6bafa485f4eaf587e2faac3b770` | Kind `intraday_trend` handled like fast/gap special contexts (own context check, contract filter, 1 lot, entry ≤ +0.5 %, pre-submission revalidation); one `check_exits` `elif` for its holding policy. 13 removed lines are the same conditions re-written with `and trend is None`. |
| `kadima/engine.py` | +44 | `0c5411bd9a89fac77d082dc53f8d6e31f5a85298` → `b16056cc36248260256cc247b6ec2e0cdd5ba34e` | `0ced83e61ab0ccb20a3fb37ae91154455f0b4d2aaeea6738272ebe600202f8b5` | Observe on the existing 5-min close (after Shield); admit on the existing chain poll (after Gamma/Celestial/fast). Exceptions contained and audited. 4 main indices only. |
| `kadima/control_api.py` | +21 | `96e1638e8eb9005b65e16b2b6f61f414eb45b635` → `91031ec29379be1732be5f1e9a5b4a2ae3cb8266` | `d071b46e3379e747c35959841a40cc40a22989cca0bd446f9eb88fa965d9ba50` | Read-only display route. |
| `tools/test_intraday_trend.py` | +553 | new → `dfb077f95f75c3587bade07d95cc7478df7d5eff` | `e01765c7366189b7a5a409430a29d49902fb2bf18a4291d529e28c840e6186ec` | 67 offline checks + parity driver. |
| `tools/parity_scenarios.py` | +318 | new → `7d5a8732910b151baf2ccb951d95f4683fc477a6` | `6e659aabdb49764d2e09205ed6caed21023ce1659822909eb87a65a2ff5d1000` | Deterministic broker-mocked scenarios run against any source tree. |

**Intraday policy as implemented (hypotheses, uncalibrated):**
- Setups use closed 5-min index bars only, and need the existing 3-min trend snapshot aligned (VWAP / EMA33 vs EMA83 / RSI 50) and under 240 s old.
  - `opening_continuation` (09:30–10:30): close beyond the 09:15–09:30 range.
  - `pullback_recovery` (09:45–14:30): a 3-bar pullback touching the 5-min EMA9, depth ≤ 2× the average bar range, then a close beyond the prior bar.
  - `afternoon_breakout` (13:00–14:30): close beyond a 12:00-onward range of width ≤ 3× the average bar range.
- Every setup needs: close beyond the level by ≤ 1× the average bar range; spot still beyond the level at the decision; no new setups at or after 14:30.
- Admission happens on the next fresh chain poll within the bar. It needs the chain and spot under 30 s old, spread ≤ 1.5 %, and a structure premium stop below the ask that is ≤ 35 % of entry. An unused setup expires at the next bar close with an outcome record.
- Holding:
  - Initial stop: the structure stop (never widened).
  - Trail: the shared ratchet.
  - Exits: no-progress (peak below +5 % at 45 min) and a time exit at 15:10. No carry into the next session.
  - Size: 1 lot.

**Protected paths / shared behaviour checks:** listed in the Gamma/CAS row above, plus the parity
and mutation results below. Shared safeguards are exercised for the new kind: kill switch, unfinished
reconciliation, daily-loss limit, session permit, duplicate signal, observation-only flag, setup
changed before submission.

**Changes deliberately deferred:** the fast-momentum patch, the audit analyzer horizon fix, the
Celestial HTML panel, and all 10 Oct Gamma/CAS/shared-admission proposals (see matrix).

## Verification
All runs: Python 3.13 venv with `requirements.txt` (smartapi-python 1.5.5, fastapi 0.143, …) plus
`httpx2` (test client needed by `test_hourly_forecast_live.py`, not in requirements). Each run used a
fresh temporary `KADIMA_DATA_DIR`. No broker login or order, and no AWS access. The new tests block
socket connects.

| Exact command / workflow and commit | Result | Scope | Skips / limitations |
|---|---|---|---|
| `python tools/test_intraday_trend.py` @ 7c67c67 | **67/67 pass** | Setup detection (CE/PE symmetry, failed breakout, extension, alignment, stale/unclosed/missing bars, windows), contract evidence, freshness, re-entry, episodes, exit policy, broker-mocked executor entry + exit, engine hook isolation, parity | Fixture data only |
| Parity inside the above: `python tools/parity_scenarios.py --root <git archive de33ef3>` vs `--root .` | **Byte-identical**, 23 scenarios | Gamma legacy CE/PE accept, misaligned, below threshold, duplicate, kill switch; Celestial candidate accept / no-candidate reject; Shield no-evidence; fast and gap accept + reject; forecast manual buy; exits: stop, trail, EOD, 15:15 hold, fast timeout, Shield/CAS-kind/MCX-kind holding | Celestial `top20`/config stubbed per scenario; CAS executor modules absent from lineage |
| Mutation check A (scratch copy): `LIMIT_ORDER_BUFFER_PCT` 2.5→3.0 | Parity run **detects** (9 scenarios differ) | Proves sensitivity | — |
| Mutation check B (scratch copy): `GAMMA_MIN_SCORE` 70→55 | Parity run **detects** (below-threshold Gamma now submits) | Proves Gamma path covered | — |
| Existing suites @ de33ef3 vs @ 7c67c67: `test_audit_fixes`, `test_forecast_manual_buy`, `test_hourly_forecast`, `test_hourly_forecast_live`, `test_offline`, `test_parallel_orders` | 35 / 17 / 41 / 42 / 15 / 36 pass on **both**; 0 failures | Unchanged baseline behaviour | `test_hourly_forecast_live` needs `httpx2` |
| Handoff suite `tests/test_fast_candidate_selection.py` (scratch, outside repo) | Our `fast_momentum.py`: 12 fail / 8 pass (matches handoff baseline). Handoff patched text: 20/20 | Confirms patch not applied and gap unchanged | Text extracted from the handoff file |
| Offline microbenchmark | `detect()` ≈ 0.08 ms/call; `prepare()` ≈ 0.7 ms/call (10 ranked contracts incl. IV solves); decision payload ≈ 1.6 kB | Lane cost only | Not the EC2 host |

- **Gamma/CAS behaviour parity:** shown for the shared executor dispatch and exit paths in this lineage (above). CAS executor parity cannot be shown here: `cas_executor.py`, `cas_engine.py` and `cas_orders.py` are not in this lineage, and the live CAS runtime is UNVERIFIED. The lane does not import or call CAS code.
- **Full executor / broker-mocked integration:** real `AutoExecutor` with a stubbed broker transport, LTP, funds and lot size. The intraday path reaches the shared ledger as exactly one 1-lot BUY, and no-progress exits send one SELL. Real `SmartConnect` is not exercised.
- **Market replay / costs / holding policy:** **NOT DONE — BLOCKED_MISSING_EVIDENCE.** No decision-time NIFTY 1/5-min candles or option bid/ask samples were available in this session. No claim of captured moves, win rate or profitability is made. Day-end prices were not used.
- **Resource measurements:** EC2 host CPU/RAM/disk **UNMEASURED**. The lane adds no thread, polling or subscription. Worst-case decision storage is ≈ 1 MB/day with 30-day pruning; actual writes happen only on setups and outcomes.
- **Missing dependencies or source:** live EC2 source and hashes; the production Celestial dashboard HTML; CAS executor modules for this lineage; `kadima-audit` analyzer (not reviewed).

## Publication and deployment status
- Draft PR / exact patch reference: [PR #39](https://github.com/manchalakusumakar-spec/kadima/pull/39) (draft), `fix/parallel-orders-retry` ← `claude/intraday-trend-integration`
- Full integration head and file SHA-256 hashes: `7c67c672b5d485af336893433ffe52ba2cf893eb`; hashes in the file table above
- Committed: **YES** (branch only)
- Merged: **NO**
- AWS deployment performed: **NO** for this task
- Live activation performed: **NO** for this task (`KADIMA_INTRADAY_TREND_LIVE` defaults off; not set anywhere)
- Broker orders placed: **NO** for this task
- Services restarted / configuration changed: **NO**

## Conflicts and open decisions for the trader
1. **Same strike / same index — resolved 11 Oct.** Same strike: before 15:15 Gamma > Scalping > Intraday, CAS from 15:15; intraday steers clear and keeps a strike it already holds. Different strikes on one index: the baseline limit of 2 (`MAX_OPEN_PER_UNDERLYING`) stays. Gamma-over-Scalping enforcement is a separate follow-up (scalping lane change).
2. **Shared budgets.** Intraday positions consume the same broker cash and ₹10,000 daily-loss headroom as Gamma and the other lanes.
3. **News.** The lane has no news gate, consistent with the trader's 10 Oct rule that news never blocks technical leads. Existing fast and gap lanes still require news coverage; that is unchanged here (deferred).
4. **Live opt-in.** Activation needs a separate explicit decision, after replay and shadow evidence.

## Remaining blockers and next release gate
**Evidence still required:**
- Read-only EC2 identity: per-file hashes of `~/kadima-py/kadima/*.py`, process entrypoint and service drop-ins, and whether de33ef3 is installed.
- Decision-time candles and option bid/ask for replay. EC2 `price_history.db` and the `candles` table are candidate sources.
- The live Celestial dashboard source.
- The trader's answers to conflicts 1–4.

**Single integration owner for the next change:** proposed — this lineage
(`fix/parallel-orders-retry` → PR #39), with one chat owning each shared-file change at a time.
The trader decides. Other chats should rebase on PR #39 rather than edit `auto_executor.py` in
parallel.

**Next exact read-only or offline step:** export (read-only) recent sessions' 5-min index candles and
option-chain samples from EC2, then run an offline chronological replay of `intraday_trend.detect` /
`Lane.prepare`. The replay must use decision-time inputs only and account for fees, slippage, the
45-min / 15:10 policy, losses and valid rejections, with calibration and validation periods kept
separate. Only then propose a shadow (observation-mode) install, which needs separate approval.
