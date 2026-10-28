# ADR 0004: Identity resolution under row-level security

- **Status:** Accepted, 2026-10-28
- **Decided by:** Amit Kumar (step 05 carried out on this design)
- **Implemented by:** migration `0005_connector_ingest`, `connectors/`

## Context

A ServiceNow page mixes tickets from many customers. To attach each ticket to a tenant,
the connector must match its company name against every tenant's aliases. The LLD sets
the order (exact, then normalised, then quarantine) but not the mechanism.

The connector writes as the ingest login, under row-level security. Two obvious
mechanisms are both wrong:

- **Give ingest a scope covering every tenant** so it can search all aliases. Then
  every connector run can read and write every customer's data, and RLS no longer
  limits a connector bug or a compromised connector.
- **Resolve in application code** from a copy of the alias table. That needs a
  privileged read path outside `packages/tenancy`, a second source of truth, and the
  normalisation rules duplicated in Python.

## Decision

Use a narrow `SECURITY DEFINER` function, `cloudops.resolve_tenant_alias(source, name)`.
This is the pattern ADR 0002 established for `access.resolve_principal`.

- **Order:** an exact match on source plus alias first, then a normalised match across
  every registered alias. Ambiguity (two tenants) and no match both return no tenant;
  never a guess.
- **Normalisation:** `cloudops.normalise_company(text)` lowercases, strips punctuation,
  drops legal-suffix tokens and closes up the spaces. The suffix list is configuration:
  the `staging.legal_suffix` table, seeded with the LLD's list.
- **Ownership:** the function is owned by `cloudops_resolver`, the NOLOGIN role that
  already resolves identities before scope exists. A permissive policy,
  `alias_registry_read`, lets that role read `tenant_alias`, and nothing else. Reusing
  the role means no new server bootstrap, in Azure or locally.
- **Callers:** executable by `cloudops_ingest` only, not by the application role and
  not by `PUBLIC`.
- **What it returns:** a tenant id and an outcome (`exact`, `normalised`, `ambiguous`,
  `unmatched`), never alias text. A connector learns which tenant a name belongs to and
  nothing about other tenants. Under its own empty scope, the ingest login still sees
  zero `tenant_alias` rows (a test proves this).
- **Commit scope:** after resolution, a page commits in a transaction scoped to exactly
  the tenants it resolved to, so RLS still checks every row written.

## Consequences

- **Scored against ground truth:** all 54 messy spellings in the synthetic dataset
  resolve correctly. Every registered alias matches exactly, every variant that
  normalises finds the right tenant, and every typo, accent, abbreviation and ambiguous
  fragment is quarantined.
- **Normalisation is broad by design.** "Fabrikam, Inc." normalises to `fabrikam`, so
  any name that reduces to the same string resolves there. That is the LLD's rule as
  written. Bad matches surface in review, and an exact alias registered for the right
  tenant always wins first.
- **The catalog test now allows exactly two permissive policies beyond
  `tenant_isolation`**, both confined to `cloudops_resolver`. A third fails the build
  until someone decides how it is protected.
