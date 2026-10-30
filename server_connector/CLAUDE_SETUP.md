# Connect Claude to the KADIMA server (read-only)

Prepared 17 October 2026 on branch `claude/server-connector-for-claude`.
Builds on the Codex connector from 6 October; nothing in the trading engine changes.

**What you get:** in any Claude chat (phone, web or desktop) Claude can read
live engine status, strategy readiness, positions/ledger evidence and the last
10 days of sanitized audit log, including "why didn't CAS trade" summaries.

**What it can never do:** place, modify or cancel orders, change settings,
restart services, run commands or read credentials. The MCP service runs as a
separate locked-down user that cannot even see `/var/lib/kadima`.

Do every step **after 15:45 IST or on a holiday**. Allow about 45 minutes.

## Step 0 — Prerequisites (one-time)

1. **Elastic IP.** EC2 → Elastic IPs → Allocate → Associate with
   `KADIMA-BRIDGE`. Your address currently changes on every stop/start; the
   connector's URL is baked into its sign-in tokens, so it must not change.
   Afterwards update `session.py`'s `clientPublicIp`, re-issue the sslip.io
   certificate for the new name, and check nginx. Note the new hostname, e.g.
   `18-61-x-y.sslip.io`. The connector URL will be `https://<that host>/mcp`.
2. **Rotate `KADIMA_API_TOKEN`** (flagged as exposed). The collector reads it
   locally from `/etc/kadima-dashboard/config.json`; it never leaves the box.
3. Check versions — send Claude the output if anything looks off:
   ```bash
   /usr/bin/python3 --version     # must be 3.12 or newer
   free -m; df -h /               # need ~300 MB disk free for the archive
   ls -l /etc/kadima-dashboard/config.json
   ```

## Step 1 — Create the sign-in (AWS Cognito, Hyderabad region)

1. AWS console → **CloudFormation** (region ap-south-2) → Create stack →
   upload `server_connector/cognito-template.json`.
2. Parameters: `LoginDomainPrefix` = something unique like `kadima-mk-login`;
   leave `ChatGPTRedirectUri` **empty** unless you also connect ChatGPT.
3. Create. From **Outputs** copy `ClaudeClientId` and `UserPoolId`.
4. Cognito → that pool → Users → **Create user** (your username, a strong
   password). On first sign-in you'll set up an authenticator app code (MFA).
5. Copy your user's **sub** (a UUID on the user's page).

## Step 2 — Write the connector config on EC2

```bash
sudo mkdir -p /etc/kadima-connector
sudo tee /etc/kadima-connector/oauth.json > /dev/null << 'EOF'
{
  "resource_url": "https://YOUR-HOST.sslip.io/mcp",
  "issuer_url": "https://cognito-idp.ap-south-2.amazonaws.com/YOUR_USER_POOL_ID",
  "jwks_url": "https://cognito-idp.ap-south-2.amazonaws.com/YOUR_USER_POOL_ID/.well-known/jwks.json",
  "read_scope": "kadima/read",
  "allowed_subjects": ["YOUR_COGNITO_SUB"],
  "allowed_client_ids": ["YOUR_CLAUDE_CLIENT_ID"]
}
EOF
sudo chown root:root /etc/kadima-connector/oauth.json
sudo chmod 600 /etc/kadima-connector/oauth.json
```
Replace the four `YOUR_…` values. None of them are secrets, but keep the file private.

## Step 3 — Install the connector (separate from KADIMA)

```bash
cd ~/kadima-py
git fetch origin claude/server-connector-for-claude
STAGE=$(mktemp -d ~/kadima-connector.XXXXXX)
git archive FETCH_HEAD server_connector | tar -x -C "$STAGE"
python3 "$STAGE/server_connector/install.py"            # plan only, changes nothing
sudo -n python3 "$STAGE/server_connector/install.py" --apply
```
This does not `git pull` or touch your live engine checkout. It creates only
`/opt/kadima-connector`, `/var/lib/kadima-connector`, user `kadima-reader`
and three `kadima-connector*` units. KADIMA is not restarted.

Check: `systemctl is-active kadima-connector.service kadima-connector-collect.timer`
should print `active` twice, and `sudo ls -l /var/lib/kadima-connector` should
show `snapshot.json` and a `days` folder within a minute.

## Step 4 — Expose `/mcp` through nginx

```bash
sudo cp /etc/nginx/sites-available/kadima ~/nginx-kadima.bak.$(date +%F-%H%M)
sudo nano /etc/nginx/sites-available/kadima
```
Paste the two `location` blocks from `server_connector/nginx-locations.conf.example`
**inside the existing HTTPS (`listen 443`) server block**, next to the
existing `location /control/` block. Then:
```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://YOUR-HOST.sslip.io/mcp   # expect 401
```
(Pasting into nano is fine for nginx config; the "no nano" rule is for Python files.)

## Step 5 — Add it to Claude

claude.ai (web is easiest) → **Customize → Connectors → + Add → Add custom connector**:
- Name: `KADIMA Server`
- URL: `https://YOUR-HOST.sslip.io/mcp`
- OAuth client: **Use your own OAuth client** → Client ID = `ClaudeClientId`,
  client secret **empty**.

Sign in with your Cognito user and authenticator code. Then in a new chat ask
"KADIMA server status". Claude should see 7 tools.

## Rollback (connector only, trading untouched)

```bash
sudo systemctl disable --now kadima-connector.service kadima-connector-collect.timer
```
Remove the two nginx `location` blocks, `sudo nginx -t && sudo systemctl reload nginx`,
and remove the connector in Claude's settings.

## Known risks to watch on first connection

- **Sign-in metadata.** Claude requires S256 PKCE. Cognito supports it, but if
  Claude reports "Authorization with the MCP server failed", send Claude the
  error and the output of
  `curl -s https://cognito-idp.ap-south-2.amazonaws.com/YOUR_USER_POOL_ID/.well-known/openid-configuration`.
- **Token audience.** Access is granted only if Cognito binds the token to the
  exact `/mcp` URL. If tools return 401 after a successful sign-in, that is
  the cause; do not loosen the check, send Claude the connector log
  (`journalctl -u kadima-connector -n 50`).
- **Claude Code** (terminal) cannot use this Cognito client, because Cognito
  can't match its random loopback port. The Claude app on phone, web and
  desktop all work.
- **Disk.** The archive keeps 10 days, at most 25 MB a day, and pauses itself
  below 1.5 GB or 10% free disk.
