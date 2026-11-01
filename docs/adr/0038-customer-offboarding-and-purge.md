# ADR 0038: Editing a customer, and deleting one: offboard now, purge later

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar
- **Implements:** the user's decision (2026-11-01): "delete" offboards a customer
  immediately, a platform administrator purges it after a retention period with a
  second confirmation, and the audit log is kept; customer names are editable.
  Migration `0036_tenant_lifecycle`, `services/api/src/cloudops_api/tenants.py`,
  `connectors/src/cloudops_connectors/lifecycle/` (the purge worker, run by the `azure`
  connector's worker), `TelemetryStore.purge` in `packages/tenancy`, and the console's
  Settings → Manage customers (`/settings/customers`).
- **Amends:** ADR 0002 D9 (an all-tenant principal's scope is every tenant *that is not
  offboarded*), ADR 0015 decision 4 (a tenant is now renamed through
  `access.update_tenant`), ADR 0018 (a customer principal of an offboarded tenant
  resolves to nothing, as with a suspended portal).

## Context

Until now a tenant could be onboarded and never removed or renamed: no function changed
its name, and deleting its row was impossible (the audit log references it) and would
have been wrong (the LLD keeps the audit log seven years, never dropped). A customer
that leaves must stop being visible and reachable at once, and its data must go after
an agreed period, without losing the record of what was done to it and by whom.

## Decisions

1. **Edit is the customer master's** (`mapping.manage`; an internal tenant also needs
   `access.manage`, as onboarding one does, ADR 0031): `access.update_tenant(actor,
   tenant, name, code)`, the tenant in the caller's scope, logged in `access.change_log`
   with both values. **The short code changes only where safe:** a stored credential's
   Key Vault secrets, Temporal task queue and schedules are named by it (ADR 0027), so it
   is refused (`short_code_in_use`) while the tenant has a credential that is not
   revoked. Aliases and mappings are keyed by tenant id and are untouched.

2. **Delete is offboarding, a platform administrator's** (role class `platform_admin`
   holding `access.manage`, the tenant in scope; `access.offboard_tenant`). It needs the
   customer's exact name typed and a reason, and takes effect in one transaction:
   - the tenant's status becomes `offboarded` (`offboarded_from`, `_at`, `_by`, reason
     and `purge_after` recorded);
   - its stored cloud credentials are revoked (the customer worker drops their workers
     and schedules on its next pass, within a minute);
   - its portal, if live, is suspended.
   The platform's host cannot be offboarded (`platform_host_rule`).

3. **An offboarded tenant is in no scope.** The resolvers leave it out, and row-level
   security does the rest, on every table, for every query:
   - `resolve_principal_v2`: all-tenant principals lose it, named ones lose it, and a
     customer principal of it resolves to nothing (the same as a suspended portal);
   - `resolve_tenant_alias`: its names match nothing, so connectors quarantine new
     records for them with a count (CLAUDE.md rule 6), never drop them;
   - `finops_tenants`, `knowledge_tenants`, `pending_drafts`: the FinOps jobs, the
     platform cost feed's tenant list, the knowledge indexer and the drafter skip it.
   So it vanishes from every list, the customer selector and every dashboard on the
   next request, and no connector or job writes to it.

4. **Restore within the retention** (`access.restore_tenant`, a platform administrator
   assigned the tenant or every tenant). The status it had comes back. Its credentials
   stay revoked and its portal suspended: both are onboarded again on purpose, with the
   checks that onboarding runs. After `purge_after` it can no longer be restored.

5. **Retention: 30 days by default**, a single row in `access.lifecycle_policy`, changed
   by a migration, never by the application.

6. **Purge: a request after the retention, a worker does it** (ADR 0015's pattern; the
   API starts nothing). `access.request_tenant_purge` needs the name typed *and* the
   short code typed (the second confirmation), refuses before `purge_after`
   (`retention_not_ended`), records `audit.tenant_purge` and marks the tenant `purging`.
   The purge worker, every minute in the `azure` connector's worker (or
   `python -m cloudops_connectors.lifecycle purge`):
   1. claims the request (`cloudops.claim_tenant_purge`) before it knows the tenant;
   2. deletes the tenant's **bronze pages**: those whose metadata names one of its
      stored credentials (`instance` ends `#credential=<id>`), which hold its estate
      alone;
   3. deletes its **telemetry** at every grain where a column store with a purge login
      is configured, else records `not_configured` (decision 9);
   4. calls `cloudops.purge_tenant`, which in one transaction deletes the tenant's rows
      from **every table with a `tenant_id` column**, found in the catalog (partitioned
      tables through their parents, the vector store's `knowledge_chunk` included),
      clears every other column that references the tenant (a quarantined name's
      suggestion), removes it from CUR assembly state, deletes customer principals that
      held only this tenant, and marks the tenant `purged`.
   A failure closes the request `failed` with a step code; the tenant stays `purging`
   and a platform administrator asks again. Bronze and telemetry deletion are idempotent.

7. **What is kept: the audit log and a tombstone.** `audit.action_log` (LLD: seven
   years, never dropped) and `access.change_log` keep every row. The tenant row stays as
   the tombstone the audit log's foreign key needs: id, name, short code, kind, when and
   by whom it was offboarded and why, and when it was purged; contract dates are
   cleared. `audit.tenant_purge` keeps each request, its counts per table and its
   outcome, forward-only and undeletable. A purged tenant's name and short code may be
   used again (uniqueness is now among tenants not purged).

8. **The database holds every rule.**
   - A trigger on `cloudops.tenant` refuses any change to the lifecycle columns, and any
     change at all to an offboarded or purged row, except by the lifecycle functions'
     owners, the tables' owner (the purge) or a superuser. The ingest role keeps its
     seed-era UPDATE on tenants and cannot use it to offboard, restore or hide one.
   - The append-only tables (alias proposals and decisions, re-process requests,
     credentials and their checks, evidence, released narratives) yield a **delete** to
     a purge only: the tenant must be `purging` *and* the transaction must be the purge
     (a transaction-local marker `purge_tenant` sets). Updates stay refused.
   - Restore, the purge request and the list reach tenants outside every scope, so they
     belong to `cloudops_resolver`, the one role that reads every tenant, with one more
     policy: it may update a tenant that is offboarded or purging. The purge itself is
     the tables' owner's; the ingest login may only call it, for a claimed request.
   - **The catalog test** (`test_every_tenant_reference_is_one_the_purge_clears`) fails
     when a table holds a tenant id the purge cannot find: a uuid column named for a
     tenant that is not `tenant_id`, a foreign key to the tenant that cannot be cleared,
     or a `tenant_id` outside the platform schemas. The purge test counts every
     `tenant_id` table from the catalog, so a new table is covered without being listed.

## Consequences

- An offboarded customer disappears for everyone at once, including other platform
  administrators; only Manage customers lists it, with Restore while the retention
  runs and Purge after it.
- A named staff principal whose only tenant was offboarded is left with no scope
  (`no_scope`, as before any assignment) until it is restored or reassigned.
- Waiting work for an offboarded tenant is not run: a credential check refuses a revoked
  credential, a re-process finds its name no longer resolves, drafting and FinOps skip
  it. A request already running finishes under the scope it started with.
- The audit trail outlives the customer by design: it names the customer (the tombstone
  and the change log), which is what makes it an audit trail.

## Not decided here (follow-ups)

- **Bronze pages a platform-wide source fetched.** A ServiceNow page, the platform's own
  cost export, a Qualys or CoreStack page hold many tenants' records. They are not
  deleted; they age out under the bronze lifecycle (seven years, LLD). Options: rewrite
  each such page without the tenant's records (breaks write-once bronze and replay
  hashes); write per-tenant pages going forward; or encrypt per tenant and destroy the
  key (crypto-shredding). The user to choose.
- **Bronze versions and soft delete in Azure.** The bronze account keeps versions and
  30-day soft delete (stack 04-data). A deleted page leaves a previous version, which
  the ingest identity (Storage Blob Data Contributor) cannot delete. Closing it needs a
  management-policy rule on the bronze account that deletes previous versions after N
  days, or a purge identity holding `deleteBlobVersion`: a Terraform change for the user
  to plan and apply.
- **Telemetry in production.** The column store is still LLD L1. `TelemetryStore.purge`
  is built and tested (one tenant, the ingest role, the tenant predicate), but the
  application's ClickHouse user has SELECT and INSERT only, as it should; a purge login
  with `ALTER DELETE` is wired when L1 is decided. Until then a purge records
  `telemetry: not_configured`, and telemetry ages out by its TTL (90 days raw, 13 months
  5-minute, 3 years hourly).
- **Where the purge runs in production.** The `azure` connector's worker, which holds the
  bronze container stored credentials write to. Production has no stored credentials
  (ADR 0003), so there the purge has no bronze to delete; a dedicated deployment can
  replace this when the execution plane's broker exists.
- **A second person for the purge.** One platform administrator confirms twice. A
  two-person rule like the mapping's (ADR 0015) is a small change if wanted.
- **Watermarks and cursors** keyed by a credential's instance stay in `staging` after a
  purge: source positions, no customer data.
