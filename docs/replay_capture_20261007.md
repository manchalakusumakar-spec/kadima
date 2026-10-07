# Fast-trade replay capture — 7 October 2026 IST

**Source repair prepared; historical replay and profitability remain blocked.**
No AWS access, deployment, trading activation or broker order was performed.
Capture is opt-in (`KADIMA_REPLAY_CAPTURE=1`); its default is off. This document
is not an instruction to enable it on the current unidentified production host.

## Exact sources

| Role | Source |
| --- | --- |
| PR28 inspected/current patch parent | `263d52a7e8a825d08ae0b0e92ff744db5612d51f` |
| PR28 evidence commit | `b97b217a5a10828832ceae9e42b0acde73d7b273` |
| PR28 original baseline | `c53365deddcca84ffee4484ef8246c14dc6cc450` |
| PR20 audit inspected/export patch parent | `d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce` |
| Original CSV repository/commit | `manchalakusumakar-spec/kadima-audit` / `a045bb431a2ade6399a703d9e77d405dd0bec16b` |
| Original CSV Git blob | `e83aaa81755991840fdc4fbd39b20e51a6fe8f76` |
| Evidence copy Git blob | `92df0d5841843f987fe79f9bbf4d362d3421d754` |

The evidence CSV, notebook and evidence review have identical blob IDs at the
evidence commit and inspected PR28 head. Both PRs were draft and unmerged when
inspected. PR28's complete source tree has **no `kadima/trade_audit.py`** and
no PR20 recorder hooks. Do not treat those branches as a deployed composition.
`source_pins_20261007.json` records the inspected source blob IDs.

## Exactly where evidence is lost

| Boundary | Source-pinned finding | Repair / implication |
| --- | --- | --- |
| PR20 row identity → CSV | [`trade_audit._identity`, `record`, `CSV_COLUMNS`](https://github.com/manchalakusumakar-spec/kadima/blob/d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce/kadima/trade_audit.py): every saved plan has immutable `audit_id`; CSV exports `signal_id` but omits `audit_id`, episode, producer and contract exchange/token. | Minimal separate PR20 patch appends these existing fields. No new ID column or migration. Re-export can recover existing IDs **if the original database rows survive**. Do not synthesize IDs for old CSV rows. |
| Stock suggestion → execution link | [`stock_fast.plan`, `Worker.process`](https://github.com/manchalakusumakar-spec/kadima/blob/d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce/kadima/stock_fast.py): plan includes configured `holding_minutes`; `record` runs before `bind_policy`, then after binding with `signal_id`. | Blank execution signal IDs on unexecuted suggestions are expected. This source does not demonstrate a missing stock propagation hook. A deployed-source or database difference requires host evidence. |
| Index plan → horizon | [`fast_momentum.Lane.scan`, `scenarios`, `timeout_reason`](https://github.com/manchalakusumakar-spec/kadima/blob/263d52a7e8a825d08ae0b0e92ff744db5612d51f/kadima/fast_momentum.py): produces 5/10/15-minute projections but does not set `holding_minutes`. Five-minute no-progress protection is conditional, not a fixed maximum hold. | Capture projection horizon separately; maximum hold remains null. Setting it to 15 would invent a policy and alter audit outcome windows. |
| Candidate inputs → retained record | Both lanes drop `setup['bars']` from result. Index `self.rows[name]` replaces the last result; `FAST_CANDIDATE` logging is signature-gated. Stock history is a bounded memory cache; `stock_fast_snapshots` replaces per-root snapshots. PR20 `record` persists a narrow plan and deliberately skips SCALP rows without entry. | New decision event keeps exact 1m/5m input rows, timestamp and accepted/rejected result for every evaluated scan and revalidation, including pre-contract rejections. It does not broaden which roots get scanned. |
| Underlying DB → decision provenance | [`storage.upsert_candle`](https://github.com/manchalakusumakar-spec/kadima/blob/263d52a7e8a825d08ae0b0e92ff744db5612d51f/kadima/storage.py) retains candle state, not the immutable version consumed by each decision. CSV contains no candles. | Snapshot the actual rows read by the selector; do not perform a later history query or fabricate closed flags. Historical database presence remains unverified. |
| Option FULL response → retained OI data | [`OptionChain.accept_full_sample`](https://github.com/manchalakusumakar-spec/kadima/blob/263d52a7e8a825d08ae0b0e92ff744db5612d51f/kadima/option_chain.py) parses bid/ask, but the `insert_oi_snapshot` payload/schema omits both. Premium windows and `_forecast_sample` are transient. | Capture existing accepted ladders with bid/ask and receipt time. Also capture existing FULL held-position quotes via `celestial_exit.publish_quote`. No additional polls/subscriptions. |
| PR20 sampled price history → replay | [`price_history`](https://github.com/manchalakusumakar-spec/kadima/blob/d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce/kadima/price_history.py) coalesces sampled prices and minute extrema, not complete option bid/ask paths or exact decision candles. | Do not substitute sampled extrema for exchange OHLC or an executable tape. |
| Broker status → ledger/export | [`execution_ledger.apply_fill`](https://github.com/manchalakusumakar-spec/kadima/blob/263d52a7e8a825d08ae0b0e92ff744db5612d51f/kadima/execution_ledger.py) persists cumulative deltas, price/P&L and engine receipt times; `OrderStatus.raw` is transient. CSV aggregates fills and cannot establish missing BUYs. | Capture received status and committed delta with signal, intent, order and contract IDs. Optional allowlisted broker times/fill IDs/charge fields remain unverified observations. Missing charges stay null. |
| Runtime → version provenance | Existing audit events have no complete deployed source/config attestation. GitHub heads do not prove AWS source. | Record first-capture disk hashes and loaded Python code hashes, Python version, run ID and per-process sequence. Capture experimental constants, actual stock controls and actual live-gate/news/structure observations. Never dump credentials or arbitrary environment. |

Recomputed evidence: 17 rows, 13 suggestions, all 13 exported signal IDs blank,
eight maximum-hold fields blank. The five recorded 15-minute plans are stock
suggestions; the eight missing fields are index suggestions. That matches the
inspected producer behavior but **does not prove the deployed source**. No row
can be replayed against PR28 from this CSV alone. The four Manual records are
not relabelled auto-scalp trades or used as proof of profitability.

## Patch boundary

The PR28 patch adds one observational module and small hooks in six existing
files. It does not import PR20, merge branches, add trading tables, allocate
execution signal IDs for rejected suggestions, change submission/exit decisions,
or change experimental thresholds. Suggestion IDs use PR20's existing SCALP
`audit_id` key; each evaluation additionally has its own UUID. Events retain full
contract identity; consumers must reject conflicting identity for one ID rather
than join by ticker. The PR20 companion changes only CSV export columns and adds
a focused test file.

Existing `audit.log` provides append-only JSONL and fsync. Every new adapter
catches its own exceptions, counts failures and emits those counts on subsequent
successful events. Oversized events are rejected whole, not silently truncated.
Per-process sequence gaps and failure counters must invalidate affected replay
coverage. The existing sink is synchronous: enabling capture adds CPU, disk and
fsync latency. Host latency, volume and retention have **not** been measured;
default-off is preserved. The patch adds no background worker or unbounded
in-memory queue. A write stall is not the same as a caught write exception.

The first-capture source manifest is an observation, not a signed deployment
receipt. Loaded-code fingerprints are Python/path dependent, and later imported
modules or in-process monkey-patches are not automatically re-attested. Only the
allowlisted policy values and actual captured gate inputs are recorded, not every
possible host overlay or risk setting. Quote fields in a decision are observed
at capture time; concurrent chain mutation can prevent certifying a rank-time
snapshot. These limits are explicit replay blockers, not silently accepted
provenance. Source/config composition must be verified before enabling capture.

## Chronological replay release gate

Every decision reports `full_replay_ready=false`; it is evidence, not a validator.
To lift the profitability block, assemble independently frozen chronological
training and held-out days and verify all of the following:

1. Source/config/deployment identity and all required baseline/PR28 inputs match
   the decision, including raw closed candle chronology, rejected opportunities,
   controls and execution/risk settings. Exclude lookahead and ambiguous joins.
2. Sampled quote paths cover the specified horizon and same full contract.
   Current-ladder polling does not guarantee retention after a strike rolls out;
   held-position depth is available only when the existing FULL path runs.
   Missing or sparse paths cannot establish fills, stop/target order or filtered
   winners. Document slippage, liquidity and both-hit ambiguity assumptions.
3. Reconcile complete BUY/SELL trade-book fills, partial executions and charges
   against broker records/contract notes, including pre-adoption Manual BUYs.
   This patch does not fetch those unavailable records or invent missing fees.
4. Compare the frozen baseline and unchanged PR28 using identical capital/cost
   assumptions; report net returns, drawdown and filtered winners on held-out days.
   Do not tune or claim improvement using the October 5 outcome labels.

## Validation

- 15 new capture tests and 6 PR20 export tests pass with socket connections blocked.
- All 19 existing PR28 quality tests pass; both existing offline suites pass.
- Tests cover identity reuse, distinct episodes/evaluations, immutable snapshots,
  null horizons, pre/post signal linking, CSV recovery/formula safety, exact candle
  replay, rejected inputs, quote depth, partial/duplicate/terminal/regressing fill
  observations and real ledger accounting during capture I/O failure.
- Five integration files have identical executable ASTs after removing only the
  new observer calls/imports. Index candle queries retain their order, count,
  limits and arguments; existing filter tests exercise the equivalent local binding.
- `config`, `fast_entry_quality`, `trade_policy`, `auto_executor`, `angel_orders`,
  `risk_budget`, `exit_manager`, `capital_guard` and `execution_pricing` are byte
  identical to the PR28 pin. Compilation and diff-whitespace checks pass.

These are software regression results, not historical replay or net-profit evidence.
