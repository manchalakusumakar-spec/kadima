# AWS sign-in for the read-only connector

AWS root + password + Google Authenticator is the AWS console sign-in, not an
OAuth provider for KADIMA. Never give root credentials or its TOTP seed to the
plugin. A dedicated Cognito user pool supplies a separate application login.
Its authenticator factor can be a separate entry in the same authenticator app.

## Prepared, not provisioned

`cognito-template.json` creates only a dedicated Cognito pool, read-only custom
scope, public OAuth client, managed-login domain and default login branding.
It creates no IAM roles and grants no AWS administration access. It changes no
EC2 instance, trading service or broker configuration. Cognito Essentials usage
is billed under the account's applicable pricing. No AWS resources have been
created by this source change.

Public self-sign-up is disabled. Admin-created operator accounts require a
password and authenticator MFA. Recovery is admin-only. The public client permits
only authorization-code flow; ChatGPT must use S256 PKCE. No client secret is
generated. Access tokens last five minutes, refresh tokens seven days. Local JWT
verification cannot immediately detect token revocation; removing the allowed
subject and restarting only the connector blocks access.

## Setup sequence

1. Establish the intended HTTPS `/mcp` endpoint and obtain the **exact OAuth
   callback URI** shown by the ChatGPT connection setup. It may be callback-specific;
   do not guess the legacy stable URI. If the host cannot show it before discovery,
   resolve that connection flow before creating the client; do not use a wildcard.
2. In AWS CloudFormation, Hyderabad / ap-south-2, upload `cognito-template.json`.
   Supply that callback and an available Cognito login-domain prefix. Review the
   change set before resource creation. Retain/deletion-protection settings mean
   deleting the stack does not delete the operator pool.
3. Create your operator account in that Cognito pool using the AWS console,
   without an email invitation. Complete initial password and TOTP enrollment
   through Cognito's sign-in page. Keep passwords/codes in sign-in prompts.
   This is a separate MFA registration from the existing AWS root factor.
4. Inspect the pool's discovery metadata for its exact issuer, JWKS, authorization
   and token endpoints, and S256 PKCE support. Use the discovered issuer, not the
   managed-login domain or a guessed pool URL.
5. Fill `/etc/kadima-connector/oauth.json` with the real resource URL, discovered
   issuer/JWKS URLs, `read_scope: "kadima/read"`, the public app-client ID and the
   operator's exact `sub` UUID from Cognito. Keep root:root 0600.
6. Follow README installation steps. Configure ChatGPT with the pre-registered
   public client ID and no client secret. Authenticate in the secure OAuth prompt.
   The authorization request must include `resource=<exact HTTPS MCP URL>` and
   scope `kadima/read`. Cognito resource binding places this URL in access-token
   `aud`; without that exact audience the connector rejects access. Refresh must
   retain the same audience.
7. Verify unauthenticated 401, discovery, authorized tools/list and timestamped
   server-status output. Report any connection/PKCE failures. Do not fall back to
   unauthenticated access or accept ID tokens to make the connection appear ready.

The template still needs live CloudFormation validation and a complete hosted
OAuth round trip. Local tests verify scope handling and security properties; they
do not establish that an AWS pool, account or ChatGPT connection exists.

Official references checked 6 October 2026:
- https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html
- https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html
- https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-client-apps.html
- https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-cognito-userpool.html
- https://developers.openai.com/plugins/build/auth
