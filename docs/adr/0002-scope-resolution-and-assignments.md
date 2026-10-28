# ADR 0002: Scope resolution, assignments and customer principals

- **Status:** Accepted, 2026-10-28. D7 and D11 were revised the same day, when O1 was
  answered. The open questions are answered (D12–D16). The proposals under "What
  follows" are for step 04 to confirm or refine.
- **Decided by:** Amit Kumar
- **Applies to:** step 04 (API skeleton and scope resolution) and the isolation cases
  it completes (1, 6 and 7)

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| D1 | What identifies a principal | The Entra object id (`oid`), never email or UPN |
| D2 | Who maintains assignments | A platform admin, in the operations console |
| D3 | Getronics staff scope | Named per tenant, or all tenants |
| D4 | Assignment removed | Access ends **immediately** |
| D5 | How customers sign in | As B2B guests in the Getronics Entra tenant |
| D6 | Entra roles for customers | Every customer user holds the same read-only Entra role. Customer roles are defined in the platform, not in Entra. |
| D7 | Customer principal to tenants | **Exactly one tenant.** A customer organisation *is* one platform tenant. Its subsidiaries' Azure subscriptions and AWS accounts are `cloud_account` rows under that tenant, not separate tenants. (Revised once O1 was answered.) |
| D8 | Permission matrix | Held and administered in the platform, per role. It is not a static table in the HLD. |
| D9 | Does "all tenants" (D3) include tenants onboarded later? | **Yes**, automatically (was O2) |
| D10 | Can the matrix grant commercial data (margin, rate card)? | The `commercial_visibility` database policy stays fixed in code, and the matrix can only **narrow** it. Customer roles can **never** be granted commercial data. (was O4) |
| D11 | Can a customer user compare their subsidiaries? | **Yes.** Subsidiaries are subscriptions within the user's own tenant, so this is an ordinary in-tenant query. (was O5, revised with D7) |
| D12 | What is a customer organisation? | One platform `tenant`, which may hold many subscriptions and accounts (was O1). No separate organisation entity. |
| D13 | Customer roles | None are predefined. Platform admins create them in the platform (was O3). |
| D14 | First platform admin | The platform creates one default admin (was O6). |
| D15 | Permission matrix lifecycle | The platform ships a default matrix, which admins can update, add to and delete from (was O7). |
| D16 | Access to some of a tenant's subscriptions | **Required.** An assignment can limit a user to a subset of the tenant's subscriptions and accounts (was O8). |

## What follows from them

- **Nothing about scope comes from the token.** Under D5 and D6, every customer's token
  carries the same Entra role, so the token cannot say which customer a user belongs
  to. The token proves identity (`oid`, issuer, audience, signature). Role and tenant
  come only from the platform's assignment data. This is the rule in CLAUDE.md, and
  the design makes it unavoidable.
- **Scope is resolved per request, from the database.** D4 rules out caching scope in
  a session, a cookie or a long-lived cache. A short-lived cache is acceptable only if
  revocation invalidates it synchronously. The default is to resolve on every request.
- **The step 04 brief's invariant stands (D7).** A customer principal resolves to
  exactly one tenant. Zero tenants, or more than one, is a defect and raises; it never
  degrades to a partial scope.
- **No schema change for organisations (D12).** `tenant` → `cloud_account` →
  `resource` already models one customer with many subscriptions.
- **Assignment data is read before any scope exists,** so it cannot sit behind the
  tenant RLS policy. It needs its own protection, for example a resolver function that
  takes only the authenticated `oid`, or a dedicated resolver role. Step 04 proposes
  the mechanism.
- **Every change to an assignment or a role permission is audited,** with the admin
  who made it, because these rows decide who sees which customer.
- **Onboarding a customer grants access** to every all-tenant user immediately
  (D9). Onboarding is therefore a privileged, audited action. All-tenant assignments
  should be few, named, and reviewed at the quarterly access recertification.
- **Commercial visibility has two locks (D10).** RLS decides the maximum, and the
  matrix can remove access but never add it. Removing a role from the
  `commercial_visibility` list is a migration, so it goes through review.
- **Isolation case 4 stands as the LLD wrote it.** A customer user is always
  single-tenant, so any comparison across tenants is refused. Comparing subsidiaries
  (D11) never crosses a tenant boundary.
- **Customer roles are data, so the database sees role classes (D13).** RLS cannot
  key on role names that admins invent at runtime. Proposal for step 04: `app.role`
  stays a fixed, code-defined set, the existing staff roles plus one `customer` class.
  Every admin-created customer role maps to the `customer` class; its finer
  permissions come from the matrix. A customer role is never in the
  `commercial_visibility` list, so D10 holds whatever an admin creates. Adding
  `customer` to `Role` turns the isolation stub for case 6 red, by design, until a real
  test replaces it.
- **The default admin has no password (D14).** Users sign in through Entra, so the
  "default admin" is one `platform_admin` assignment for an Entra `oid` supplied at
  deployment: from `.env.local` locally, from Key Vault in Azure. It is never written
  in code. Bootstrap is idempotent, audited, and refuses to run if any admin
  assignment already exists, so it cannot be used to add a second admin quietly.
- **The matrix cannot lock itself out (D15).** It ships with a default, seeded by
  migration, and admins can edit it, with every change audited and constrained by
  D10. Two rules prevent self-harm: the permission to manage assignments and the
  matrix cannot be removed from `platform_admin`, and the last `platform_admin`
  assignment cannot be deleted. Deleting a role that still has assignments revokes them
  immediately (D4), after an explicit confirmation.
- **Subscription limits are a second database boundary (D16), not a filter in the
  API.** Proposal for step 04:
  - an assignment optionally lists `cloud_account` ids, and an empty list means the
    whole tenant;
  - the tenancy package sets `app.account_ids` beside `app.tenant_ids`, the same way
    (`SET LOCAL` semantics, resolved server-side, never from the request);
  - tables carrying `account_id` (`cloud_account`, `resource`, `cost_fact`) get a
    restrictive `account_scope` policy, and `work_item` and `vulnerability` follow
    their `resource_id`;
  - each new policy ships with its isolation test (CLAUDE.md rule 1).
  - **Rows with no subscription** (an unlinked ticket, a vulnerability on an unknown
    host, a SOW) are hidden from subscription-limited users. A limited user sees only
    what is provably theirs; this fails closed.

## Resolved elsewhere

| # | Question | Why it matters |
| --- | --- | --- |
| O1a | **Resolved by ADR 0003:** stored app registration secrets and AWS access keys are allowed in non-production only, read-only, with guardrails. Production uses Lighthouse or GDAP and `AssumeRole` with an external ID. | ADR 0003 blocks the first production deployment until the exit steps are done. |

## Implementation (step 04)

| Piece | Where |
| --- | --- |
| Access schema, resolver and admin functions, default matrix, `account_scope` policies | `db/migrations/versions/0004_access_and_account_scope.py` |
| Account scope (`app.account_ids`), role classes `customer` and `unresolved` | `packages/tenancy` |
| Token validation | `services/api/src/cloudops_api/auth.py` |
| Scope resolution | `services/api/src/cloudops_api/access.py` |
| Pipeline: token, scope, matrix, handler, audit | `services/api/src/cloudops_api/pipeline.py` |
| BFF endpoints and views | `services/api/src/cloudops_api/bff.py` |
| `Metric` and the provenance rule | `services/api/src/cloudops_api/provenance.py` |
| Default admin (D14) | `python -m cloudops_api.bootstrap` |

The "What follows" proposals are implemented as written, with these choices made
along the way:

- **Infrastructure prerequisite (Azure).** Two NOLOGIN roles own the access functions:
  `cloudops_resolver` and `cloudops_access_admin`. The migration role must be a member
  of both, `WITH INHERIT FALSE`, so it can hand over ownership without gaining their
  rights. They are created by `db/bootstrap/roles.sql`, run once per server by its
  administrator: locally by `infra/local/postgres/init.sh`, and in Azure by the
  Entra administrator, as the README's "Azure database bootstrap" section describes.
  The migration checks for them and stops if they are missing.
- **A subscription restriction applies to a single-tenant principal only.** One flat
  `app.account_ids` list cannot express "these subscriptions of tenant A, and all of
  tenant B", so the resolver raises a defect for a restricted principal with more than
  one tenant.
- **What subscription limits cover.** `cloud_account`, `resource`, `cost_fact` and
  `resource_quarantine` filter by account. `work_item` and `vulnerability` follow their
  resource, and unlinked rows are hidden. Tenant-level tables (`tenant`,
  `tenant_alias`, `sow`, `knowledge_chunk`, `audit.action_log`) are **not**
  subscription-scoped, so a limited user still sees the tenant's knowledge base.
  Revisit when the knowledge module lands, if documents can belong to one subsidiary.
- **Audit volume.** `action_log.tenant_id` is required, so a request is logged once per
  tenant it could reach, or once for the tenant a detail endpoint actually touched.
  For an all-tenant user a list request writes one row per tenant. That is correct but
  grows with tenant count; revisit before the tenant count reaches the hundreds.
- **Attempts with no tenant are not in `action_log`.** A rejected token or an unknown
  principal has no tenant to log under, so these go to the application log only, as
  structured warnings with a request id. The LLD's always-page alert on "any
  cross-tenant query attempt" needs a security event sink. Until one exists, a probe
  for another tenant's resource is in `action_log` as `failed` under the caller's own
  tenant.
- **Default matrix (D15).** "Customer viewer" includes `cost.read`, so customers see
  their own cloud spend. The product owner should confirm this, along with the rest of
  the defaults in migration 0004.
- **Local tokens** come from a dev issuer whose signing key is in `.env.local`
  (`make token`). Entra never issues for that issuer, tenant or audience, so a dev
  token is useless against Azure.
