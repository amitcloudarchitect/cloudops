# Terraform

One stack per concern, one state per stack per environment. Development runs in
a single subscription: resource groups stand in for the LLD's subscriptions,
and every stack takes `subscription_id` plus optional per-domain overrides
(`subscription_ids`), so the five-subscription split is a tfvars change.

**Plan freely. Never apply from automation.** A human reviews each plan and
applies it (CLAUDE.md).

## Order

| Stack | Creates | Needs |
| --- | --- | --- |
| `01-network` | VNet `10.65.0.0/16`, subnets, NSGs, NAT gateway, private DNS zones | — |
| `02-security` | Key Vaults (app Standard, keys Premium), workload and AKS identities | 01 |
| `03-observability` | Log Analytics, Application Insights, Key Vault diagnostics | 02 |
| `04-data` | PostgreSQL 16, Redis Premium, storage, Event Hubs | 01, 03 |
| `04a-registry` | Container registry `crcloudops{env}{region}` in its own resource group | 03 |
| `04b-rbac` | **Every role assignment** (User Access Administrator); after a first `05`, apply again for the kubelet's AcrPull | 02, 04, 04a |
| `05-aks` | Private AKS cluster, pools npsystem/npapp/npingest/nptelemetry/npai | 01, 02, 03, 04b |
| `06-edge` | Front Door Premium with WAF (Prevention, DRS 2.1 and bot rules), the ingress public IP in public mode | 01, 03, 05 |

## Running a stack

```bash
cd infra/terraform/stacks/01-network
terraform init -backend-config=../../env/dev/backend.hcl
terraform plan -var-file=../../env/dev/common.tfvars -out=tfplan
terraform show -json tfplan > plan.json && python ../../../../tools/ci/check_terraform_tags.py plan.json
```

Stacks 04 and 05 also take `-var-file=../../env/dev/04-data.tfvars` / `05-aks.tfvars`.
Copy each `*.tfvars.example` to `*.tfvars` first; tfvars files are gitignored.

## Prerequisites

- `az login` as yourself. No service principal, no secret.
- The state account `stcloudopstfstatedevweu` (AZURE-ACCESS.md), with
  **Storage Blob Data Contributor** for you on it — `use_azuread_auth` needs it.
- Contributor on the subscription; User Access Administrator (or a colleague)
  for `04b-rbac`.
- vCPU quota for Dv5 in West Europe: the LLD pools need **56 vCPUs at
  minimum, 140 at maximum**.
- Resource providers registered: Network, KeyVault, ManagedIdentity,
  OperationalInsights, Insights, DBforPostgreSQL, Cache, Storage, EventHub,
  ContainerService.

## Open items carried by this code

- **Redis AOF persistence** (LLD) is off: it needs a storage connection string,
  i.e. a key, and shared key access is disabled everywhere.
- **AKS pod CIDR** is `100.64.0.0/16`, not the LLD's `10.x.16.0/20`: overlay
  pods must sit outside the VNet, and a /20 caps the cluster at 16 nodes.
- **Bronze storage is GRS**, not the LLD's GZRS: Azure cannot archive blobs in
  zone-redundant accounts, and the LLD's archive-at-13-months tier won.
- **Dev PostgreSQL is in northeurope** (`psql-cloudops-dev-neu`): the dev
  subscription is restricted from Flexible Server in westeurope. Other
  environments default to westeurope.
- **AKS service CIDR** is `10.200.0.0/16`: `172.16.0.0/16` is used by two VNets
  in the dev subscription, and AKS reserves `172.30/16` and `172.31/16`.
- **Dev box to PostgreSQL** (`01-network`, dev only): `allow-devbox-postgres` admits
  `snet-devbox-dev` to the data subnet on 5432, and nothing else. It is there to run
  the server bootstrap (`db/bootstrap/roles.sql`) and, until the migration pipeline
  exists (step 13), migrations as `id-cloudops-migrate-dev`. PostgreSQL still requires
  an Entra token. The trade-off: while `ssh_allow_any_source` is on, a compromised dev
  box can reach the server's port. Remove the rule once the pipeline runs migrations.
- **`network_mode = "public"`** (dev only; a validation refuses it elsewhere). The LLD
  is "private". In public mode:
  - the AKS API server is public, limited to `api_server_authorized_ip_ranges` (empty,
    as dev currently sets it, means any IP), and
    still Entra-only (no local accounts). Switching modes replaces the cluster: new node
    resource group, new OIDC issuer, workload identity federations to redo;
  - the Gateway API ingress has a public static IP (`06-edge`, ADR 0011). Two locks
    keep everything but our Front Door out: the `AzureFrontDoor.Backend` NSG rule
    (`01-network`) and the exact `X-Azure-FDID` match on the API's HTTPRoute;
  - Front Door reaches the origin over **plain HTTP**: it refuses self-signed origin
    certificates and dev has no public certificate for the IP. Client to Front Door is
    HTTPS only. Private mode uses Private Link and HTTPS to the origin.
- **Dev container registry is Basic with public network access** (`04a-registry`).
  The LLD's registry is private: Premium, a private endpoint and the
  `privatelink.azurecr.io` zone (already in stack 01). Basic was chosen for cost. The
  admin user and anonymous pull are off, so every pull and push needs an Entra
  identity with AcrPull or AcrPush (`04b-rbac`). UAT and production must use Premium
  with the private endpoint.
- **Dev PostgreSQL accepts password logins** (`04-data`, dev only; ADR 0008). Entra
  stays enabled and is how the server is administered. The password-auth admin
  `breakglass_dev` has a write-only password generated at apply and not kept. The
  application and Temporal logins are created by `scripts/db_dev_logins.py`.
  UAT and production stay Entra-only.
- **Dev box to Key Vault** (`01-network`, dev only): `allow-devbox-keyvault` admits
  `snet-devbox-dev` to the vault subnet on 443, so `scripts/db_dev_logins.py` can store
  the connection strings. The vault still requires an Entra identity with a secrets
  role (`04b-rbac`).
- **PgBouncer** (L6) is not configured; transaction mode is mandatory once it is.
- **Exec plane** (identity, vault, VNet `10.62.0.0/16`, npexec) is not here,
  pending the security review.
- **Azure Policy** at `mg-cloudops` needs management-group rights; tags are
  enforced at plan time by `tools/ci/check_terraform_tags.py` until then.
