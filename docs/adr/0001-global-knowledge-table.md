# ADR 0001: Getronics-wide knowledge lives in its own table

- **Status:** Accepted, 2026-10-28
- **Decided by:** Amit Kumar
- **Changes:** LLD, AI platform configuration, Vector store
- **Implemented by:** migrations `0002_global_knowledge_chunk` (expand) and
  `0003_knowledge_chunk_tenant_required` (contract)

## Context

The LLD gave `knowledge_chunk` a nullable `tenant_id`. A NULL meant content that belongs
to no customer, such as shared runbooks and best practice, and every scope could read
it. The retrieval query admitted those rows with `OR tenant_id IS NULL`.

No scope could ever write a NULL row, because the policy's `WITH CHECK` has no NULL
branch. Even so, it was the only customer-data table where `tenant_id` could be absent.
It also put an exception into the isolation policy and the catalog checks.

## Decision

Every table holding customer data has a non-nullable `tenant_id`, with no exceptions.
This applies to every customer onboarded from now on.

- `cloudops.knowledge_chunk` holds customer content only. Its `tenant_id` is
  `NOT NULL`, and it has the same `tenant_isolation` policy as every other tenant table.
- `cloudops.global_knowledge_chunk` holds Getronics-wide content. It has no `tenant_id`
  and no RLS; like `fx_rate`, it is reference data protected by grants. The
  application and ingest roles get `SELECT` only, so no scoped session can write
  content that every customer's assistant would read. Who authors global content, and
  through which path, is a later decision. Until then, only the schema owner can.
- Retrieval queries both tables, with the tenant predicate inside the SQL for the
  customer branch. Each branch has its own HNSW index (`m=16`, `ef_construction=64`).

## Consequences

- The isolation catalog checks lose their nullable-tenant exception. A new test fails
  if any isolation policy contains an `IS NULL` branch.
- Retrieval becomes a `UNION ALL` of two ordered, limited branches instead of one query.
  `ef_search` is tuned per index against the evaluation set.
- Writing to the global table is now a privileged operation that never runs inside a
  customer scope. That separation is the point of the change.
- **Contract timing:** 0003 shipped in the same change as 0002 only because no release
  has run against 0001; there is no deployment pipeline before step 13. Once a release
  is deployed, a contract migration ships one release after its expand, as the LLD
  requires.
