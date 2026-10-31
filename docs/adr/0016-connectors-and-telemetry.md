# ADR 0016: Remaining connectors, telemetry and connector health

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Implements:** step 15; LLD Connector contract, Identity resolution, Telemetry
  store, Event Hubs; HLD Integration. Migration `0010_connectors_and_health`,
  `connectors/src/cloudops_connectors/{core,qualys,corestack,azure,aws,sharepoint,entra,telemetry}`,
  `packages/tenancy/src/cloudops_tenancy/telemetry.py`, `db/telemetry/`, and the
  console's connector health screen.

## Context

ServiceNow (step 05) proved the connector contract: raw to bronze before parsing,
the watermark advanced inside the committing transaction, unmatched identities
quarantined with counts. Six more sources and the telemetry stream follow it. There
are no customer credentials yet, so everything is built against recorded fixtures.
The LLD leaves several things open; the decisions below were taken by the user
before the build.

## Decisions

1. **One orchestration for every new source.** `core/ingest.py` holds the contract as
   two Temporal workflows (`ConnectorIngest`, `ConnectorReplay`) over a small `Source`
   interface (`fetch`, `inspect`, `process`, optional `scopes`, `revoke`). ServiceNow
   keeps its own workflows unchanged. Per-scope watermarks (a SharePoint site, an
   Entra directory) and a cursor for continuation state (Graph delta links), both
   moved only by a committing page. Sources whose pages are not in time order
   (Qualys, AWS Config) move the watermark only on the run's last page.

2. **Telemetry store (LLD L1 stays open).** The schema and the wrapper are built on
   ClickHouse, locally and in CI. The Azure backend keeps refusing until L1 is
   decided. The wrapper's interface (`MetricQuery`, `MetricSample`) carries no SQL,
   so a different backend changes only the module behind it.

3. **The wrapper is the isolation.** The store has no row-level security.
   `cloudops_tenancy.telemetry` is the only code that reads or writes it:
   - It accepts no SQL.
   - Every statement opens with the tenant predicate, and the account predicate for
     a subscription-limited scope.
   - Values are bound as server-side parameters.
   - An empty scope sends nothing.
   - Reads run `readonly=2`.
   - Writes need the ingest role and check every row against the scope before
     sending.

   CI fails any string outside the package that names a telemetry table or the
   store's HTTP dialect. The isolation suite runs the wrapper against a real store.

4. **Metric allowlist (LLD L3): a starter list as data.** `staging.metric_allowlist`
   holds (resource type, metric) pairs for capacity, saturation and availability.
   Other metrics are counted and not stored; the Event Hubs Capture copy in bronze
   keeps them. Changing the list is a data change, not a release.

5. **Cloud accounts resolve through the customer mapping.** A subscription, AWS
   account or Entra directory id is an alias of its provider (`azure`, `aws`,
   `entra`). It must match exactly; an unknown id is quarantined and appears in the
   step 14b mapping console under the two-person rule. Once mapped, the connector
   upserts `cloud_account`. The synthetic seed registers its accounts this way.
   Sources that name the customer by name (Qualys, CoreStack, SharePoint) resolve as
   ServiceNow does.

6. **Scope: ingest only.**
   - CUR lands in `cost_fact` with `charge_class` from `lineItem/LineItemType` through
     a fixed table. Each assembly is staged in `staging.cost_fact_load` and swapped in
     one transaction when its last file lands, so a restatement replaces its accounts'
     rows for the period atomically. Reconciliation, allocation and refined
     classification are step 16.
   - CoreStack findings go to `cost_finding`, beside the billing exports and never
     instead of them. A finding whose account belongs to a different customer than
     its name is rejected, not written under either one.
   - SharePoint writes text chunks to `knowledge_chunk` without embeddings, and
     deletes them when a document is deleted or a site's consent is revoked.
     Embedding and citations are step 17.

7. **Connector health is data, shown in the console.** `access.connector_health`
   reads the run log, dead letters, quarantine depth and schema drift, banded
   against each source's expected interval (`staging.connector_schedule`, from the
   HLD cadences). The LLD pages after three missed intervals. Schema drift is a new
   unmapped field, or an expected field missing from a whole page. It is recorded
   per field and acknowledged by a person (`connectors.manage`). `connectors.read`
   goes to the staff roles that watch ingest, never to customers.

## Consequences

- The connectors cannot reach a real source until credentials exist. Without
  `--fixtures` they refuse rather than reach for a credential (CLAUDE.md rule 2).
  Endpoint shapes for CoreStack, and Qualys's `Customer:` tag convention, are
  assumptions to confirm against the real systems.
- Telemetry backfill from the Avro capture (the LLD recovery sequence) is not built;
  the capture itself is infrastructure (04-data) and runs in parallel already.
- An approved mapping for an Azure, AWS, Entra or CoreStack name takes effect at the
  next run. Only ServiceNow has the explicit re-process of held records (ADR 0015).
