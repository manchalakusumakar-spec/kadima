# Separate ChatGPT read/write integration

## Scope

This adds a new IAM role `KadimaChatGPTApprovedOps`, two new fixed SSM documents,
and a new workflow/environment `kadima-chatgpt-ops`. It does not update the
existing connector stack, role, provider, GitHub variables, instance profile or
Claude configuration. Sharing the engine means an approved code deployment
changes the code Claude also sees; separate credentials do not isolate runtime
code. No deployment should overlap a Claude maintenance session.

The initial capabilities are read-only status and deployment of one exact,
reviewed current `main` commit. Deployment writes the existing checkout and
restarts `kadima.service`; it may interrupt the engine. It is not unrestricted
shell access, arbitrary file editing or permission to change trading controls.
Use a maintenance window with no open positions and obtain explicit approval of
the commit and restart before each deployment. Code changes are reviewed in PRs
first. Routine diagnostics use `status` and do not restart anything.

## Deploy the integration

1. Review and merge this PR after checks pass. This installs no AWS resources.
2. Create a **new** GitHub environment `kadima-chatgpt-ops`. Require reviewer
   `manchalakusumakar-spec`, allow owner approval, disable administrator bypass,
   and allow only branch `main` with no tags. Never edit `kadima-production-admin`
   or its variables. The new workflow uses no shared environment variables.
3. In AWS Hyderabad (`ap-south-2`), create a **new** CloudFormation stack
   `Kadima-ChatGPT-Ops` from `server_connector/chatgpt-approved-ops.yaml`.
   Keep the provider ARN parameter pointing to the existing no-slash GitHub
   provider. Review the change: one new IAM role and two new SSM documents only.
   Acknowledge named IAM creation. Never update/import the old connector stack.
4. Confirm `CREATE_COMPLETE`. Dispatch **KADIMA ChatGPT approved EC2 operations**
   on `main`, operation `status`, blank commit SHA, and a ticket/reference.
   Approve the separate environment gate. Verify actual OIDC claims, exact role
   identity, SSM command ID, service state, checkout SHA and response code zero.
   This is the first acceptance check; do not start with `deploy`.

## Approve a code deployment

1. Complete a specific user-requested code PR, review its diff and CI, and merge
   it to protected `main`. Confirm that no concurrent Claude maintenance is active.
2. Obtain the owner's explicit approval for the exact commit, engine restart and
   maintenance window. Dispatch operation `deploy` with that 40-character SHA
   and reference, then have the owner review the environment deployment request.
3. The SSM document fails closed unless the existing checkout is clean and on
   `main`, the SHA is the current remote `main`, and the update is a fast-forward.
   It rejects common dependency, service and deployment file changes, which
   need separate installation/review. It does not install dependencies.
4. Verify the command response and deployed SHA, then separately check live-data
   freshness, data quality and application readiness. An active systemd service
   alone does not prove engine health or live readiness.

If restart fails after the fast-forward, the checkout remains updated. The
workflow reports failure and does not automatically reset files or repeat the
deployment. Inspect the logs and approve a specific recovery. Record the previous
SHA printed by the document. Dependency/infrastructure changes and dirty or
non-main checkouts require a separate reviewed deployment path.

## Security and unverified assumptions

The new trust subject uses immutable owner ID `318973916`, repository ID
`1369670503`, and environment `kadima-chatgpt-ops`. The repository was created
2026-09-14; GitHub documents immutable subjects for repositories created after
2026-07-15. The workflow prints only public `iss`, `aud`, `sub` claims and verifies
them before AWS authentication; it never prints tokens. If claims differ, stop
and correct the exact new trust policy rather than introducing wildcards.

SSM SendCommand is limited to the two new documents and instance
`i-0e8039442bc5c80c3`. GetCommandInvocation requires resource `*`. No generic
AWS-RunShellScript, IAM administration or instance-profile changes are granted.
The deploy document requires SSM ENV_VAR parameter interpolation; an old agent
fails closed. The fixed checkout `/home/ubuntu/kadima-py`, checkout owner `ubuntu`,
service name `kadima.service`, clean main checkout and live health still require
EC2 verification. Neither these source checks nor CI establish deployment.

This GitHub channel does not by itself create a persistent ChatGPT MCP plugin.
That connection remains a separate integration step. Write operations should
remain manually approved; no unattended write automation is configured here.

## Rollback of integration

Disable the new workflow and remove the new environment. Delete only the new
`Kadima-ChatGPT-Ops` stack if appropriate. Leave the providers and existing stack
intact. This revokes the new access; it does not undo any approved engine commit.

Reference: https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws
