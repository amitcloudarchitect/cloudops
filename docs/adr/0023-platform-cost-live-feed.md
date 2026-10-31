# ADR 0023: The platform's own Azure cost, live

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Implements:** the follow-up deferred by ADR 0022, decision 11. `connectors/.../azure/auth.py`,
  the live path in `azure/__main__.py` and `core/cli.py`, `cloudops_config` component
  `azure_cost`, `infra/gitops/base/connector-azure`, Terraform stacks 04-data (export,
  containers), 04b (roles), 05 (federation), `policy/terraform/security.rego`.
- **Departs from:** LLD, Identity ("assignments at resource group scope or narrower,
  never subscription-wide"), for two named read-only roles (decision 4).

## Context

Step 19 made the platform's own cost a tenant like any other (the internal tenant,
`cloudops.tenant.internal`), reconciled and checked for anomalies by the customer
FinOps code and shown on the Grafana platform-cost dashboard. The data was seeded only:
the Azure connector ran on recorded fixtures and had no token path, and nowhere was
decided for the Cost Management export to land. Bronze has public network access and
shared keys off, and `azurerm_subscription_cost_management_export` cannot give an
export a managed identity.

This is not a customer credential (CLAUDE.md rule 2, ADR 0003). The connector reads
the platform's own subscriptions with the platform's own workload identity.

## Decisions

1. **Token path: `httpx.Auth` on the workload identity** (`azure/auth.py`). `EntraAuth`
   wraps `azure.identity.aio.WorkloadIdentityCredential`, built from the identity the
   Azure profile has already validated (`cloudops_config.identity.WorkloadIdentity`),
   never from a credential chain. One scope and one origin per instance: ARM
   (`management.azure.com/.default`) for the client, Storage (`storage.azure.com/.default`)
   for the export reader. A request to any other scheme, host or port is refused before
   a token is fetched, a second lock behind the clients' own `nextLink` checks. Async
   only; the credential caches and renews; a 401 is Temporal's to retry.

2. **Live path: two entities, Azure profile only** (user). `cost_export` (FOCUS line items
   into `cost_fact`) and `cost_total` (the month-to-date total that reconciliation checks
   against). `resource` and `invoice` stay fixture-only: the source refuses them before
   any request, and `run` and `schedule` refuse to name them. A connector with no live
   path still refuses to start without `--fixtures`. Which tenant the cost lands in is
   the alias mapping's (ADR 0016): the platform subscriptions are aliases of `azure`
   for the internal tenant, approved in the mapping console like any other.

3. **The export lands in bronze, in its own container** (user). `cost-exports-platform`,
   written only by Cost Management. The export (`Microsoft.CostManagement/exports@2025-03-01`
   through `azapi`, FOCUS 1.0, daily, month to date, partitioned gzip CSV, a new folder
   per run) has a system-assigned identity, which makes Cost Management a trusted service
   past bronze's firewall (it already admits trusted services, for the Event Hubs
   capture). Stack 04b grants that identity Storage Blob Data Contributor on that
   container alone; the ingest identity gets Storage Blob Data Reader on it, and
   Contributor on its own raw container, `azure`. One export per platform subscription,
   named `cloudops-platform-<env>-<first eight>`. Not considered further: a dedicated
   landing account (another private endpoint and DNS record, and nothing gained over a
   container-scoped grant) and a public landing account (against the LLD).

4. **Two subscription-scope read roles, by name** (user). The Cost Management query has
   no narrower scope that gives the subscription's total. A resource-group query would
   miss subscription-level charges (Defender, support, resources outside our groups), and
   reconciliation would then flag drift that isn't real. So:
   - `id-cloudops-ingest`: **Cost Management Reader** on each platform subscription.
     It reads cost, never a resource's configuration or data.
   - The CI identity: **CloudOps role assignment reader (<env>)**, a custom role holding
     only `roleAssignments/read` and `roleDefinitions/read`, on each platform subscription.
     CI refreshes stack 04b and could not otherwise read the assignment above back.
   conftest admits exactly these role names at subscription scope, and checks that the
   custom role holds nothing else; every other subscription-wide assignment is still
   denied.

5. **No tags on the export.** ARM refuses them on `Microsoft.CostManagement/exports`;
   the tag gate exempts that azapi type by name and still checks every other one.

6. **Deployed as its own worker** (`connector-azure`, platform-ingest). It runs the
   connectors image's Azure entry point with the ingest login, federated to the ingest
   identity like `platform-metrics`. One replica, no autoscaler: two daily entities.
   A sync hook schedules `cost_export` and `cost_total` daily.

## Consequences

- Reconciliation and anomaly detection of the platform's own cost run on real data:
  the first export run is the day after the schedule starts. The dashboard reads what
  the FinOps jobs write, unchanged.
- The ingest identity can read cost on every platform subscription. A stolen ingest
  token can see the platform's spend, not a customer's: the identity has no role on any
  customer subscription. Customer access stays ADR 0003's and the LLD's.
- If someone gives the ingest identity a role on a customer subscription, that
  subscription appears in the listing. Its cost is written only if its id is an approved
  alias, and then under that alias's tenant. A role like that is a review finding in 04b.
- Restating a month re-exports it whole; the connector swaps the period in one
  transaction (`azure/focus.py`, `core/assembly.py`).
- Nothing in the product marks a tenant `internal` in Azure (only the dev seed does).
  Until it does, it is a manual step (`docs/platform-cost-feed.md`).
