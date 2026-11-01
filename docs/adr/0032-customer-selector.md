# ADR 0032: The customer selector narrows scope, never widens it

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar
- **Implements:** `cloudops_api.pipeline` (`narrow`, `narrowed_scope`, `ScopeDep`), the
  list endpoints behind the console's customer-specific screens, and the console's
  `lib/customer.tsx` (`CustomerProvider`, `useCustomer`) and top-bar selector.
- **Refines:** ADR 0002 (scope resolution).

## Context

Staff assigned several customers read every screen across all of them. The user asked
for a customer dropdown at the top of every screen that shows customer-specific
information. ADR 0002 forbids taking scope from a request parameter, and two endpoints
already took a `tenant_id` anyway (`/v1/reports/packs` as a filter, the assistant's
session as a binding), each in its own way.

## Decision

1. **One helper.** `narrow(access, tenant_id)` returns the scope a request runs under:
   the caller's server-resolved scope when `tenant_id` is absent, otherwise a scope of
   that one tenant, with the same role, principal and subscription restriction. The
   transaction is opened with it, so row-level security does the narrowing, not a
   `WHERE` clause. Endpoints take it as a dependency (`scope: ScopeDep`), which reads
   the optional `tenant_id` query parameter and narrows the audit rows to that tenant.
2. **It never widens.** A `tenant_id` outside the resolved scope is 404 `not_found`,
   the same as a tenant that does not exist, so the answer reveals nothing about which
   ids are real. A customer's scope is its own tenant: naming any other is that 404.
3. **Narrowed today:** work items, resources, customers, performance, posture,
   approvals, FinOps overview, margin overview, report packs and the portfolio. A new
   customer-specific list endpoint takes `ScopeDep` the same way.
4. **The console.** `CustomerProvider` holds the choice; `useCustomer()` returns
   `{ tenantId, setTenantId }`, and screens pass it as `tenant_id`. The choice is
   remembered per user (`localStorage`, keyed by object id, failures ignored) and
   forgotten when the tenant is no longer in `/v1/me`'s tenants. The selector shows
   to every staff user with any tenant in scope, so the top bar always says whose
   data a screen holds, even with one customer (amended 2026-11-01: it first showed
   only with more than one, which left a one-customer environment with no name at
   all). A customer sees its own organisation's name, read-only, never a choice, and
   its `tenantId` is always null (its scope is already one tenant).

5. **Navigation areas.** The console's modules are grouped into Operations, Security,
   Commercial, Assurance & Reports, Knowledge and Settings (`lib/nav.ts`). Settings
   holds user management, cloud and portal onboarding (with the AMARA switch),
   customer mapping (with the platform host) and connectors. Each entry keeps its own
   permission, so the area shows only to platform administrators and the staff (or
   customer administrators) authorised for one of them. Every page still checks
   permission itself. Observability, SecOps and Commercial setup have their places,
   reserved until their pages exist.

6. **Collapsible navigation** (amended 2026-11-01). Observability is now an area of
   its own, after Settings. Each area has an icon and collapses on its own; the
   whole sidebar collapses to an icon rail, with a tooltip and an accessible name on
   every link. The rail is the default below desktop width (1024 px), so the page
   never scrolls sideways at tablet width. Both choices are kept per user in the
   browser (`localStorage`, `cloudops.nav.<object id>`); a blocked store falls back
   to the defaults. Collapsing is presentation only: the list is already filtered by
   the role's permissions, and the rail shows exactly the same modules. Every module
   and area names an icon (`components/NavIcon.tsx`, inline SVG): a new entry adds
   one there if none fits (`cloud` and `trend` are spare for Cloud Services and
   Revenue & margin).

## Consequences

- A client-supplied tenant id can only ever reduce what a request reads. Hiding the
  selector is presentation; the API's intersection is the control.
- A customer that sends another tenant's id now gets 404 where it used to get its own
  data with the parameter ignored. Both reveal nothing; the 404 is the consistent rule.
