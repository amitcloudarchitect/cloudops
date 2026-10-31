# The platform's own cost, live: turning it on

ADR 0023 (the follow-up to ADR 0022, decision 11). The Azure connector reads the Cost
Management export of each platform subscription, plus Cost Management's month-to-date
total, into the internal tenant. The FinOps jobs reconcile that data and the Grafana
platform-cost dashboard shows it. Nothing here is applied by CI. The steps run in this
order.

## 1. Terraform (plan, then you apply)

| Stack | What it adds | Notes |
| --- | --- | --- |
| `04-data` | Containers `cost-exports-platform` and `azure` in bronze; the `Microsoft.CostManagementExports` resource provider registered on each platform subscription (a new subscription does not have it); one export per platform subscription, `cloudops-platform-dev-cd12743c`, with its own system-assigned identity | Needs Cost Management Contributor (or Owner) on the subscription to create the export. The schedule starts at `platform_cost_export_from` (default `2026-11-01T00:00:00Z`). Cost Management refuses a start in the past, so set a later date in `04-data.tfvars` if you apply after that. Output `platform_cost_export_roots` is the overlay's `CLOUDOPS_AZURE_COST_EXPORT_ROOTS`. |
| `04b-rbac` | The export identity: Storage Blob Data Contributor on `cost-exports-platform`. The ingest identity: Storage Blob Data Reader on `cost-exports-platform`, Storage Blob Data Contributor on `azure`, Cost Management Reader on the subscription. The CI identity: the custom role *CloudOps role assignment reader (dev)* on the subscription. | **Apply after 04-data.** The export's identity exists only then, so a 04b plan made before 04-data is applied shows 5 additions, and a plan made after shows 6. Needs User Access Administrator. The two subscription-scope roles are ADR 0023's recorded exception. |
| `05-aks` | The federated credential for `platform-ingest/connector-azure` on `id-cloudops-ingest-dev` | It also shows `platform-ingest/platform-metrics` if step 19's 05 apply has not happened yet. |

## 2. The internal tenant and its subscription

1. **The tenant.** If dev's database has no internal tenant yet, onboard *Getronics
   CloudOps platform* in the console (step 18). Then mark it internal. Nothing in the
   product does that yet; only the dev seed does (`cloudops_devdata.seed.mark_internal`:
   one `UPDATE cloudops.tenant SET internal = true` through `packages/tenancy`, with an
   ingest scope over that tenant). At most one tenant can be internal (migration 0014).
2. **The alias.** In the mapping console (step 14b), map the subscription id
   `cd12743c-f76e-4284-aa62-eb8bd576ac91` (source `azure`) to that tenant. In dev, one
   person may approve. If you skip this, the first run puts the subscription in the
   alias quarantine with its count, and nothing is written until you approve it there.

## 3. Merge, then let Argo CD roll it out

Merge after step 19 (PR #37), which this branch is built on. The main pipeline builds
the connectors image, and the dev overlay's tag bump brings `connector-azure` into the
workers Application. Its sync hook creates the `azure-cost_export` and
`azure-cost_total` schedules, both daily. The dev overlay keeps `CLOUDOPS_TELEMETRY: "off"`
like the other workers until the collector runs in dev (docs/observability.md).

## 4. The first run, and checking it

- The export's first scheduled run is on the schedule's start date. To run it now:

  ```bash
  az rest --method post --url "https://management.azure.com/subscriptions/cd12743c-f76e-4284-aa62-eb8bd576ac91/providers/Microsoft.CostManagement/exports/cloudops-platform-dev-cd12743c/run?api-version=2025-03-01"
  ```

- Check that a run folder and its `manifest.json` landed under
  `cost-exports-platform/focus/cloudops-platform-dev-cd12743c/`. If the run history
  shows a storage access error, check that the 04b grant for the export's identity
  exists. Microsoft documents exports to a firewalled account with keys disabled as
  supported, but that is not yet proven on this account.
- After the next `azure-cost_export` run, the console's connector health shows
  `azure` / `cost_export` and `cost_total`, and the platform-cost dashboard shows the
  month to date, reconciled against Cost Management's total.
