# KADIMA ChatGPT + EC2 integration: operator setup and security boundary

Status: **source staged only; NOT deployed, connected, or production verified**.

This branch extends the existing 0.2.0 **read-only** MCP server in
`server_connector/` and adds an independent GitHub Actions/SSM maintenance
channel in `.github/workflows/kadima-guarded-ec2.yml`.

## Important design decisions

- The existing MCP service stays read-only, unprivileged, and unable to invoke shell.
- Broad administration must not mean arbitrary root commands from model output.
  Use separately reviewed, narrowly scoped operations with human approval.
- No operation can submit broker orders, override Bull Shield, modify stops,
  change entry permits, disable kill switches, or change trading limits.
- This workflow currently supports only `status`, `connector_status`,
  `connector_restart`. Trading service restart, deployment, rollback,
  configuration edits, and shell execution are deliberately **not enabled**.
- Review the security model before expanding administrative operations.

## AWS prerequisite: EC2 Systems Manager

In the AWS console, identify the exact KADIMA EC2 **instance ID** and **region**.
Ensure the instance appears as **managed/online** in Systems Manager Fleet
Manager. The instance profile should grant `AmazonSSMManagedInstanceCore`
(or equivalent restricted SSM agent permissions). No public SSH ingress is
needed for SSM.

Create an IAM OIDC provider for `https://token.actions.githubusercontent.com`
with audience `sts.amazonaws.com` **only if it does not already exist**.
This is an account-wide IAM resource; avoid duplicates.

In AWS CloudFormation (region `ap-south-2`), create a stack from
`server_connector/kadima-guarded-ssm.yaml`. Its parameters are:

- `GitHubOIDCProviderArn`: the existing or newly created GitHub OIDC provider ARN.
- `KadimaInstanceId`: `i-0e8039442bc5c80c3` (verify exact instance in EC2).

Review the template and acknowledge named IAM capabilities if requested.
The stack creates `KadimaGuardedOps` (a fixed-operation SSM document) and a
dedicated GitHub OIDC IAM role. The role trusts only:
`repo:manchalakusumakar-spec/kadima:environment:kadima-production-admin`,
with audience `sts.amazonaws.com`. Its `ssm:SendCommand` permission is
restricted to **both** the exact EC2 instance and the custom SSM document;
`AWS-RunShellScript` is **not allowed** to this role. The custom document
accepts only three enumerated operations. Its shell script and CloudFormation
template are security-sensitive: protect their changes and stack-update rights.

Read the stack's `RoleArn`, `Region`, and `InstanceId` outputs for the
GitHub environment variables. Do not create IAM access keys.

In GitHub → Settings → Environments, create
`kadima-production-admin`. Configure required reviewers and prevent
self-approval if supported. Restrict deployment branches to the approved
branch. Protect workflow edits with CODEOWNERS/branch protection; review
changes to workflow, IAM policy and connector before enabling.

Set **environment variables** (not secrets):

- `KADIMA_EC2_INSTANCE_ID`: `i-...`
- `KADIMA_AWS_REGION`: actual EC2 region, e.g. `ap-south-2`
- `KADIMA_AWS_OIDC_ROLE_ARN`: ARN of the dedicated role

Do not paste access keys, dashboard tokens, broker credentials, or SSH private
keys into ChatGPT or GitHub. Verify AWS account ID and instance ID in console.

## Manual first-run verification

After merging a reviewed workflow into `main` (the workflow now refuses to run on other refs), and after configuring the protected GitHub environment with approved deployment branches and reviewers:

1. GitHub → Actions → **KADIMA guarded EC2 operations** → Run workflow.
2. Select `status` and enter a reason. An environment reviewer approves.
3. Verify the run displays the expected hostname-independent system metrics,
   the active KADIMA service status, and deployed Git commit SHA.
4. Run `connector_status` to check the separately installed MCP connector.
5. Only if necessary and approved, run `connector_restart`. This restarts
   **only** the MCP connector, not the trading engine.
6. Review the AWS SSM command ID and CloudTrail history.

A green CloudFormation `CREATE_COMPLETE` is not evidence that SSM has executed successfully or that ChatGPT is connected. The GitHub integration available to this assistant cannot create GitHub environments, set environment variables, dispatch new workflows, or inspect AWS CloudFormation outputs directly; those account-owner steps must be performed in the respective consoles. Do not skip approval-gate configuration.

If `send-command` fails, inspect SSM managed-node connectivity, OIDC role
trust, region, document permissions, and instance identity. Do not broaden
the role to administrator or add wildcard resource permissions as a shortcut.

## ChatGPT MCP connection — separate setup

The existing `server_connector/README.md` and `COGNITO_SETUP.md` document
the read-only MCP service, sanitized 10-day audit archive, isolated installer,
nginx/TLS routing, and Cognito OAuth. Review those files before installation.

**Critical compatibility checkpoint:** the current verifier requires an
RS256 access token whose `aud` equals the exact `https://HOST/mcp` URL.
The existing Cognito template does not prove Cognito will issue that audience.
Verify OAuth discovery, PKCE callback, actual access-token claims, and MCP
authentication in a staging environment. Do **not** remove audience, issuer,
subject, or client checks to make login succeed. If unsupported, use a
compatible identity provider or redesign the verified audience policy after
security review.

ChatGPT plugin installation/connection requires an actual reachable HTTPS
endpoint, working OAuth, and explicit user authorization. A GitHub PR alone
does not connect ChatGPT to EC2. No connector or AWS configuration has been
installed by this change.

## Deployment sequence

1. Review this PR and the existing connector code, including the Cognito
   audience issue, host IP stability, TLS, resource use and token redaction.
2. Configure AWS SSM and the restricted GitHub OIDC role.
3. Configure the protected GitHub environment and run `status`.
4. Stage/test the read-only connector on EC2 **outside market hours**,
   using the connector's isolated installer; do not update the live trading
   checkout or restart trading services.
5. Configure TLS and OAuth, test a 401 for unauthenticated MCP requests, then
   connect the endpoint through ChatGPT's supported custom app/plugin flow.
6. Test each read-only MCP tool and audit-data completeness.
7. Only after proven connectivity, design a separate approval-gated
   administrative API with explicit operation IDs, operator approval,
   command output sanitization, audit trails and rollback. Never expose a
   generic `run_shell` tool.

## Rollback

Disable the GitHub workflow or revoke the dedicated OIDC role to stop SSM
maintenance commands. To disable the separate MCP connector, follow the
existing `server_connector/README.md` rollback. Neither should touch
broker positions, trading engine, or risk-control settings.
