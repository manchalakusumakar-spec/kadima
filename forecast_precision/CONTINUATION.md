# PR40 continuation — resumable receipt

State: BLOCKED FOR HOST INTEGRATION / NOT INSTALLED / NOT ENABLED.
Branch: codex/pr40-precision-continuation. Starting point: 6a398750a90bec2eb08b149d231bb663b2b2e7fc.
No production writes, service operations, new subscriptions, broker polling,
model fitting, orders or trading source changes performed.

## Read-only inspection on 2026-10-12

- kadima.service active, PID 552968; unit /etc/systemd/system/kadima.service.
- ExecStart /home/ubuntu/kadima-py/venv/bin/python3 /home/ubuntu/kadima-py/run.py.
  /proc executable resolves to /usr/bin/python3.14; cwd /home/ubuntu/kadima-py.
- Installed source fingerprints: HOST_SOURCE_IDENTITY.json. They establish disk
  identity only. Loaded Python module bytes and overlays remain UNVERIFIED;
  service start or Git HEAD cannot prove them. Installed model reports hf-2.0.
- Nonsecret config-file SHA256:
  6396505296c4d97496600b8c5e8be9d1bd31ac9bdc7333e4657c553225af6dea.
  The whitelist-only process inspection found no explicit KADIMA_DATA_DIR or
  KADIMA_FORECAST_INDICES setting. No environment dump or secret values recorded.
- Databases /var/lib/kadima/kadima.db and /var/lib/kadima/forecast.db opened
  mode=ro, query_only. Latest receipt 15:40:01.163040 IST; latest completed
  candle start 15:39 IST; latest forecast day 2026-10-12. This does not establish
  feed completeness, OI/Greeks freshness, or actual live-session availability.
- Celestial embed points at /forecast.html. Nginx serves both from /var/www/html.
  forecast.html SHA256 e52ec259cd2ee07db2e6fea578ab0a16d49eb08255eed521ff0fbb1c3ba9972f;
  embed SHA256 1faa2488be1838f8d08e26e8371e6d6630d2de71e45ac9413e270223c0aa928c.
  No production forecast/UI replacement.
- Installed constituent_radar.py absent. Engine's radar is a distinct chain
  publisher. No dated all-50 membership/weights or fresh coverage established.
  The reference radar's <=62 samples/member is not 50 complete chart histories.
- Disk: 5.6GiB available, 70% used. RAM available 236MiB; swap free 4MiB.
  Existing dashboard/news/macro/universe scanner and pm2 services active.
  Competing deployment exclusion NOT established from this service list alone.
- Outside market hours: WAITING_FOR_LIVE_SESSION. Fresh broker position/order
  reconciliation has not been performed; installation must block without it.

## Implemented and verified boundary

Bounded read-only local candle reader, required writer-lock snapshot capture,
one-item observation queue, explicit independent feature timestamp mapper,
CAPTURE_ONLY consumer, immutable restart-safe snapshot journal, original-only
per-horizon scorecards and separate guarded staging code. Original release_plan
--apply refusal is untouched. Trading and production forecast files unchanged.
No synthetic fixture has been fitted or deployed as a live model.

Host integration remains incomplete: no source-matched writer lock/cache handoff,
no independent native OI/Greek producer timestamps, no all-50 radar, no genuine
same-packet existing-model wrapper, no live publication endpoint. Research
outputs explicitly have actual_publication_at=None; they cannot be counted as
published forecasts. No fitted live candidate available. Net Error remains unset.

The staging CLI refuses apply/enable because broker reconciliation and deployment
exclusion providers have not been source-matched. The library also refuses stage
without specific approval, fresh reconciled flat positions/no pending orders,
unchanged installed source and exclusion of competing deployment. It stages only
committed forecast_precision files into a new release-specific disabled directory.
No working installer activation, backup/rollback or composed-live parity is claimed.
Do not request deployment approval until those pieces are concrete and reviewed.

## Validation and next steps

Isolated /tmp/kadima-pr40-precision-venv, Python 3.14, numpy 2.3.5.
120 precision tests passed including primitive-input bounds and detached radar/lock contention.
Nine existing suites passed: hourly, hourly201, manual-buy, audit, offline,
OI-confirmation, Shield-v9, timing, CAS. Network denied in existing-suite harness;
temporary test data only. Production packages unchanged.
First actual-host reader attempt interrupted at its default 100ms bound; no
successful cache read/resource validation claimed from that attempt.

Resume by verifying the loaded-source/overlay identity and authoring the smallest
reviewable native handoff against that exact composition, with trading behavior
parity. Recover dated official all-50 weights rather than inventing them. The
constituent reference may be reviewed without installing its trading overlays.
Recover independent OI/Greek timestamps or keep those features missing. Add a
pure existing-model adapter only if it genuinely consumes the identical packet.
Implement commit-time publication receipts and score only targets still future
at that publication event. Prepare source-matched reconciler, installer backup,
health and forecast-only rollback before asking production approval. Do not restart
kadima or install a second broker collector to obtain access to memory.

Second host read also interrupted: 101.81ms, process peak RSS 40192KiB.
EXPLAIN uses native primary-key SEARCH; no full-table scan. Reader remained at
its 100ms default; budget was not widened to claim host success.
A repository-root test invocation failed from the existing tests’ import-path
assumption; rerunning from forecast_precision (documented command) passed.

GitHub continuation will be a separate draft PR targeting the original PR40
head branch codex/nifty-precision-shadow-v1; PR40 and its reviewed commit remain
unchanged. CI must pass on the continuation SHA before any release consideration.
