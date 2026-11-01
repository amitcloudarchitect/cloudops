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
   only to staff with more than one tenant; a customer never sees it, and its
   `tenantId` is always null (its scope is already one tenant).

## Consequences

- A client-supplied tenant id can only ever reduce what a request reads. Hiding the
  selector is presentation; the API's intersection is the control.
- A customer that sends another tenant's id now gets 404 where it used to get its own
  data with the parameter ignored. Both reveal nothing; the 404 is the consistent rule.
