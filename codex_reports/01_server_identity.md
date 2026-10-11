# Step 1 — read-only server identity and precise baseline comparison

## Exact live executor / ledger comparison with de33ef3 — latest read-only follow-up

Inspected at `2026-10-11T15:09:35.072270+00:00` by reading source only. No application module in `~/kadima-py` was executed or imported for this comparison. Baseline: `de33ef36a3b8e1da64f5a24fe64e1b08f0a0de9b`; decoded baseline blobs were verified. Complete source differences: [auto_executor.py.baseline.diff](/home/ubuntu/codex_handoff/codex_reports/auto_executor.py.baseline.diff), [execution_ledger.py.baseline.diff](/home/ubuntu/codex_handoff/codex_reports/execution_ledger.py.baseline.diff).

**One-position cap `min(1, MAX_CONCURRENT_POSITIONS)` in live auto_executor.py: NO.** Neither that expression nor any reference to `MAX_CONCURRENT_POSITIONS` is present in the live executor or ledger. The live capacity helper has no position-count cap. It can admit additional filled/open contracts subject to cash/risk and contract gates, but blocks NEW submissions while any unresolved ledger order exists. That pending-order gate can stop a second entry until reconciliation; it is not a permanent one-open-position cap.

**de33ef3 parallel-pending/reserved-capital and delivery-failure retry changes installed: NO.** Some earlier multi-position and durable-ledger behavior is present. The distinction is detailed below.

|File|Live SHA-256|Live Git blob|de33ef3 Git blob|
|---|---|---|---|
|auto_executor.py|`d89711002f29f3c56f96b26236b7458635f71c0ad63f7098f805981387a832d1`|`f86a5f369e4cb1c7cda4d48c6a1ed3f45101b039`|`21f1257fbcd94bc62c5f45b288e34eb872d921e0`|
|execution_ledger.py|`ce2a8aa20480625e4fb6e5c8c29478019b0f13861ab3f9001b7929c87b07a875`|`ffe700bca0c6e0ea523dd2ce34db2f19a0c0e5aa`|`eace209cfe4f6356914317c006d4b1b1be1e6545`|

### auto_executor.py — present in both

- Serialized shared writer: `serialized()` with `_execution_lock` as an RLock. The execution lock remains held across entry decision/submission; serialization of code execution does not itself impose a one-position holding cap.
- Session permit, startup reconciliation, kill switch, market hours/cutoff, daily realized-loss gate, duplicate signal check, manual carry-forward-underlying block, outside-held-contract block and fresh spot/chain/alignment checks.
- fast/gap contexts; Gamma/Celestial/Shield eligibility paths; strike ranking, entry quality, capital guard, fresh LTP LIMIT pricing, stop/risk sizing, durable ledger submission and order refresh.
- Gap-and-Go restriction against another nonclosed position on the same underlying; shared trailing/structure protection, latched exits and forecast-panel manual BUY support.

### auto_executor.py — live behavior differs from de33ef3

|Feature|de33ef3|Live source and consequence|
|---|---|---|
|Config position ceilings|`_position_limits(index_name)` reads `MAX_OPEN_POSITIONS=5` and `MAX_OPEN_PER_UNDERLYING=2`, passed to both capacity checks and forecast BUY|Helper absent; neither config limit is wired into the live executor capacity calls. No min(1, MAX_CONCURRENT_POSITIONS) replacement is present.|
|Parallel orders while another BUY is pending|v2 capacity helper accepts consistent independent pending intents and reserves their unfilled cash and loss-to-stop; configured count ceilings apply|Calls the older v1 helper with positions/pending only. Live helper immediately returns `portfolio_order_pending_wait` for any pending row or entry_pending/exit_pending position. Multiple already-filled positions can coexist; parallel pending entry admission is absent.|
|Retry-price protection|Calls `execution_ledger.retry_price_ok` before creating/submitting the position; rejects `retry_price_drift_exceeded`|Call absent; live ledger has no such function.|
|Delivery-failure resend notice|Uses the returned retryable status and ledger seen() to add the bounded resend notice|Absent; ordinary ENTRY-result notice only.|
|Entry function shape|Decision logic resides in serialized evaluate_and_enter|Serialized wrapper forwards to `_evaluate_and_enter_core`; adds Gamma timing_recorder.record_evaluation and note_choice. Lock discipline is retained.|
|Broker product identity|Reconciliation keys use INTRADAY|Position has explicit product field; reconciliation uses angel_orders.product_for(pos) for adopted/CAS products.|
|Managed manual reconciliation|Managed branch is enabled by Celestial mode; returns immediately on unhealthy ledger|Also enabled by MANUAL_PROTECT_ENABLED with a contract master. Reads holdings to protect manual options even if an unrelated ledger intent is unhealthy; that unhealthy ledger still prevents new entries.|
|External pending-order scope in managed branch|Clears all managed verification when external_pending_orders is observed|Discards verification only for positions whose contracts clash with unmatched external pending orders; filters absent order IDs/tags from owned sets; retains monitoring of unrelated verified positions. Entry/adoption remain blocked on unhealthy reconciliation.|
|Manual exit / structure updates|Manual exits use Celestial/non-Celestial verification; all open index positions can receive structure updates|Manual exit honors the expanded manual-reconciliation branch. Structure updates skip cas and MANUAL_PROTECTED positions.|
|CAS support|No cas_engine/cas_orders imports, cas_rows or evaluate_cas in this executor|Imports and retains them; serialized evaluate_cas dispatches to cas_orders; check_exits(kinds=None) allows kind filtering.|
|Manual protection exits|No dedicated MANUAL_PROTECTED block here|Warns on unverified holdings/missing quotes; evaluates dedicated manual stops/trails; keeps product-aware warnings/events. CARRYFORWARD/NRML manual holdings are exempt from equity EOD force close; their manual protection remains separate.|
|MCX session exit|Hard-coded 23:25 comparison|Uses priority_sessions.exit_due("MCX", now).|
|CAS exit quotes and urgent intent handling|Generic LTP polling and exit path|Skips generic CAS LTP polling; obtains fresh executable bid/option-VWAP/source timestamp. Missing bid retains urgent exit intent and queues pending-BUY cancellation through the ledger, then alerts rather than inventing a fill.|
|CAS exit policies and trailing|Generic ratchet and Celestial exits|Handles angel_orders.CAS_KIND via cas_executor.exit_reason, and lowercase cas via cas_engine exit_reason with its own stop/trail policy. Skips generic Celestial exit evaluation for lowercase cas.|
|SELL limit tick handling|round(LTP minus buffer, 1)|CAS product uses to_tick(..., SELL); other products floor the limit at 0.05 to avoid a zero-priced exit.|
|Forecast manual BUY organization|Class method decorated with serialized; config ceiling arguments supplied|Equivalent support is installed as external `_hf_forecast_manual_buy` then bound through serialized. Uses guarded getattr checks for carry-forward/external-contract sets and omits the de33ef3 count-ceiling arguments.|

The per-trade expressions `min(1, MAX_LOTS_PER_SIGNAL)` and `allowed_lots = min(1, allowed_lots)` ARE present for gap sizing. They limit lots for one gap trade; they do not limit concurrent positions to one. Live locations: auto_executor.py:750 and :793. Entry capacity checks are at :676 and :821; forecast checks at :1215 and :1258. This conclusion was obtained from current source, not from a legacy comment mentioning MAX_CONCURRENT_POSITIONS in capital_guard.py.

### execution_ledger.py — present in both

- Durable submitting/pending/unknown/terminal intent states and cumulative fills with synchronous=FULL; unique nonterminal intent per signal; pending(signal_id) query.
- Persisted queued exit intent, cancellation request for a pending BUY and final-quantity confirmation before exit; normal live fills rely on broker-book confirmation.
- Rejects regressing/impossible fills or changed already-accounted notional; tracks remaining quantity and SELL realized P&L; records cumulative fill rows transactionally and weighted exit price across SELL fills.
- Reconciles contract/token/exchange/side/product, attempts to cancel aged/unfilled BUY remainder after 30 seconds, and reprices unfilled SELL limits after 15 seconds at 15-second intervals with 2.5%/4%/6% buffers. Cancellation acceptance is not treated as terminal confirmation.

### execution_ledger.py — missing de33ef3 behavior

|Feature|de33ef3|Live source|
|---|---|---|
|Schema migrations and initial limit price|Adds price/note columns if absent; submit stores the first attempt price|Those migrations and INSERT price field are absent from this module. This is a code statement, not a claim that the current database physically lacks those columns.|
|Retry outcome notes|DELIVERY_FAILED, UNKNOWN_TIMEOUT, EXPIRED_PREVIOUS_DAY, RETRYABLE_REASONS and _setting exist|All absent; no outcome-note classification in the failed submission update.|
|seen(signal_id)|Allows another attempt only when every earlier attempt is delivery_failed, attempts <3 and age within 45 seconds; ambiguity or a sent order ends the signal|Any ledger row makes seen() true, including a terminal rejected/delivery-failed attempt. No bounded delivery-failure retry path.|
|retry_price_ok|Checks new limit against stored first limit +1% drift ceiling|Function absent; executor call absent.|
|Definite delivery failure|Marks terminal with delivery_failed note when rate_limited/session_unavailable; returns retryable and audits execution_delivery_failed|Marks terminal without a note; returns only ok/reason. Live BUY failure preserves an existing confirmed quantity as open, otherwise closes; de33ef3 closes a failed BUY position unconditionally.|
|Unknown outcome timeout|After 60 seconds, an order with no broker ID still absent from the book is terminalized via _expire; confirmed ownership is preserved, reservation released, never resent|No timeout/release branch. A missing match stays unresolved and refresh returns unhealthy.|
|Previous-day unresolved intents|Expires prior-day missing-book rows, including orphan rows with no position, using expired_previous_day|No date-expiry branch. Missing position or unmatched order continues to make refresh unhealthy.|
|Late-arrival watch|_timed_out_today, _watch_late_orders, _LATE_ALERTED detect/alert late arrivals, even when no pending rows remain|All absent; refresh immediately returns true when no pending rows remain.|
|Recovered unknown order ID|Deduplicates matches by order ID, stores discovered broker ID/state and audits execution_unknown_order_found before fill processing|Requires exactly one matching book row; matches ordertag only when local broker ID is absent. No dedicated discovered-ID audit/update branch; successful apply_fill can still store the confirmed order ID.|
|Unmatched accepted orders|Uses explicit accepted_order_missing_from_book / ambiguous_match / position_not_tracked diagnostics after timeout rules|Unmatched/ambiguous rows emit generic execution_order_unresolved; missing local position returns unhealthy without the corresponding detailed reason.|

### execution_ledger.py — additional live overlays retained

- Product-aware place_limit_order, book validation and modify_order contract; CAS SELL repricing uses to_tick and passes side="SELL", while non-CAS uses the 0.05 floor.
- Staged BUY fill averaging uses `(existing entry_price * existing remaining_qty + incremental notional) / (existing remaining_qty + incremental qty)` and recomputes lots with ceil(remaining_qty/lot_size). Baseline uses this order's cumulative notional/quantity as entry_price and does not recompute lots there.
- Records submission evidence through trade_audit.safe_call and marks confirmed fills dirty through trade_learning.guarded. Those calls are absent from de33ef3.

### Verification and scope

All live Python source hashes still match the original Step 1 record. The new replay at 52010ce is documented in 02_replay.md; it changes no server source or policy. Source comparison is static/read-only and does not attest live broker outcomes. The original effective-systemd-unit and public-IP access limits remain unchanged. No install, restart, config edit, broker order/login, or live flag setting was performed in this follow-up. Step 3 stays stopped.

---

# Step 1 — read-only server identity

Status: tested (read-only inspection); deployed: NO; activated: NO.

Host: ip-172-31-41-95
UTC: Sun Oct 11 14:40:56 UTC 2026

## Live Git identity

```
kadima-live-backup-20260915

A  docs/hourly_forecast.md
 M kadima/angel_orders.py
 M kadima/api.py
 M kadima/auto_executor.py
 M kadima/capital_guard.py
 M kadima/cas_blast_scanner.py
 M kadima/checklist_forecast.py
 M kadima/config.py
 M kadima/control_api.py
 M kadima/engine.py
 M kadima/entry_quality.py
 M kadima/eod_watchdog.py
 M kadima/execution_ledger.py
 M kadima/exit_manager.py
 M kadima/feed.py
 M kadima/forecast_observer.py
AM kadima/hourly_forecast.py
AM kadima/hourly_forecast_model.py
 M kadima/instrument_master.py
 M kadima/macro_watch.py
 M kadima/option_chain.py
 M kadima/rate_limiter.py
 M kadima/scrip_master.py
 M kadima/stock_execution.py
 M kadima/stock_handoff.py
 M kadima/stock_universe_scanner.py
 M kadima/storage.py
 M kadima/strike_selector.py
 M kadima/test_order_pathway.py
 M kadima/trade_policy.py
 M run.py
A  tools/forecast_backtest.py
A  tools/forecast_day_report.py
AM tools/forecast_nightly.py
A  tools/install_celestial_embed.py
A  tools/install_forecast_nightly.py
A  tools/install_hourly_forecast.py
A  tools/strikes_today.py
A  tools/test_forecast_manual_buy.py
A  tools/test_hourly_forecast.py
A  web/celestial_forecast_embed.js
AM web/hourly_forecast.html
?? connection_update/
?? deploy/
?? docs/manualstop-20261009T191938Z.md
?? docs/manualstop-refinement-20261009T193405Z.md
?? docs/option_chain_data_report.md
?? docs/preflight-20261009.md
?? executor_fix/
?? forecaster_update/
?? kadima/auto_arm.py
?? kadima/cas_config.py
?? kadima/cas_engine.py
?? kadima/cas_eod_guard.py
?? kadima/cas_executor.py
?? kadima/cas_orders.py
?? kadima/celestial.py
?? kadima/celestial_adoption.py
?? kadima/celestial_exit.py
?? kadima/celestial_scanner.py
?? kadima/celestial_universe.py
?? kadima/commodity_watch.py
?? kadima/execution_owner.py
?? kadima/external_reconciliation.py
?? kadima/fast_momentum.py
?? kadima/feed.py.tick-backup-20260918T102223.015792Z
?? kadima/forecast_bhavcopy.py
?? kadima/forecast_chain_data.py
?? kadima/full_universe_scanner.py
?? kadima/gap_and_go.py
?? kadima/gap_baseline.py
?? kadima/gap_live.py
?? kadima/index_event_gate.py
?? kadima/manual_protection.py
?? kadima/market_session_gate.py
?? kadima/mcx_live.py
?? kadima/momentum_quote_watch.py
?? kadima/move_capture.py
?? kadima/move_capture_execution.py
?? kadima/move_capture_handoff.py
?? kadima/move_capture_service.py
?? kadima/news_catalysts.py
?? kadima/news_collector.py
?? kadima/news_context.py
?? kadima/oi_confirmation.py
?? kadima/owned_watchdog.py
?? kadima/portfolio_capacity.py
?? kadima/preparation_diagnostics.py
?? kadima/price_history.py
?? kadima/priority_sessions.py
?? kadima/rate_limiter.py.quote-backup-20260918T094309.367072Z
?? kadima/shield_confirmation.py
?? kadima/stock_fast.py
?? kadima/stock_fast_api.py
?? kadima/timing_outcome.py
?? kadima/timing_recorder.py
?? kadima/trade_audit.py
?? kadima/trade_learning.py
?? kadima/universe_api.py
?? kadima/universe_display.py
?? policy_update/
?? tools/index_news_gate.diff
?? tools/kadima_db_compact.py
?? tools/manual_protection.diff
?? tools/manual_protection_refinement.diff
?? tools/market_data_retention.diff
?? tools/market_data_retention.py
?? tools/option_chain_data.diff
?? tools/preflight.diff
?? tools/preflight_autotrade.py
?? tools/preflight_output.txt
?? tools/replay_day.py
?? tools/replay_scalp_gates.py
?? tools/test_audit_fixes.py
?? tools/test_cas_executor.py
?? tools/test_index_news_gate.py
?? tools/test_manual_protection.py
?? tools/test_market_data_retention.py
?? tools/test_oi_confirmation.py
?? tools/test_option_chain_data.py
?? tools/test_preflight_fixes.py
?? tools/test_timing.py

commit 433ee0095b925a33e6f5fef04a4aecee0a4e2535
Author: Ubuntu <ubuntu@ip-172-31-41-95.ap-south-2.compute.internal>
Date:   Tue Sep 15 17:47:40 2026 +0000

    Backup live KADIMA state before Gamma Blast integration

```
## de33ef3 comparison

|File|Baseline blob|Live blob|Identical|
|---|---|---|---|
|engine.py|0c5411bd9a89fac77d082dc53f8d6e31f5a85298|fc2c5ce3169b110cd01686f332aa327681dc1cb4|False|
|auto_executor.py|21f1257fbcd94bc62c5f45b288e34eb872d921e0|f86a5f369e4cb1c7cda4d48c6a1ed3f45101b039|False|
|control_api.py|96e1638e8eb9005b65e16b2b6f61f414eb45b635|3ec1ae2e6c1d11a22fba2ef1a7a1200fc034f596|False|
|execution_ledger.py|eace209cfe4f6356914317c006d4b1b1be1e6545|ffe700bca0c6e0ea523dd2ce34db2f19a0c0e5aa|False|

**de33ef3 parallel-orders / retry changes installed: NO.** The ledger lacks delivery_failed retry semantics, retry_price_ok, UNKNOWN_TIMEOUT, and expired_previous_day handling. The live tree has separate CAS/product and staged-fill changes. Do not overwrite it.

## Service configuration

Systemctl result:
```
Failed to connect to system scope bus via local transport: Operation not permitted (consider using --machine=<user>@.host --user to connect to bus of other user)

```
Fallback: unit and drop-in files read directly, with all Environment values redacted. This does not prove the effective loaded unit.
```
# /etc/systemd/system/kadima.service

[Unit]
Description=KADIMA Trading Engine
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kadima-py
Environment=[values redacted]
Environment=[values redacted]
Environment=[values redacted]
Environment=[values redacted]
Environment=[values redacted]
ExecStart=/home/ubuntu/kadima-py/venv/bin/python3 /home/ubuntu/kadima-py/run.py
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/kadima.log
StandardError=append:/home/ubuntu/kadima.log

[Install]
WantedBy=multi-user.target

# /etc/systemd/system/kadima.service.d/99-kadima-auto-execution.conf

[Service]
EnvironmentFile=/etc/kadima/auto-execution.env

# /etc/systemd/system/kadima.service.d/zz-kadima-session-auto.conf

[Service]
EnvironmentFile=/etc/kadima/session-auto.env

# /etc/systemd/system/kadima.service.d/zzzz-kadima-paused-start.conf

[Service]
UnsetEnvironment=KADIMA_FORCE_START
EnvironmentFile=/var/lib/kadima-autotrading-20260928/trading.env

# /etc/systemd/system/kadima.service.d/zzzzz-cas.conf

[Service]
Environment=[values redacted]

```
## Process identity

Public IP: UNVERIFIED — network access is restricted; no static source-code IP is treated as current.

Dashboard SHA-256: 01081776d0f6bb5688fd3ff066b6cb04675de746ebd5eb692d85aaac30ac323c

## Resources

```
Filesystem       Size  Used Avail Use% Mounted on
/dev/root         19G   13G  6.0G  68% /
tmpfs            953M     0  953M   0% /dev
efivarfs         128K  3.3K  120K   3% /sys/firmware/efi/efivars
tmpfs            382M  984K  381M   1% /run
none             1.0M     0  1.0M   0% /run/credentials/getty@tty1.service
none             1.0M     0  1.0M   0% /run/credentials/serial-getty@ttyS0.service
none             1.0M     0  1.0M   0% /run/credentials/systemd-journald.service
none             1.0M     0  1.0M   0% /run/credentials/systemd-resolved.service
none             1.0M     0  1.0M   0% /run/credentials/systemd-networkd.service
tmpfs            191M   16K  191M   1% /run/user/1000
tmpfs            953M  591M  362M  63% /tmp
/dev/nvme0n1p13  989M  163M  760M  18% /boot
/dev/nvme0n1p15  105M  6.3M   99M   7% /boot/efi
tmpfs            953M     0  953M   0% /tmp/codex-daemon-1000
tmpfs            953M     0  953M   0% /tmp/.git
tmpfs            953M     0  953M   0% /tmp/.agents
tmpfs            953M     0  953M   0% /tmp/.codex
tmpfs            953M     0  953M   0% /tmp/.aws
tmpfs            953M     0  953M   0% /home/ubuntu/codex_handoff/.git
tmpfs            953M     0  953M   0% /home/ubuntu/codex_handoff/.agents
tmpfs            953M     0  953M   0% /home/ubuntu/codex_handoff/.codex
tmpfs            953M     0  953M   0% /home/ubuntu/codex_handoff/.aws

               total        used        free      shared  buff/cache   available
Mem:            1905        1215          78          10         805         689
Swap:           1023         973          50

1.4G	/var/lib/kadima

-rw-r--r-- 1 ubuntu ubuntu 368M Oct 11 13:28 /var/lib/kadima/price_history.db

```
## Every Python file hash

|Path|SHA-256|Git blob|
|---|---|---|
|kadima/__init__.py|e9beff616dcc1416e900630cc27282df777bdb46001ae52d7f1485ae1f3ccb77|9436380d1ecc1252e5398dbf66c2db5c745cdd3e|
|kadima/alerts.py|1c04921f63e3e11c8b447745d3f8e7b5f819b5dc486c06b4edb7bdced8354d45|472c361e3636805bcb48591398a3f2a5fa708b18|
|kadima/angel_orders.py|fbb6ccfbabfdc7a8145e0543721c73aca74aaf50a00ecea699cc1d1c90d98534|466c0415dad265daf0e58350a2f38c969724e3f6|
|kadima/api.py|e38de8a505253abff0b074ee8da0e3223fce38c38aef932ddd9faad65988aa00|2a515cfae13850b77d88ef076c7378bdb7ff760e|
|kadima/audit.py|1034ba1b7ba48b2b41d36b34691648bb76ab2e9d92d59b514106546ae060ad98|4b8cc576e8b2215fde0dbef670b6de3f01487e39|
|kadima/auto_arm.py|1ba29590dbf93e7b726ae60c0b18e4cce613ba0a482a33d22a30ed6af8d4708c|e359b816dd3e9a1c3d0e47167bd4bd60680c7027|
|kadima/auto_executor.py|d89711002f29f3c56f96b26236b7458635f71c0ad63f7098f805981387a832d1|f86a5f369e4cb1c7cda4d48c6a1ed3f45101b039|
|kadima/candles.py|c26ff4f5f314ce6c5166694ce0d351eb64062beb22b01dba897af5bdcc8e8ed2|a43d9c614c75b5e61c213f9d0087dfd966497ffe|
|kadima/capital_guard.py|5141f361513b668a0382485537be5d2108127a6c9006e5bdf58fcd82d2988dab|d3cfe78a4d6aa6a682bbb8b02c84779146db7fb6|
|kadima/cas_blast_scanner.py|ac6991384030e7e2d51613b52afac15580624a154607791bb936dca0b58c5f8d|b5a24863d27d8f0029e3a6b7525311387e1ddda4|
|kadima/cas_config.py|8afc68d75b667a3d9b72d3bd37606bc066ec7f8207a1431503282d2bfbca6686|516ac34a51cee4b708c6a4403ffdc0b92478533a|
|kadima/cas_engine.py|f5c02066858b6fdc752601bc56bf07cc63bfd8e1cfc1bad2e3cfed7f0f8b1d8e|ec9d23b2d6e08b6e4f37d1241f1052bb2c880efe|
|kadima/cas_eod_guard.py|3b3e993e42772bb9cba5eb5d7d8652c67e6f1edb2d834ae6414997be3b7ef41a|a7c27efcd24aa3a1ec9a1d63ef241a8272c96882|
|kadima/cas_executor.py|744c612aa72fc73d3988c194f29c7a3f5210c86d6f113743903cbd5f597d4243|6d419178d2d47653ed4a1c0a959074faf44cfae5|
|kadima/cas_orders.py|153c512134969af1ba136caa27e580cf21fed41c588816fff844952010be3a8d|32988f28f0fe47a2ecb25a586b9d99cbe3f567c4|
|kadima/celestial.py|3b45dc9a61c693e262a3c14c23e973fcfac28cec8f7a53552569f5fc42e73eb8|3df08d371d6c3271dfd9ef5e1251d03b8dc64157|
|kadima/celestial_adoption.py|0eb17dc243344ac285055db8629342de134887559728e4573ac9e90d26a804d2|a6b71963dc4933f1d02696f25ad6970efb31dca5|
|kadima/celestial_exit.py|1609944da509ee9a9165549a70419c6862195be8ebc38c942df4072c99c0a54c|6c96f4bcba246df93325f6108ce3949d115a5a8e|
|kadima/celestial_scanner.py|a15f66c700deb26583aaed908bbf303daa30bf8907467812a7310359726430ef|32d342f035f99f110a9facebb13b44e850faa6dc|
|kadima/celestial_universe.py|a54d107ce9d546ad9208c4073ed923e1511a657fde82cf4102c049e2abaf082e|0776824179727d22d5e250a3c8ae1ecf2322ce2f|
|kadima/checklist_forecast.py|cca9aa9c133286236397b846c85f06792ffb6b5d789cd089409efffa0c9e20c0|3c66e68c54a55e2eace3c1f7c8a69731f16ab81f|
|kadima/commodity_watch.py|218d89dc5751c5ebfc1fe9ff421c609bb8e1b8d1fb7f7da2f9d483891eb6165f|2697a9b637ff4bd19f4a100fd3340a91facb216b|
|kadima/config.py|6396505296c4d97496600b8c5e8be9d1bd31ac9bdc7333e4657c553225af6dea|1a4f1b535d17651350c4242a2099499c4a994385|
|kadima/control_api.py|22defcb0dd6eada1ba678e54925f6a865a3cc014a4998a8c7f984200a94783c1|3ec1ae2e6c1d11a22fba2ef1a7a1200fc034f596|
|kadima/dynamic_stop.py|f4134491c7cd655db504e5ede7432769e64753739235c73fac884de1a3931065|6c8ef7c29c1c5941ac02755e680ee2a9891e8a85|
|kadima/engine.py|e04de181ed97bc274f217a526e0a22e576dda76dc38125854dd59c32c0b026bc|fc2c5ce3169b110cd01686f332aa327681dc1cb4|
|kadima/entry_quality.py|036e8f1890a254f5e185cb06bd5e7577ebe1c08852994504996342c4f300d8ff|b1548af877b449912fc4a87d944434cb8b452088|
|kadima/eod_watchdog.py|1ba9f6ea5612b164f6904c593ef28e290e7a7627250307040f1c2ee8a0726e5b|d184872de2aa63814fc908807d18c97f361cf512|
|kadima/execution_ledger.py|ce2a8aa20480625e4fb6e5c8c29478019b0f13861ab3f9001b7929c87b07a875|ffe700bca0c6e0ea523dd2ce34db2f19a0c0e5aa|
|kadima/execution_owner.py|28f510c59f734c29052d12e69583135e349b58a87caa0ad1314d7fe04301d812|7af98d30b86c90bc503d779b5dc7ec595f431bde|
|kadima/execution_pricing.py|9e7be07b122040bc439583ae869415578be4b488974ff93fcc49a1e0ab43cf8e|fb27f668f152653ffb10d3f8b3a7990b86f95442|
|kadima/exit_manager.py|ad062c7be873bcb8fa315fee31f371030ff5766bd75a7781feb2b20dde70e331|4aa71e271b20fa6aba2afb4679301624c37772aa|
|kadima/external_reconciliation.py|a18105d5eb5125b78329672d7380963b86b314c9361c29c9b7deda7307e2d369|18af16e13a3be83f6ffc30679476b7c66abdd968|
|kadima/fast_momentum.py|91fa8956e1715db9d2670ba422b684bfce44545c9ba032084ebf87cbb158a67b|9dd4fd9416c2ddb5e8ed077cbfa5848b4cbbc316|
|kadima/feed.py|9ac0d6ffa06c71d0122df3b14bf7005d6d1173981098ca5818fb88d672d7e344|8a9f8ea0aa5a708739c9297583badabc85fc2c2a|
|kadima/forecast_bhavcopy.py|2d5843a0e65cf2045fc153956d9a9b811140c65c2cd95826b13b94bda6501d75|5c9f9f7e6e86ebf12d734eeae43c8c889abb5ec5|
|kadima/forecast_chain_data.py|19bd15543fd81606dcf2f1e18babb509eddf3e6b2947031d141017542ff0775d|2ee52fdf068753309c5b8e1dac7340a115f6a16e|
|kadima/forecast_connection.py|9eae0ebd31ff013fce552265e3914eb11dd905111e75c9c6563260665f6f9abb|8f08f4e77caca88c0666f96180220748040fcea2|
|kadima/forecast_observer.py|a4b7b66cd78936ff81e87486880530ed3f462960739c1950b589733a62e7bf28|28bd4ace9973d5eddc6f79f362b13eaa2ac73463|
|kadima/forecast_trail.py|3f47e92010b46690ce2650b11fc970d05775ee7882391819da4dba61b1df2125|034db8cd18ba2e14e529f2878b6684b3473c53d5|
|kadima/full_universe_scanner.py|1c396222f3a3e16c95f9729d0226cf17dd781eb10a0634e1b8050a46bed38443|834a06e6faa5b06f9945cc9e57ead7f56a8f4270|
|kadima/gamma_blast_scanner.py|02010d88d0c0a9afb9a0d10ce72fc35f368e49685d1e5a2319e0d0752cd9cc8c|ad50afee9da2cb1e83b1fe5d4646392df6c618c8|
|kadima/gap_and_go.py|8ae0538deaa4aa6ddcbabb67e33a102de6d0e45c462129c41529280a436e2288|93493b02c26ef2657a8ae2c3ddbe87ce8a44873b|
|kadima/gap_baseline.py|f336806221ea4c37a4cdcd2e96f266ca4db906b2f0c3677abfd373192095a36e|97e2851b4c599ca7b04fedba2bacc39169074455|
|kadima/gap_live.py|bfd50acdc8bac112bd4100238fc9029bb98dffaf793b9edfa5ed5264ab6a72af|1bc0f6233d70ede026bf462bee114b4116f791f6|
|kadima/greeks.py|68b042e835a5a1d5524befcc1b236d5f210c56b0fba52cf759c3d93d2601b0b3|b371a0a63ab0a0a13fb677d20a5e9b487e6ba9cc|
|kadima/hourly_forecast.py|c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870|83e344e5aa2d2573809d6000663cbec85304d7bc|
|kadima/hourly_forecast_model.py|4856671e3fec23a113648ba0ff1d649a8b49d22e3375f58a0d6636101be11fd3|a351d20d414de238b953e92e5f064687f00c2578|
|kadima/index_event_gate.py|665ade4f2fda0db8e032d7fe878d4fcda824cacc69467897db53ca9b888764cb|420daa2f86577a4e9459ac41f3c70b68eef1d078|
|kadima/indicators.py|666bdb238c8efcf6ff9b0087ed1b57f81434e532f941484ba39e6bf563c8b5b8|5698e81af786ee07dafcad5fa305e8b0d89eaf82|
|kadima/instrument_master.py|4f782ac83a447ed7e2cc4b498bc1a92556e9f9c01fe5b1db7fd0388fa29e67a7|f144267bfdad372ca5c0bbc2e191265cbaa0fa37|
|kadima/killswitch.py|fda43fd315d048d1df46a5290dbb8710830ca064e93f43b5ad7891ed55d46b34|0ddf753d67337fa1188e28f8da282007e686ec3a|
|kadima/last_known.py|842d168f18c2d3407786f521098ce8723ee0bbae511269f9194e6c61a6197f7f|ae1803c25f6a6ac284ede56ebba97171890a1065|
|kadima/macro_watch.py|7855b3f0fcbf04015bade1083743be89535fca8de1d85a18f7610d4b463767ec|61f04cb86af8f187a6798e272d49f31776f4642d|
|kadima/manual_protection.py|7c9e044191474cbab4bd81a2dfee661e789f8fda55d30f9d6b1bf52ae57b461e|3a7551bb43d0af293154b51ebde6a98c7898566c|
|kadima/market_session_gate.py|d26c7dbcc14ab6e050b5bd281647afdc23b8aae4ff4809dd0357e64a6f8c88ae|f7e691b3eff05b868fe5b9c4dda86e4dcd15d17a|
|kadima/mcx_live.py|a15c829448bc827ee2a959cbe6968fa333effe2823bac8b9014f0e301a95b863|4080fbb4f1f678ed489e6a99f674a1fcb25b216d|
|kadima/momentum_quote_watch.py|7c77ef4c9bc4bd1348c7f68a3427b031b7531eeedf6531198b536cf45450f3fb|f5ce00ec363c2494e92c3f0f0b57f158f28fbc38|
|kadima/move_capture.py|27827f127ec6c7c6d6432fd8f8d8036e8cadc10d97db31c0e4320dcd160ed077|b27014acd121a34b015c4de0febe4c3646ab2953|
|kadima/move_capture_execution.py|8e0f5ac20a854391529001464c6a8033e0caed71dfae3114c9269053fa1bb94e|4567a2cd2c018b7c689f25044ff67c576a9a6085|
|kadima/move_capture_handoff.py|56ce82530b46d6a99bc27cde7a09f4d910bb9e9e8f8f420f3b5b2c8d1ed0ed83|a880ba1a94d68ad8878a589dbc2985a2dff88e3d|
|kadima/move_capture_service.py|63b0e133c2b9122673b868cd8bc8efa41ae340941e740accb0aa5b7c5c141d68|6a03acc981fdfed81da569f311c1ce33f3db7ba3|
|kadima/news_catalysts.py|0cfe1df0fe3e2eb2a8f2eaa464500b5d85ce0efb29143c85de889d9b370c8a13|2cdbcdd91616c75b8c57a9dc6192b6b6aa2c09e9|
|kadima/news_collector.py|62d8b317c8a5629f7e98ad9d6bb5b696d81533aa60dd88a45ab6a5331c06b9ff|a1fc2610d995b198da508214e8dcfcff5446b17a|
|kadima/news_context.py|1df8d9ddbec04394c2ae1b0b2ffca994c8ee1aedd1ae253bbbbf102676da8140|c8f31dd813856167749308455461973b76233983|
|kadima/oi_confirmation.py|1476918ae1056b69f4d43d0b37fce24e8971ad81c7324b4aa610c0c5a617a37d|a02942317829761bd2c5020acb06756eea20155f|
|kadima/option_chain.py|9780d7de6fe2cf9f6fa1087879b5fb7e5279f299ee8811806f75c09d70fc4c91|91b412e425acc27124d4d525e1b69f2b92bf0ee2|
|kadima/owned_watchdog.py|ea6d166a0d4301e34c7a34817c51f58ffa18914d4fce3a5db1858e12a744c857|f1af5d46a28818c442d2c646fa45b600d51b0c62|
|kadima/portfolio_capacity.py|cfabf66cdc3981def06b1f0302462bc7b75a13156e31c42a3483cc1acfb4f066|c5d92290e608d22241afbad88cf5eb6dcd4bf22b|
|kadima/preparation_diagnostics.py|8c9c53137a4730c91ff371108f45c6d980d8ddffe5201ef5b349307558d26fd6|43a4ea613825e8b3dc7153c53dec1f6bc07eafbf|
|kadima/price_history.py|5984e8d0e45f3c2b6e698f7d4d58737b057358cd9a8ea6fa0ffc8da518228a9a|73cf94eff704674544b50568e59e9358e8f5383c|
|kadima/priority_sessions.py|c38fa43efed33c2d3c9fc7b2425941e72c2fc27de6d501bb74cae358e7749d09|94fee8c80af61e31a6afba8fdd7278c309c32bc5|
|kadima/rate_limiter.py|bacccec8287f985115ac5a359f6db388c32989868dd8503ad580f4ab0b70a6a0|cbe90c82019edc3ec063a64cc73c18570f0c8fce|
|kadima/risk_budget.py|20bd0cdb7e5c7296315324bd1d4970812a63dd67fd6a7096b64daf07f715fa6c|a8ccc00e3ef22edaec97e3f6f067ed915b2c212f|
|kadima/scrip_master.py|147f82ea870083e47da84740a02c6d3d71576d9ed08906d9323cec846bebf162|0eb3f86e68bd0f79e2ceee808be8e19767a41531|
|kadima/session.py|6b8ade289949ebcb00eb5460852cee7682f7c55591d021d72b612fc6612303f4|55c8ed0c50d53bb86ba2f26c4d9b2c79897a7269|
|kadima/shield_confirmation.py|0e5e8ef5370088caf2d29c38d6283723493ee4357bda4fac1da709e0d80d5cdb|88f8485c1f8845147db8074fb82b1dcda0bc90ef|
|kadima/stock_execution.py|312266c7b615919546e2e8a15875da572ee52df876fcd2350107e0e0880bf604|554e4d10542451237c3a2f542cc8a8d6514b69d4|
|kadima/stock_fast.py|45e7ec7ac45553587e9404eab4012b5ef94e11b3e7044aa2cbe37737520b475b|7773f7e60644d06f4328a8134bcb44065b1610c9|
|kadima/stock_fast_api.py|68cbe409d1600fd70e0161b77e84c3410746196cdef86078108525808fc3aa7e|57805126938637754e6fa22aa772fd132aa3a891|
|kadima/stock_handoff.py|7fd39688b76d4b77035c8cdaac77ef0897c6eb5da81c4326672f72761410af85|f5b238272bcc84c474afc26aa66030445334dab4|
|kadima/stock_universe_scanner.py|6456988065e212ac3726abc95c28098ff2fcee9246f073bc28943c0882671b72|1117df7df0825e918a5ae119e9abb00d0627df62|
|kadima/stops.py|7975a6fde4c191530b6b8cd349df80717339d117a6b957de9bb5bcab69cc7ad1|a8815f4d91e8a87ef69f43d545f275d2dc640727|
|kadima/storage.py|234da3afc344c999a45757d57e5ff56e487d46103ee21cdce016f78ef879de87|5875846967be1d2c1887ee99d5ad8d1cea20d326|
|kadima/strike_selector.py|cfe6056b6c0aaba8df9c46c0fcbf5198d3926e3bf7f40b4c776b69ac3b3d93b7|78cc50a800e53ab817e4699486983572da7e6a17|
|kadima/test_order_pathway.py|462768706a16f8973b1e024167220a3936ec34050389817a188215169fc35b15|5f4d39ad3834b81a16f1eee5a56532d72d05a765|
|kadima/timing_outcome.py|99311d74c1cfe39c878728362aa60bf56a62d3de07fa95d20b7d81d77ea96f48|1f71232358e849c39ea671f013bec5657c515b0f|
|kadima/timing_recorder.py|c143ab8b5713f91efe474d3901da5073e6d27574ab136a355e8d9d13d925a92d|03abeae62f3bb688b85078bc148b2a2c3d8c1b88|
|kadima/trade_audit.py|1e74368bd800277b330ced5fb00ea0a8b9fa70d29e226a3ecab1b63d89a95adb|4215094ff71248ea4288a72b7d3da8910df2c7d0|
|kadima/trade_learning.py|24041df3108e66ea42ceb93b5d1ecb7aa8168c4479100d64ce7dfd711c94c076|411ca5bb00fa1192f5dfa9b363166d6a236c9f96|
|kadima/trade_policy.py|a8ff5f5f11549c02de0202229f77e975bd0637ede77a1d6b3b69edb532040bdc|cc84936e0da3427b7721f982c6be0b47dc1cd669|
|kadima/universe_api.py|5009c4efd3753c6624dfc66dd5cc610548389e2b8a8f49fbf3ab4716a8aaa08b|b8bd92f3edbe62c1e4a93a1747d27c0fd4e97510|
|kadima/universe_display.py|508b809609854f0a047070a062a8c7d66611b9bde4c70ba901b9d4dc0b37332d|7cc59f92d5fd5cddf47ab68390b828199e339792|
|kadima/utils.py|6701d127ed790e16a3526dbbe059c8acb6eefb0b70abe5296a6c805dc2e66278|4781fd73993d899aa0c6c606521147f49cab895d|
|run.py|03ea3de55eb18b8e73ff4be0d4992dabbbca51ae1ee6f6846ed337309a72e03e|9828f4ad0bafd77ff30d1fbed46eb9429e2fda3a|

Live activation performed: NO
Broker orders placed: NO
Broker login path invoked by parity harness: YES; request failed at DNS; no successful login observed
Services restarted: NO
Protected live files changed: NO

## Hard-rule stop

The offline parity preparation invoked the live-tree broker login path despite socket guards. DNS failed. This violates the no-login rule; all replay/install work stopped. The generated parity JSON is invalid and has been replaced by a failure record. No service restart or live source edit was performed.

## Corrected preparation follow-up

The login isolation issue was corrected before imports. The hardened before record has 23 scenarios, empty stderr and SHA-256 045be57eaf6535af2368c0293d036e79d779f6c0da107c80de39ad1106aad462. The isolated proposed-copy result is byte-identical. All live Python-file hashes were rechecked and are unchanged. The incident above remains part of the task history.
