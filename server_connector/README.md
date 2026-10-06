# KADIMA server diagnostics connector — 0.1.0

Prepared 6 October 2026. **Source implemented and locally tested; AWS deployment,
TLS routing, OAuth account connection and live tool discovery are not verified.**
This is an independent read-only connector. It does not fix CAS trading semantics,
grant an entry permit, enable execution, or prove that a future trade will fill.

## What is implemented

| MCP tool | Evidence |
|---|---|
| `get_server_status` | systemd service state, process switches, on-disk source hashes, API read failures, snapshot age |
| `get_strategy_readiness` | separate `cas`, `cas_legacy`, `gamma`, `index_scalp`, `stock_fast`, `move_capture` evidence |
| `get_execution_evidence` | bounded local positions/ledger/audit; strategy attribution by explicit kind or signal ID only |
| `get_recent_diagnostics` | bounded sanitized audit events and entry-block reasons |

`cas` means the newer CASLane; `cas_legacy` means legacy `cas_blast`. The newer
priority endpoint is not evidence of legacy CAS readiness. An unlabelled
`lane_permit_not_granted` event is not automatically a CAS rejection. A checkout
commit or file hash is not proof that the running process loaded those bytes.

There is deliberately no `READY` verdict: the existing APIs cannot prove all
current per-strategy permits, executable quotes, cash and exit protection. The
connector returns `BLOCKED` or `UNVERIFIED` plus source evidence, rather than
inventing readiness. Missing endpoint, log, strategy attribution, timestamp or
broker evidence remains missing. Empty local positions do not prove broker flatness.

## Isolation and privacy

The collector makes four **fixed GET requests** on `127.0.0.1:8081`, reads an
allowlist of process switches/source files, and the last 2 MB / 400 lines of
today's audit file. Up to 100 sanitized audit records are exported. It does not
import the trading package, query its SQLite database, call a broker SDK, or
accept caller-selected paths, commands, SQL or URLs. Existing engine GET handlers
may refresh their own caches or obtain read-only broker facts; they are not order
submission routes. The snapshot is not a full-day audit or fresh broker book.

The unprivileged MCP service reads only the sanitized snapshot. It cannot read
the engine state directory, dashboard API credential or Ubuntu home through its
systemd sandbox. It has no mutation tools. Only the local collector receives
the existing dashboard credential via systemd LoadCredential. The credential
is never written to the snapshot, plugin files, logs or repository.

Collector runs at most once per completed collection plus 30 seconds. A snapshot
older than 90 seconds or future-dated is stale. A failed endpoint replaces its
previous result with an error rather than silently reusing old success.
Memory limits are 96 MiB for the short-lived collector and 128 MiB for MCP;
actual AWS use still needs measurement. The engine and its dependency environment
are unchanged; connector dependencies use a separate pinned virtual environment.

## Authentication that still needs provisioning

Use an existing established OAuth 2.1 identity provider. No self-built password
server or reusable dashboard token is exposed to ChatGPT. The provider must:

1. Offer authorization-code + S256 PKCE, metadata/discovery, and a pre-registered
   ChatGPT client (or compatible registration flow).
2. Issue **RS256 access tokens** whose `aud` is exactly the chosen public resource
   URL (for example the actual deployed `https://your-host/mcp`, not the example
   hostname below), with `iss`, `exp`, `iat`, `sub`, `scope` and `client_id` or `azp`.
3. Grant `kadima:read` only to the authorized operator, with refresh tokens if
   persistent connection is desired. Disable public self-sign-up for this app.
4. Register the exact redirect URI shown by ChatGPT's connection setup; do not
   guess a callback URL. The provider handles sign-in/MFA and account recovery.

The server checks signature, issuer, audience, expiration, not-before when present,
scope, authorized subject and authorized client. ID tokens, absent claims and
wrong subjects/clients are rejected. JWKS retrieval is pinned to configuration,
HTTPS-only, no redirects, bounded to 512 KB / 4 seconds. No arbitrary token `jku`
or other caller-controlled key URL is followed.

Create `/etc/kadima-connector/oauth.json` as **root:root 0600**, using
`oauth.example.json` as the schema. Its URLs and subject/client IDs are non-secret,
but still keep this host configuration private. Example URLs/placeholders are
rejected. Never paste passwords, tokens or client secrets into chat.

Until a real provider/client and HTTPS route exist, the server cannot be connected
as a working ChatGPT plugin. The current KADIMA plugin is not modified by this
source release, and no placeholder plugin is published.

## Installation on the existing AWS host

Use an isolated export of the exact connector commit, not `git pull` in the live
engine checkout. Replace `CONNECTOR_COMMIT` with this PR's reviewed full commit SHA.

```bash
KADIMA_CONNECTOR_COMMIT=CONNECTOR_COMMIT
git -C /home/ubuntu/kadima-py fetch origin "$KADIMA_CONNECTOR_COMMIT"
KADIMA_CONNECTOR_STAGE=$(mktemp -d /home/ubuntu/kadima-connector.XXXXXX)
git -C /home/ubuntu/kadima-py archive "$KADIMA_CONNECTOR_COMMIT" server_connector |
  tar -x -C "$KADIMA_CONNECTOR_STAGE"
python3 "$KADIMA_CONNECTOR_STAGE/server_connector/install.py"
```

This prints a plan only. After OAuth configuration is present, installation is:

```bash
sudo -n python3 "$KADIMA_CONNECTOR_STAGE/server_connector/install.py" --apply
```

Installer targets only `/opt/kadima-connector`, `/var/lib/kadima-connector`, the
dedicated `kadima-reader` account, and three `kadima-connector*` systemd units.
It refuses to overwrite an existing connector installation. An incomplete install
must be inspected before retrying; it does not reset itself or touch trading.
It does not edit nginx, firewall, AWS security groups, existing service units,
live source files, positions or orders. Python 3.12+ and venv support are required.

Review `nginx-locations.conf.example` within the existing **HTTPS** server block.
The hostname and certificate must match `resource_url`. Keep dashboard login
rules on the dashboard; the two exact MCP/discovery routes use OAuth instead.
Do not open engine port 8081 or connector port 8766 to the internet. Run `nginx -t`
before reloading nginx. Reverse-proxy installation cannot be automatically
generated safely without reading the actual host configuration.

## Verification and connection

1. Check `systemctl is-active kadima-connector.service kadima-connector-collect.timer`.
   Inspect collector service result and snapshot timestamp. Collector is oneshot;
   being inactive after successful completion is expected.
2. An unauthenticated POST to the public `/mcp` must return **401** and a
   `WWW-Authenticate` challenge with the real protected-resource metadata URL.
   Discovery must return the configured resource, issuer and `kadima:read` scope.
3. Configure the custom MCP connection with the **verified** endpoint and complete
   OAuth in ChatGPT's secure sign-in flow. Discover exactly the four tools above.
4. Fetch server status plus both CAS readiness views. Compare them against the
   same-time server snapshot. Unavailable/stale evidence must remain explicit.
5. Only after verification, attach that connection to the existing KADIMA plugin
   using its owning source/release process. This release does not duplicate or
   overwrite the existing plugin's skills or identity.

For rollback, disable only the connector:

```bash
sudo systemctl disable --now kadima-connector.service kadima-connector-collect.timer
sudo systemctl stop kadima-connector-collect.service
```

Remove only the two added nginx locations after reviewing the config; validate
and reload nginx. Keep release files and snapshots for diagnosis. No engine
restart or automatic cleanup of trading state is part of rollback.

## Tests and source references

```bash
python3 -m venv /tmp/kadima-connector-test
/tmp/kadima-connector-test/bin/pip install -r server_connector/requirements.lock pytest==9.1.1
/tmp/kadima-connector-test/bin/python -m pytest -q server_connector/test_connector.py
```

Tests exercise actual MCP HTTP discovery, 401 protection, tool listing/calls,
rejected mutation tool names, token validation, stale evidence, explicit strategy
attribution, redaction and atomic snapshot permissions. No tests place orders or
contact AWS or Angel One. Installation and real identity-provider integration
remain untested until server access is available.

Reviewed interface sources:
- KADIMA main `c53365deddcca84ffee4484ef8246c14dc6cc450`
- CAS branch `0747318bc79556918083915f5b8863fefbcfbbbc` for optional newer routes
- https://developers.openai.com/plugins/build/auth
- https://py.sdk.modelcontextprotocol.io/run/authorization/

Source baselines are not assertions about the deployed host. This connector is
separate from the CAS product-identity, tick-precision, lane-exclusivity and
missing-timing-recorder repair work.
