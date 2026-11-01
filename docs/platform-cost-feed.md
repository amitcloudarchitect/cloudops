# The platform's own cost, live: turning it on

ADR 0023 (the follow-up to ADR 0022, decision 11). The Azure connector reads the
platform's own cost into the internal tenant:

- `cost_total`: Cost Management's month-to-date total per subscription.
- Line items, from one of two places, set by `CLOUDOPS_AZURE_COST_LINES`:
  - `export`: a daily FOCUS export into bronze (EA and MCA agreements).
  - `query`: the query API's daily cost, for a subscription whose agreement allows no
    export. That is dev: a Sponsorship ("WebDirect") offer, which Azure refuses every
    export type for.

The FinOps jobs reconcile the internal tenant's cost and the Grafana platform-cost
dashboard shows it. Nothing here is applied by CI. The steps run in this order.

## 1. Terraform (plan, then apply)

| Stack | What it adds | Notes |
| --- | --- | --- |
| `04-data` | Containers `cost-exports-platform` and `azure` in bronze. The `Microsoft.CostManagementExports` resource provider registered on each platform subscription. With `platform_cost_export_enabled = true` (the default), one FOCUS export per platform subscription, with its own system-assigned identity. | Dev sets `platform_cost_export_enabled = false` in `04-data.tfvars`. Put the same line in the CI variable `TF_DEV_04_DATA_TFVARS`, or CI's plan will show an export to create. Creating an export needs Cost Management Contributor or Owner on the subscription. The schedule starts at `platform_cost_export_from`, which must be in the future. |
| `04b-rbac` | The ingest identity: Cost Management Reader on each platform subscription, Storage Blob Data Reader on `cost-exports-platform`, Storage Blob Data Contributor on `azure`. The CI identity: *CloudOps role assignment reader (<env>)* on each subscription. Each export's own identity: Storage Blob Data Contributor on `cost-exports-platform`. | Apply after 04-data: an export's identity exists only then. Needs User Access Administrator. The two subscription-scope roles are ADR 0023's recorded exception. |
| `05-aks` | The federated credential for `platform-ingest/connector-azure` on the ingest identity | |

**Dev, as of 2026-10-31.** All three stacks are applied:

- 04-data: the provider is registered; there is no export, and the containers exist.
- 04b: five grants. There is no export identity to grant.
- 05: both federated credentials.

## 2. Merge, then let Argo CD roll it out

Merge after step 19 (PR #37), which this branch is built on. The main pipeline builds
the connectors image, and the dev overlay's tag bump brings `connector-azure` into the
workers Application. Its sync hook creates the configured daily schedules:
`azure-cost_daily` and `azure-cost_total` in dev.

```bash
kubectl -n platform-ingest get pods,jobs -l 'app.kubernetes.io/name in (connector-azure,connector-azure-schedule)'
```

```bash
kubectl -n platform-ingest logs job/connector-azure-schedule
```

## 3. The owning tenant and its subscription

Each platform subscription has one owner (ADR 0031), and this feed writes its cost
there. In dev that is Azure Sponsorship (`AZSPONSOR`), which owns
`cd12743c-f76e-4284-aa62-eb8bd576ac91` and hosts the platform.

1. **The tenant.** On the console's Customer mapping screen (`/mapping`), choose
   **Onboard a new customer**, as a platform administrator:
   - Name: *Azure Sponsorship*
   - Short code: `AZSPONSOR`
   - Kind: **Internal to Getronics**
   - Source: `azure`
   - Alias: the subscription id `cd12743c-f76e-4284-aa62-eb8bd576ac91`

   Then **Onboard and propose**, and approve the mapping. In dev, one person may approve.
   If the tenant exists already, map the alias to it, and use **Make internal** in its
   row.
2. **Mark it the platform's host.** In the same table, **Hosts the platform**. At most
   one tenant does; naming another moves it. The Grafana platform-cost dashboard and the
   home page's Platform cost card then read its platform resources (`rg-cloudops-*`).
   Dev's query feed has no resource-level lines, so they say "not captured".

If the tenant also has a stored credential for the subscription (ADR 0027), the
credential reads its resources only: the cost is this feed's, once.

If the mapping is not there at the first run, the subscription's rows go to the alias
quarantine with their count, and nothing is written until it is approved.

## 4. Checking it

- After the first `azure-cost_daily` run, the console's connector health shows `azure` /
  `cost_daily` and `cost_total`.
- `cost_fact` holds the owning tenant's month by day, service and charge class, in the
  subscription's billing currency. Reconciliation compares it with
  `cost_total`.
- The Grafana platform-cost dashboard needs the collector running in dev
  (docs/observability.md).
