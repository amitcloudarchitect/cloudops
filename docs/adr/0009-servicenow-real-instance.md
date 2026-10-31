# ADR 0009: Reading a real ServiceNow instance

- **Status:** Accepted, 2026-10-29
- **Decided by:** Amit Kumar
- **Implements:** LLD readiness gate 3 (source credentials) and the Connector
  contract for ServiceNow. Code in `connectors/src/cloudops_connectors/servicenow`
  (`auth.py`, `client.py`, `__main__.py`), `cloudops_config` (the `SERVICENOW`
  component), `tools/servicenow_record.py`, `connectors/tests/recorded/`, and stacks
  04 and 04b.

## Decisions

1. **OAuth 2.0 client credentials, not Basic auth.**
   - Basic auth was refused on the dev instance's release.
   - Client credentials need an instance property
     (`glide.oauth.inbound.client.credential.grant_type.enabled`) and an OAuth client
     bound to the integration user.
   - The client ID and instance URL are configuration. The client secret is a Key
     Vault secret, `servicenow-read-client-secret`, mounted as a file and read on every
     token request, so a rotated secret needs no restart.
   - Tokens are cached until a minute before expiry. A 401 refreshes once and retries.
     Neither the secret nor the token appears in any log or error.
2. **The integration account is read-only, and that is verified, not assumed.**
   - `svc_cloudops_read`: machine identity, internal integration user, roles `itil`,
     `cmdb_read` and `snc_read_only`.
   - A test write on 2026-10-29 was refused (403 ACL).
   - The OAuth client is auth-scoped (2026-10-31), replacing the first client, which
     was useraccount-scoped (its token could do whatever the account could):
     - Client `svc_cloudops_read_scoped` (client ID in the dev overlay) carries only
       the auth scope `cloudops_read`.
     - A REST API Auth Scope record requires `cloudops_read` on the Table API.
     - The connector asks for no scope and gets what the client grants, so the change
       needed no code.
     - Table-level limits still come from the account's read-only roles. An auth
       scope limits which API a token may call, not which tables.
     - Not yet on: "Enforce token restriction", which needs a REST API Access Policy
       for the Table API. It is a second layer, for before UAT.
3. **The integration user's time zone must be GMT, enforced at start-up.**
   - Encoded-query date-times are read in the user's time zone, and watermarks are
     UTC.
   - `check_integration_user` refuses an account in any other zone, or with none set,
     before anything is read.
4. **Contract fixtures come from the real instance, scrubbed.**
   - `tools/servicenow_record.py` records one page per entity with the platform's own
     client.
   - Before writing, it replaces every person reference with `Person N`, replaces long
     free text, and removes email addresses (anywhere) and phone numbers (in kept free
     text only, since the pattern would match date-times).
   - Tests prove every real record normalises, and that the recordings carry no email
     address, person name or long free text.
5. **One bronze container per source** (`servicenow` in the bronze account), so the
   ingest identity's write role is scoped to its own container.

## Consequences

- The worker can run in Azure once Temporal has mTLS (ADR 0008 open item). Its base
  manifest already carries the ServiceNow settings and the mounted secret. *Done in
  ADR 0010: it runs in dev, scheduled every 5 minutes per entity.*
- The dev instance is a personal developer instance: it hibernates when idle and is
  reclaimed after long inactivity, so a reclaimed instance means a new account, OAuth
  client and recording.
- The instance sends no `X-RateLimit-*` headers. The client then never pauses on quota,
  and still waits out a 429 (30 seconds without `Retry-After`).
