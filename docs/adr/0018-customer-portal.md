# ADR 0018: The customer portal

- **Status:** Accepted, 2026-10-31
- **Amended by:** ADR 0021: `cost_fact`'s cost columns are Getronics' cost, revoked from
  `cloudops_customer`; the cost permission now shows a customer the price it is charged.
  ADR 0030: every role lands on its home dashboard, led by its D4 landing view; CXO and
  VP or director read their published report packs.
- **Decided by:** Amit Kumar
- **Applies to:** step 18 (HLD M2 customer onboarding, M22 customer portal and identity)
- **Implements:** `db/migrations/versions/0011_customer_portal.py`, `db/bootstrap/roles.sql`,
  `packages/tenancy` (customer database role), `services/api/src/cloudops_api/portal.py`,
  the console's `/performance`, `/posture`, `/approvals`, `/users` and `/onboarding`,
  stack `06-edge` (`portal.tf`), `docs/customer-portal-federation.md`.
- **Amends:** ADR 0002 D2, for customer principals only (D2 below).

## Context

Customers use the same application as Getronics staff (HLD, M22: "building a second
front end for customers duplicates every view and guarantees divergence"). The brief
says customers must never see margin, cost base, rate card, the FTE model, effort
assumptions, other customers' data or internal commentary, and that this is enforced
by the database, not by hiding columns in the UI or filtering in the API.

Row-level security already decides which rows a customer sees: its own tenant only
(ADR 0002, D7), and no SOW row (`commercial_visibility`). But RLS cannot hide a
column, and some rows a customer may see carry fields it may not: a ticket's planned
and actual hours (the effort model), its Getronics assignee and workgroup, a
resource's raw source payload. The HLD and LLD do not say how to enforce that, which
landing view each customer role has, or how the onboarding steps are approved. These
were asked and decided as below.

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| D1 | How the database refuses a column | A customer scope runs as its own database role, `cloudops_customer`. `packages/tenancy` issues `SET LOCAL ROLE cloudops_customer` in every transaction whose role class is `customer`. The role has column-level `SELECT` grants on what a customer may see, and nothing on commercial, internal or ingest tables. |
| D2 | Who administers customer users | Platform administrators still maintain every assignment (ADR 0002 D2 stands). In addition, a customer principal may be flagged **tenant administrator**. It adds, changes and disables users of its own tenant only, with roles from the tenant's agreed set only. |
| D3 | Onboarding approval (M2) | A single platform administrator (`access.manage`) records federation, the agreed role set and the first customer administrator, then sets the portal live. The database enforces the order. A customer principal resolves only while its tenant's portal is live and its role is in the agreed set. |
| D4 | Customer roles and landing views | Six default roles (D15 default matrix; admins may edit). None may hold `commercial.read`, `audit.read`, `access.manage`, `mapping.manage` or `connectors.*`. |

The default roles (D4):

| Role | Lands on | Permissions |
| --- | --- | --- |
| CXO | `/customers`: service health, SLA, cloud cost, posture headline | tickets, inventory, cost, vulnerabilities |
| VP or director | `/performance`: SLA attainment and volumes by month | tickets, inventory, cost |
| Architect | `/resources`: the estate | inventory, cost, knowledge |
| Customer engineer | `/work`: the work queue | tickets, inventory, knowledge |
| Security officer | `/posture`: open vulnerabilities by severity and age | vulnerabilities, inventory |
| Approver | `/approvals`: open changes, read-only | `approvals.read` (new), inventory |

`Customer viewer` (step 04) stays, and now lands on `/customers`.

## Consequences

**The boundary is two locks, both in the database.**

1. **Grants (D1).** A customer query naming `planned_hours`, `assigned_to`,
   `monthly_manage_revenue`, a triage classification or an LLM prompt fails with
   `permission denied`. It does not return `NULL` or an empty set, so no serialiser,
   view or forgotten `WHERE` clause can let one through. A table added later has no
   grant until a migration gives it one, so new data fails closed. **Step 16 (commercial)
   and step 17 (reporting) inherit this:** a table customers should read needs an
   explicit `GRANT SELECT (columns) ... TO cloudops_customer`; a commercial table needs
   nothing.
2. **Restrictive policies.** `commercial_visibility` (SOW) and a new `staff_only`
   policy (triage, agent runs, LLM calls and cache, portal onboarding) return no row
   to a customer scope even if a grant were made by mistake. Tests make that mistake
   on purpose and check that nothing is returned.

The API names only granted columns for a customer (`NULL::text` for the workgroup,
for example). That is not the boundary; the database would refuse anything else.

**The login switches role, it never inherits it.** `cloudops_app` is a member of
`cloudops_customer` `WITH INHERIT FALSE, SET TRUE`. The switch is transaction-local
for the same reason scope is (`SET LOCAL`, PgBouncer transaction mode). Before commit,
the tenancy package checks that the transaction is still the role its scope requires,
just as it checks the scope settings. Application SQL cannot `SET`, `RESET` or
`set_config` (the existing statement guard).

**Infrastructure prerequisite.** `cloudops_customer` is a server-level role, so the
server's administrator creates it with `db/bootstrap/roles.sql` before migration
0011. The migration checks this and stops otherwise. Locally `make up` does it. **In
Azure the Entra administrator re-runs `roles.sql` once** (README, "Azure database
bootstrap").

**The access schema is out of a customer scope's reach.** `cloudops_customer` has no
`USAGE` on `access`. The tenant-administrator endpoints therefore call
`access.tenant_*` in the resolution scope, carrying the caller's object id. The
function, not the API, decides which tenant the caller administers. It does so from
`resolve_principal_v2`, so the live gate and the agreed set apply to the
administrator too.

**What a tenant administrator can and cannot do.**

- It reaches only customer principals whose only tenant is its own. Staff, another
  tenant's users, or an unknown object id all fail with the same "no such user", so
  the response says nothing about whether the principal exists.
- It is a whole-tenant principal. A subscription-limited user (ADR 0002, D16) can
  neither administer nor be made an administrator, because it could otherwise create
  users wider than itself.
- The tenant's last administrator cannot be disabled or demoted.
- `access.manage` cannot be granted to any customer role (trigger), in the same way
  `mapping.manage` and commercial permissions cannot be.
- Every change is in `access.change_log` with the acting object id.

**The live gate is fail-closed.** Existing customer principals in an environment
that has not onboarded their tenant stop resolving once 0011 is applied. In dev, a
platform administrator onboards those tenants in the console (Portal onboarding). The
gate is in both `resolve_principal` (same signature, for a pod still on the previous
release) and the new `resolve_principal_v2`. A later contract migration removes the
first.

**Federation (LLD L8) is Entra B2B** (ADR 0002, D5). Customer users are guests in the
Getronics directory, invited through an Entra access package that the customer's own
administrator approves, and authenticated by their home Entra tenant or by SAML or
WS-Fed direct federation. The Entra work is manual (`docs/customer-portal-federation.md`).
The platform records the identifiers only (the customer's directory id or issuer, and
its domains) and holds no customer credential.

**The separate hostname** is a second Front Door endpoint in the same profile, with
its own WAF policy (the same managed rule sets, plus per-IP rate limits: 1000 requests
a minute on every path, 300 on `/v1`) and its own security policy. It uses the same
origin and gateway, so scope never depends on which host a request came through.

## Not decided here

- **LLD L5, the reporting currency per customer.** Customer views show cost in its
  transaction currency, unconverted, as before. L5 stays open.
- **Approving from the portal.** The approver's view is read-only until the approval
  state machine exists. It is not built before the execution plane's security review.
- **Whether a customer may read its own audit trail**, its knowledge base, or
  documents. These have no grant to `cloudops_customer` yet, so they fail closed.
  Steps 17 and later decide.
- **A custom domain and certificate for the portal host.** Dev uses the managed
  `*.azurefd.net` certificate.
