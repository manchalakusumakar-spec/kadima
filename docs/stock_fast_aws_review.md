# AWS evidence review: stock-fast release

## Observed, not inferred

The user supplied screenshots of a saved host report timestamped
2026-09-19T11:32:40.919553+05:30. This is a historical observation, not a fresh
broker-book check. It shows engine/scanner/dashboard active, target-directory
match true, Celestial rollout false, global auto false, broker-order permission
true, fast-master false, kill false, local/API/pending counts 0/0/0, a differing
run.py, and a PM2 kadima-bridge process online. Earlier screenshots show the two
stock-fast modules missing or unreadable, a base full-universe scanner and a
FIFO rate limiter installed.

This means the new Celestial stock-fast route is not operationally established.
It does not prove every legacy trading path is off: the broker-order permission
is true and the Celestial flags do not describe all legacy paths.

The old report collapsed absent reconciliation/news records into false. A
NO/NO summary cannot distinguish not-published, explicit-unhealthy, stale,
invalid-time and unavailable API states. In the existing code, some health
publication is conditional on Celestial rollout. Missing records with rollout
off are not proof of a broker outage or failed credentials. Do not manufacture
healthy records or bypass these checks. Zero cached/local counts do not prove
that the broker is flat, especially when reconciliation is unverified.

## Preserve the deployed source

The exact installed rate-limiter blob 9886d951be6997e0ddf8fdda640529f5f751a25c
has been carried into this draft branch. It retains the FIFO ticket queue,
12-second bounded quote wait, shared budget row and spacing. This prevents the
release snapshot from downgrading it to the earlier non-FIFO implementation.
The new tests pin that observed source and exercise queue head/expiry handling,
legacy budget compatibility, denial behavior and wrapper idempotence.

No environment files, broker limits or configured risk amounts were changed.
No manifest allowlist was broadened to permit unknown files. The old deployment
installer still does not install the complete stock-fast change set and must not
be used as though it did. No whole-tree reset, checkout, bridge replacement or
service restart is authorized by a matching hash alone.

A differing run.py may simply be the reviewed base entry point, but the actual
hash/launch operand must be observed before deciding. PM2 bridge online is not
proof of a duplicate trading engine and is not a reason to stop PM2.

## One passive fresh check

Use tools/stock_fast_host_review.py from this reviewed commit, not the saved JSON.
The standalone standard-library tool requires no live-package imports and never
calls Angel One. It only reads fixed systemctl properties, selected process
flags, source hashes, read-only local position/intent counts and three fixed
loopback GET endpoints. It never calls resume, buy, sell, reconciliation or
other POST endpoints. Redirects and environment proxies are disabled for the
credentialed local request. Credentials, raw argv, raw environment and provider
error bodies are not emitted.

Run with python3 -I -B -S so the temporary review does not import local packages
or leave root-owned bytecode caches. The default prints a phone-sized summary;
--json prints the full sanitized observation. API availability is separated
from NOT_PUBLISHED, UNHEALTHY, HEALTHY_STALE and invalid timestamps. Unknown
reason strings are redacted rather than echoed. Missing database tables stay
UNKNOWN rather than becoming invented zero counts.

Even an all-healthy report is DIAGNOSTIC_ONLY_NOT_RELEASE_APPROVAL. Source
hashes identify on-disk bytes, not proof of in-memory module identity; the
summary is not an atomic broker snapshot. Obtain current broker/local position
and pending-order reconciliation through the established authorized workflow
before maintenance. Then perform the reviewed additive deployment, protect
existing positions and verify installed source, worker and dashboard behavior.
Real-money activation remains a separate operator-controlled action after
validation; this commit neither enables it nor places a test order.

## Validation of this amendment

31 new targeted offline tests passed locally (23 host-review and 8 preserved
FIFO tests). These are fixture/temporary-SQLite tests, not live AWS or broker
observations. Full repository CI for the amended commit must be checked
separately. Existing strategy thresholds, stock order routing, exit policies,
main/CAS branches and server configuration are unchanged by this amendment.
