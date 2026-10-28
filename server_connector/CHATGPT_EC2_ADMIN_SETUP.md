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

Keep the repository private. GitHub Pro or Team alone does not provide required
environment reviewers for private repositories:
https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments

Before activation, establish a supported, enforceable approval arrangement.
Native required reviewers for a private repository require a supported Enterprise
arrangement; alternatively, a separately reviewed external approval mechanism
must enforce approval before AWS credentials or SSM execution are available.
Do not replace approval with a workflow input, a comment, or an unenforced
environment declaration. If no supported gate is available, leave maintenance
disabled and do not dispatch.

In GitHub → Settings → Environments, configure `kadima-production-admin`
to allow only the branch `main`, with no tag patterns. Configure the supported
approval gate, prevent self-review and disable administrator bypass where
available. Protect `main` with enforced pull-request review, required CI checks,
stale-approval dismissal and conversation resolution; disallow force pushes
and deletion, and apply protections to administrators. Protect workflow,
connector and IAM template changes with required code-owner review by an
eligible independent reviewer. CODEOWNERS alone is not enforcement.
Verify the live settings and gate behavior before merging or dispatching.

Set **environment variables** (not secrets):

- `KADIMA_EC2_INSTANCE_ID`: `i-0e8039442bc5c80c3`
- `KADIMA_AWS_REGION`: `ap-south-2`
- `KADIMA_AWS_OIDC_ROLE_ARN`: exact current CloudFormation `RoleArn` output

Do not paste access keys, dashboard tokens, broker credentials, or SSH private
keys into ChatGPT or GitHub. Verify AWS account ID and instance ID in console.

## Manual first-run verification

After merging a reviewed workflow into `main` (the workflow now refuses to run on other refs), and after configuring the protected GitHub environment with approved deployment branches and reviewers:

1. GitHub → Actions → **KADIMA guarded EC2 operations** → Run workflow.
2. Select `status` and enter a reason. An environment reviewer approves.
3. Verify the run displays the expected hostname-independent system metrics,
   the active KADIMA service status, and deployed Git commit SHA.
4. For this acceptance test, run only `status`. Do not run
   `connector_status`, `connector_restart`, or trading operations.
5. Record the pending gate, independent approval, successful OIDC authentication,
   AWS identity, SSM command ID, target, complete output, and response code.
   Require SSM Success and response code 0, with no masked diagnostic errors.
6. Compare the deployed role trust, inline/attached policies, permissions
   boundary, and current SSM document with the reviewed template before
   activation. Confirm no additional policy grants generic shell access.
7. The current template masks service and Git lookup failures with `|| true`.
   A zero response code does not prove these checks worked. Resolve the known
   Git ownership failure through a reviewed document change before acceptance;
   preserve trading controls and do not globally disable Git ownership checks.
8. Separately prove an authenticated ChatGPT MCP read of current diagnostics
   and rejection of unauthenticated/invalid-token requests. GitHub maintenance
   is complete only after its own approved OIDC-to-SSM test succeeds.

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
