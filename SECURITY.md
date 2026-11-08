# Security policy and deployment boundary

This is a **single-user, single-account** read-only server. A trusted process operator supplies Trading 212 credentials. Every successful remote tool call represents that configured account; OAuth is not account selection or multi-tenant onboarding.

## Remote access

- Keep HTTP in its default OAuth mode. Configure a managed issuer, the exact resource audience, read scope and immutable owner subject. Other users' otherwise valid tokens must be denied.
- Require HTTPS at the managed ingress. Keep the container port private. Do not trust a user-supplied identity header or substitute a shared API key/browser-only proxy login for OAuth.
- `local` authentication mode is solely for an explicitly loopback-bound development process. Never expose it through a public reverse proxy/tunnel. A stdio Secure MCP Tunnel must instead be restricted at the authenticated OpenAI organization/workspace/plugin boundary; do not assume an entire shared workspace is the account owner.
- Metadata and health probes are public and contain no account data. All `/mcp` methods, including discovery/initialization, require a valid token in OAuth mode. Only exact configured hosts/browser origins are accepted.
- RS256 JWT access tokens are required. Issuer, signature, audience, expiration, subject and scope are checked. Keys are fetched only from the configured HTTPS endpoint, with bounded refreshes. Revocation/key rotation is not instantaneous: cached keys can remain valid for five minutes, and tokens expire according to the issuer. Use short-lived access tokens and manage refresh/revocation at the issuer.

## Secrets and data

Use the minimum Trading 212 read permissions for account, portfolio, orders and histories. Do not grant trading permissions. Production HTTP credentials come only from process environment/managed secrets. Local ignored dotenv files are a development convenience; they are excluded from container builds and must not be committed, attached to issues, or exported into screenshots.

The Docker image contains code and locked dependencies only. Do not inject credentials during build or publish a filled Compose/cloud configuration. Ignore `.env*`, databases and local caches in version control; scan both the intended publication set and Git history before an initial public push. If a real secret has been disclosed, revoke/rotate it at its provider rather than merely deleting a file.

Public market/identifier caches contain no account responses, positions, report snapshots, OAuth tokens or Trading 212 credentials. Optional persistence uses SQLite and versioned JSON, never pickle. Treat mounted volumes as trusted application data: restrict ownership/access to UID 10001, backups and retention. Do not share caches/reports across users. The app probes configured storage during HTTP startup but does not change volume ownership as root.

Logging is deliberately allowlisted. Raw request/response bodies, token/header/query values, exception text, account identifiers, symbols and financial amounts are omitted. Configure cloud ingress, IdP, monitoring and support tooling separately to avoid collecting them. Research results and report IDs can still be sensitive even though they are not secrets themselves.

## Operational limits

Run one worker/replica. Existing provider/account quotas and mapping/report caches are process scoped. There is no user onboarding, cross-user isolation layer, distributed limiter or shared-session service. Keep request/body/memory limits at the platform edge and allow only required outbound provider/IdP HTTPS destinations.

Read-only operations can reveal private financial information. Review tool calls and the context shared with ChatGPT as carefully as any other financial-data integration. Market-data access/redistribution rights are separate from software rights; private cloud hosting does not itself grant Yahoo/FMP licensing permission.

The service handles graceful shutdown, but a platform termination deadline can interrupt in-flight work. Synchronous SDK work is not always cancellable. Readiness certifies application/storage startup, not provider availability or valid broker credentials. Validate a demo account and actual OAuth flow before live use.

## Reporting vulnerabilities

Do not file public issues containing exploit tokens, keys, account dumps or portfolio results. Once a GitHub repository is configured, enable private vulnerability reporting and use its Security advisory channel. No maintainer email or external reporting URL has been invented for this private checkout. The repository owner must publish a contact/process before public release.

## Verification

`tests/test_http.py` covers signed JWT validation, wrong identities/audiences/scopes, expiry, forgery, JWKS outages, unauthorized MCP rejection, host/origin guards, shared runtime and log redaction. `tests/test_disk_cache.py` covers persistence, precision, TTLs, namespaces and corrupt-cache fallback. CI also checks example secrets, fail-closed settings, Docker build inputs, non-root execution and container startup/shutdown.
