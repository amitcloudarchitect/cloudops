# ADR 0031: Platform-level onboarding: internal tenants, the platform host, one owner per account

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar
- **Implements:** migrations `0029_platform_onboarding` (expand) and
  `0030_one_owner_per_account` (contract); `cloudops_api.mapping` (the kind and
  platform-host endpoints), `cloudops_api.home` (`platform`), `cloudops_api.assistant`,
  `cloudops_agents.drafting`, `cloudops_connectors.platform_metrics`, the customer
  credential worker (`customer/worker.py`, `customer/sources.py`), the console's
  Customer mapping, Cloud onboarding and home screens, and the Grafana platform-cost
  dashboard (stack 03b).
- **Amends:** ADR 0022 (one internal tenant), ADR 0023 (where the platform's own cost
  lands), ADR 0027 (decisions 8 and 9: the shared subscription and its provenance).

## Context

ADR 0022 made the platform's own cost a tenant, "the internal tenant", at most one
(migration 0019). ADR 0027 then let that tenant and one customer hold the same
subscription, because the dev subscription is both the platform's and the first
customer's ("Azure Sponsorship"), and marked both rows `shared_with_platform`.

On 2026-11-01 the user changed the model:

- Onboarding any account, internal or external, is a platform-level operation in the
  console at runtime, never a deploy step.
- Many tenants may be internal to Getronics. A tenant is internal or a customer, chosen
  at onboarding, **never both**, and internal tenants are reported on like customers.
- A tenant owns its subscriptions alone: no sharing. Azure Sponsorship (`AZSPONSOR`,
  Entra tenant `2fa5e26d-5ad2-415f-88a2-771caca87cbe`, subscription
  `cd12743c-f76e-4284-aa62-eb8bd576ac91`) is internal, hosts the platform, and is the
  subscription's single owner.

Four readers assumed exactly one internal tenant: the assistant's help-only history,
the help pages' embeddings, `platform_metrics` (Grafana's platform-cost dashboard) and
the home page's Platform cost card. With many internal tenants, "the" internal tenant
no longer exists. The user was asked (2026-11-01):

| # | Question | Decision (user) |
| --- | --- | --- |
| 1 | Which tenant do the four readers use? | **A separate flag, "hosts the platform"**, held by at most one internal tenant, set by a platform administrator in the console. |
| 2 | What is "platform cost" when the host's subscription also holds other workloads (the lab VMs)? | **Only the platform's resources**: lines linked to a resource in a platform resource group. |
| 3 | The host's subscription was read by two feeds (ADR 0023's workload identity and the stored credential). Which feeds its cost? | **The Azure ledger, one feed per subscription.** ADR 0023's feed writes the cost to the owner; the credential reads resources only for that subscription. CoreStack spend comes later, as a reconciliation cross-check (below). |

## Decisions

1. **Internal or customer, at onboarding and later.** `POST /v1/mapping/tenants`
   requires `internal` (no default). A customer master (`mapping.manage`) onboards
   customers; an internal tenant also needs `access.manage`, which the API checks and
   `access.onboard_tenant(actor, name, code, internal)` checks again.
   `POST /v1/mapping/tenants/{id}/kind` (`access.manage`, through
   `access.set_tenant_internal`) changes it later. Both are logged in
   `access.change_log`. The console offers the choice in Customer mapping and Cloud
   onboarding, and the change in Customer mapping's tenant list.

2. **Many internal tenants.** 0030 drops 0019's `tenant_one_internal_idx`. `internal`
   now means only "Getronics', not a customer's". Nothing treats an internal tenant
   differently in reporting: the home cost widgets list it with the customers, marked
   "(internal)", and the FinOps jobs reconcile it and look for anomalies like any other.

3. **One platform host.** `cloudops.tenant.hosts_platform`, unique where true (a partial
   unique index), and only on an internal tenant (a table check). It is set with
   `POST /v1/mapping/platform-host` (`access.manage`, through
   `access.set_platform_host`, which moves the flag under an advisory lock; the old host
   must be in the caller's scope too, since the functions get no wider reach than
   `tenant_isolation`), and read
   with `cloudops.platform_tenant()`. 0023's `cloudops.internal_tenant()` returns the
   same tenant until a later contract drops it. The host:
   - keeps staff help-only assistant conversations, and is billed for the help pages'
     embeddings (ADR 0029);
   - is whose cost `platform_metrics` and the home card read (decision 4).
   With no host set, those say "not captured", as they did with no internal tenant.

4. **The platform's cost is its resources' lines in the host** (user decision 2). A line
   counts when its resource's ARM id is in a platform resource group, by the LLD's
   naming: `rg-cloudops-*`, or AKS's node group `MC_rg-cloudops-*`
   (`cloudops_commercial.finops.PLATFORM_GROUP`). Subscription-level charges and lines
   with no resource are left out. The Sponsorship offer's query feed groups by service
   and carries no resource (ADR 0023, decision 7), so **in dev the platform's part is
   "not captured" until there are resource-level lines**: from an export where the
   agreement allows one, or from CoreStack if it exposes per-resource spend. This is said
   in the open: `cloudops.platform.cost_resource_lines` (0 or 1) and the home card's
   `captured`. Reconciliation and anomalies on the dashboard are the host tenant's.

5. **One owner per account.** A provider account id belongs to exactly one tenant,
   internal or not: `cloud_account_provider_native_key`, unique on
   `(provider, native_id)` again (0030). PR 62's sharing trigger, its function, the
   tenant trigger that kept the internal tenant internal while it shared, and the three
   policies they needed are dropped. A second tenant's insert fails with a unique
   violation. The credential worker reports it as `account_held_by_another_tenant`; the
   alias path leaves the run failing loudly until the mapping is fixed.

6. **Folding PR 62's pairs** (0029). For each id held by the internal tenant and a
   customer, the customer row is kept, the provider alias for that id moves to the
   customer (so ADR 0023's feed writes to the owner), and the internal tenant's row is
   deleted with what hangs off it: the same subscription's cost, read a second time.
   `shared_with_platform` is set false everywhere and no longer read. A later contract
   drops the column. In dev this moves `cd12743c` to `azsponsor`. The old internal
   tenant ("GTX") keeps its other accounts and may then be retired or kept as an
   internal tenant; it no longer hosts anything unless marked to.

7. **One cost feed per subscription** (user decision 3). An approved alias of the
   provider, in the credential's own tenant, means ADR 0023's feed reads that
   subscription's cost. For those accounts the credential's worker runs no cost
   entity: `StoredCredential.cost_binding` leaves them out, and `sources.entities`
   drops `cost_daily`/`cost_total` when nothing is left. A credential whose accounts
   are all platform-fed reads resources only. The check's detail lists them
   (`cost_read_by_platform`). The rule is read again on each pass, so a new alias stops
   the credential's cost schedules on the next pass.

8. **Removed from the API and console:** `CredentialView.shared_with_platform`,
   `AccountCheck.shared_with_platform`, `ProviderCost.shared_with_platform`, and the
   notes that explained them.

## Consequences

- Marking a tenant internal or moving the host is a console action with an audit
  trail, not a manual `UPDATE` (closes ADR 0023's "nothing in the product marks a tenant
  internal").
- Dev's platform cost card and dashboard show "not captured" after this change, because
  dev has no resource-level cost lines. That is accurate, not a regression: the earlier
  figure was the whole subscription, lab VMs included.
- A subscription ADR 0023's identity can read but whose alias maps to a tenant that
  also onboarded it with a credential costs nothing extra: one feed. A customer
  subscription with an alias but no platform role is read by neither feed for cost. A
  mapping like that is a review finding: aliases of `azure` are the platform feed's
  routing.
- Previous-release pods during the rollout: they read `shared_with_platform` (still
  there, always false) and `internal_tenant()` (now the host), and expect `CAOWN`
  rather than a unique violation, which they record as an internal error until
  replaced.

## Not decided here

- **The secret typed into the console.** The user chose a separate customer vault,
  written by the API with a write-only role and read by name at runtime by a dedicated
  identity. That is its own change and ADR, with Terraform the user applies.
- **CoreStack spend.** The sampled CoreStack API has no spend fields (ADR 0028). Once its
  cost endpoint or a sample is shared, CoreStack's figure becomes a reconciliation
  cross-check beside the provider total, and its per-resource spend, if any, can supply
  the platform's part in dev.
- Retiring the old "GTX" tenant in dev.
