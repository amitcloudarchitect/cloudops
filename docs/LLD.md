## Purpose and conventions

How the platform described in the High Level Design is built: the Azure resources, their configuration, naming, networking, identity, database objects, pipelines and operational procedures. The HLD answers *what and why*; this answers *how, with which settings*. Where the two disagree, the HLD governs intent and this governs implementation.

| Convention | Meaning |
| --- | --- |
| `{env}` | `dev`, `tst`, `uat`, `prd`, `dr` |
| `{region}` | `weu` (West Europe, primary), `neu` (North Europe, paired) |
| **MUST / MUST NOT** | Non-negotiable; enforced by policy, pipeline or review |
| Sizes and SKUs | Starting points, validated under load test before production |

Not covered here: application source structure, UI component specifications, and the delivery plan. Before implementation begins the open decisions in the HLD must be closed — in particular the autonomy ceiling, customer consent for agentic execution, data residency, and the nominated FX source.

## Pre-deployment readiness

Everything that must exist before the first Terraform apply. The build order later assumes all of it is in place.

![Prerequisites and lead times](07-prerequisites.png){width=6.6in}

| Gate | Requirement | Owner |
| --- | --- | --- |
| Landing zone | `mg-cloudops` management group; five subscriptions; vCPU, disk and public IP quota; Azure OpenAI deployment approved with tokens-per-minute quota; address space `10.60.0.0/16`–`10.65.0.0/16` confirmed free | Cloud platform |
| DNS and certificates | Public hostnames for staff and customer entry points, with issuance rights | Network |
| Identity | Entra groups populated; PIM eligible assignments and approvals; app registrations with admin consent; GitHub OIDC federated credential; two break-glass accounts | Identity, Security |
| Source credentials | ServiceNow read and field-scoped write accounts plus a non-production instance; Qualys, CoreStack and CloudBolt service accounts; Resolve OAuth via HTTP Gateway; SharePoint per-site selected consent; Entra Graph read-only | Service owners |
| Customer access | Lighthouse delegation accepted or AWS CloudFormation stack deployed, per pilot customer, with external ID | Delivery |
| Sandbox | Getronics-owned Azure subscription and AWS account for connector and runbook testing | Cloud platform |
| Reference data | Customer master and alias list; SOW register; people cost; confirmed rate card; FX source; 98 control definitions with evidence sources; policy library; tagging standard | Delivery, Finance, Security |
| Tooling | Source control with OIDC and branch protection; container registry; Log Analytics and Grafana; scanning licences; design tooling and brand guidelines; penetration test supplier engaged | Platform |
| Organisation | Executive sponsor, full-time product owner, persona owners, three pilot customers, named security reviewer | Executive sponsor |

**Gate 3 is the usual schedule risk.** Obtaining and validating the ServiceNow write account and the per-site SharePoint consents routinely takes longer than building the connectors that depend on them, so these requests go in during mobilisation.

## Subscriptions, naming and tagging

Five subscriptions under a dedicated management group, separated by blast radius rather than cost centre.

![System design and deployment topology](01-system-design.png){width=6.6in}

| Subscription | Purpose |
| --- | --- |
| `sub-cloudops-dev` | Developer environments; Dev/Test pricing, scale to zero outside hours |
| `sub-cloudops-tst` | Automated test; ephemeral |
| `sub-cloudops-uat` | UAT and performance; production-like SKUs, masked data |
| `sub-cloudops-prd` | Production platform; full HA, locked |
| `sub-cloudops-exe` | Execution plane only; hardened, holds the signing vault, execution workers and transcript store |

**Why the execution plane is its own subscription.** Everything that can reach a customer environment lives there with its own policy set, RBAC, key vault and diagnostics. A compromise or misconfiguration of the main platform subscription grants no path to customer credentials, and it gives a clean audit boundary when a customer asks what can touch their estate.

Resource groups are named `rg-cloudops-{purpose}-{env}-{region}`, one per lifecycle: `net`, `aks`, `data`, `msg`, `sec`, `ai`, `obs`, plus `rg-cloudops-exec-prd-weu` and `rg-cloudops-dr-prd-neu`. `CanNotDelete` locks on every data, security and execution group in production.

### Naming

`{type}-cloudops-{workload}-{env}-{region}`, lowercase and hyphen-separated; where a resource type forbids hyphens the tokens are concatenated.

| Resource | Example |
| --- | --- |
| Virtual network | `vnet-cloudops-prd-weu` |
| Subnet | `snet-aks-app-prd` |
| AKS cluster | `aks-cloudops-prd-weu` |
| Node pool | `npapp`, `npexec` (≤12 chars, alphanumeric) |
| PostgreSQL | `psql-cloudops-prd-weu` |
| Redis | `redis-cloudops-prd-weu` |
| Storage account | `stcloudopsbronzeprdweu` |
| Key Vault | `kv-cloudops-keys-prd-weu` |
| Event Hubs namespace | `evhns-cloudops-prd-weu` |
| Container registry | `crcloudopsprdweu` |
| Managed identity | `id-cloudops-exec-prd` |
| Private endpoint | `pe-psql-cloudops-prd` |

Kubernetes namespaces use the module layer: `platform-api`, `platform-web`, `ingest`, `telemetry`, `ai`, `workflow`, `execution`, `observability`.

### Tagging

Mandatory on every platform resource, enforced by Azure Policy with a deny effect: `owner`, `environment`, `costcentre`, `application`, `dataclass` (`c1`–`c5` per the HLD sensitivity classes), `managedby` (`terraform`). Optional: `schedule`, `reviewdate`, `changeref`. A resource found without the mandatory set is a pipeline failure, not a remediation ticket — the platform's credibility in enforcing tagging on customer estates depends on holding itself to the same rule.

### Azure Policy

Assigned at `mg-cloudops` and inherited: allowed locations `westeurope` and `northeurope` only; allowed resource types; mandatory tags; deny public IP on VM or AKS node; storage HTTPS-only with TLS 1.2 and no public blob; PostgreSQL and Redis public network access disabled; Key Vault purge protection and soft delete; diagnostic settings and Defender plans deployed if not present.

## Network

Non-overlapping address space so any pair can be peered later without renumbering. Customer estates are never peered — all customer access is outbound through provider control planes.

| Environment | VNet CIDR |
| --- | --- |
| Production | `10.60.0.0/16` |
| DR (North Europe) | `10.61.0.0/16` |
| Execution plane | `10.62.0.0/16` |
| UAT / Test / Dev | `10.63` / `10.64` / `10.65.0.0/16` |

### Production subnets

| Subnet | CIDR | Contains |
| --- | --- | --- |
| `snet-aks-system-prd` | `10.60.0.0/22` | AKS system node pool |
| `snet-aks-app-prd` | `10.60.4.0/22` | Web, API, ingest, telemetry, AI pools |
| `snet-aks-pods-prd` | `10.60.16.0/20` | Pod CIDR, Azure CNI overlay |
| `snet-data-prd` | `10.60.32.0/24` | Private endpoints: PostgreSQL, Redis, storage |
| `snet-msg-prd` | `10.60.33.0/24` | Event Hubs, Service Bus |
| `snet-sec-prd` | `10.60.34.0/24` | Key Vault, container registry |
| `snet-ai-prd` | `10.60.35.0/24` | Azure OpenAI |
| `snet-telemetry-prd` | `10.60.36.0/24` | Telemetry store nodes |
| `snet-egress-prd` | `10.60.37.0/26` | NAT gateway, egress proxy |

Pod CIDR is sized generously because resizing it requires a cluster rebuild.

### Private endpoints and NSGs

Every PaaS service is reached by private endpoint with public network access disabled at the resource, not merely restricted by firewall rule. Private DNS zones for PostgreSQL, Redis, blob, Key Vault, Event Hubs, container registry and Azure OpenAI, linked to each environment VNet; Azure-provided DNS, no custom servers.

NSGs default-deny inbound. Allowed flows: Front Door to `snet-aks-app-prd` on 443, restricted by service tag plus header check at ingress; app subnet to data subnet on 5432 and 6380; to messaging on 443 and 5671; to security and AI subnets on 443; telemetry workloads to the telemetry subnet. A final explicit deny-all rule is logged.

### Execution plane networking

| Rule | Detail |
| --- | --- |
| Inbound | **None.** No ingress rule exists; work arrives by polling the job queue outbound |
| Outbound, control planes | `AzureResourceManager`, `AzureActiveDirectory`, AWS STS and SSM endpoints, 443 only |
| Outbound, partners | Resolve Actions Pro HTTP Gateway and CloudBolt API, 443 only |
| Outbound, platform | Job queue and transcript storage private endpoints only |
| Everything else | **Denied.** No general internet, no package registries at runtime |
| Peering | None to the platform VNet |

A compromised execution worker has no path back into the application tier and no general internet egress; its credentials expire in minutes and are scoped to one target set.

### Egress and edge

All outbound platform traffic leaves through a NAT gateway with static IPs so partners can allowlist Getronics by address. Connector and benchmark traffic additionally passes an egress proxy enforcing a domain allowlist: connector endpoints, and benchmark sources (provider pricing and documentation, CIS, NIST, FinOps Foundation, NVD). Everything else is denied and logged.

Front Door Premium with WAF in prevention mode, OWASP core rule set plus bot protection, two origin groups behind separate hostnames for staff and customers so policy, rate limits and certificates are tuned independently. Origin access locked to Front Door by private link and header validation.

## Compute

| Setting | Production value |
| --- | --- |
| Cluster | `aks-cloudops-prd-weu`, private API server |
| Kubernetes version | N-1 of the latest supported minor, auto-upgrade on `stable` with a maintenance window |
| Network | Azure CNI Overlay, Cilium network policy |
| Identity | Workload Identity with OIDC issuer. **No pod-managed identity, no service principals** |
| Tier | Standard with uptime SLA, zones 1–3 on every pool |
| Node OS | Azure Linux, automatic security patching, `NodeImage` upgrade channel |
| Secrets | Secrets Store CSI driver with Key Vault provider, rotation enabled |
| Ingress | NGINX internal, Front Door private link to the internal load balancer |
| Local accounts | Disabled; Entra ID RBAC only |

| Pool | SKU | Autoscale | Purpose |
| --- | --- | --- | --- |
| `npsystem` | D4s v5 | 3–5 | `CriticalAddonsOnly`, system components |
| `npapp` | D4s v5 | 3–8 | Web, API, BFF |
| `npingest` | D4s v5 | 2–6 | Connectors, normalisers, identity resolution |
| `nptelemetry` | D8s v5 | 2–6 | Stream processing, memory-weighted |
| `npai` | D4s v5 | 2–4 | LLM gateway, embedding workers, Temporal |
| `npexec` | D4s v5 | 2–4 | `execution=true:NoSchedule`, **separate subscription and VNet** |

Spot instances are not used on any pool: a connector losing its node mid-batch is recoverable, an execution worker evicted mid-remediation is not, and mixing policies invites a mistake.

Pod controls: non-root with read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp; Azure Policy for AKS enforcing the baseline with signed-image verification on the execution namespace; requests and limits mandatory, memory limits always set; PodDisruptionBudgets on multi-replica deployments; default-deny NetworkPolicy per namespace; images only from `crcloudopsprdweu`. The execution namespace additionally disables service account token auto-mount except the job-queue identity and caps concurrency at one remediation per customer by default.

## Data tier

### PostgreSQL Flexible Server

General Purpose D4ds v5 (4 vCPU, 32 GB) scaling to D16ds v5, version 16, zone-redundant HA, 512 GB P30 with autogrow, 35-day geo-redundant backup, private access only, TLS 1.2 minimum. Extensions: `pgvector`, `pg_stat_statements`, `pgcrypto`, `pg_partman`.

| Parameter | Value | Reason |
| --- | --- | --- |
| `max_connections` | 400 | Services use PgBouncer |
| `work_mem` | 16 MB | Reporting queries sort large sets |
| `maintenance_work_mem` | 1 GB | Index builds on cost and ticket tables |
| `random_page_cost` | 1.1 | Premium SSD |
| `log_min_duration_statement` | 500 ms | Slow query capture |
| `row_security` | on | Non-negotiable |
| `statement_timeout` | 30 s application, 300 s reporting | Stops a runaway dashboard query |
| `idle_in_transaction_session_timeout` | 60 s | Connection hygiene |

**PgBouncer in transaction mode**, one pool per role. Transaction mode matters because tenant scope is set with `SET LOCAL` inside each transaction; session mode would leak scope between requests, which is a correctness and security defect rather than a performance choice. One async read replica carries reporting and pack generation; replica lag is monitored and surfaced as data freshness, not hidden.

### Telemetry store

Column store (ClickHouse via operator, or Azure Data Explorer), 3 nodes initially scaling to 5, premium SSD v2, partitioned by day and ordered by `(tenant_id, resource_id, metric, ts)`. TTL: full grain 90 days, 5-minute rollup to 13 months, hourly to 3 years. TLS only, per-service users, no default user.

**Tenant isolation here is enforced by query construction, not row-level security** — the weakest isolation boundary in the platform. Every telemetry query goes through a single wrapper in the `tenancy` package that injects the tenant predicate and rejects any query not built through it. Raw client access from application code fails CI, and the isolation suite exercises this path explicitly.

### Redis, storage, Event Hubs

Redis Premium P1 (6 GB, zone-redundant) to P3; TLS required, non-TLS port disabled; AOF persistence; `volatile-lru`; databases 0 sessions, 1 query cache, 2 rate limits, 3 approval state. Approval state is cached here for latency but authoritative in Temporal — Redis loss must never allow an action to proceed unapproved, so the broker re-reads the authoritative record before minting.

| Storage account | SKU | Purpose | Lifecycle |
| --- | --- | --- | --- |
| `stcloudopsbronzeprdweu` | Standard GZRS | Raw connector payloads | Hot 30 d, cool 90 d, archive 13 m, delete 7 y |
| `stcloudopsreportsprdweu` | Standard ZRS | Packs and evidence bundles | Hot 90 d then cool |
| `stcloudopsexecprdweu` | Standard ZRS | Session transcripts | **Immutable, time-based WORM, 7 years**, separate subscription and CMK |
| `stcloudopstfstateprdweu` | Standard ZRS | Terraform state | Versioned, locked |

All accounts: HTTPS only, TLS 1.2, shared key access disabled, Entra ID authorisation only, soft delete 30 days, infrastructure encryption on.

Event Hubs Standard with 4 throughput units auto-inflating to 12 (Premium at design scale): `evh-metrics` 8 partitions with 3-day retention and capture to bronze, `evh-config` 4 partitions with 7-day retention. One consumer group per service, never shared. Managed identity, no SAS keys. Partition count cannot be reduced later, so it is sized for design scale from the start.

## Identity, secrets and keys

### App registrations and identities

| Registration | Permissions |
| --- | --- |
| `app-cloudops-web` | `openid`, `profile`, `email`, delegated platform scopes |
| `app-cloudops-api` | Exposes `access_as_user`, `platform.read`, `platform.write`, `platform.execute` |
| `app-cloudops-graph` | `User.Read.All`, `AuditLog.Read.All`, `RoleManagement.Read.Directory`, read-only |
| `app-cloudops-sharepoint` | **`Sites.Selected` only**, granted per customer site. Never `Sites.Read.All` |
| `app-cloudops-snow` | Read scope; a second registration for the field-scoped write user |

Tenant-wide SharePoint consent would give the platform read access to every site in the Getronics tenant; per-site grants mean a customer's documents are reachable only by an explicit, auditable, individually revocable assignment.

User-assigned managed identities, one per workload, federated to the Kubernetes service account: `id-cloudops-api-prd`, `-ingest-`, `-telemetry-`, `-ai-`, `-workflow-`, `-exec-` and `-cicd` (federated to GitHub OIDC, no standing secret). The execution identity holds customer role assumption, signing operations and transcript write, and has no platform database access beyond the job queue.

### Platform RBAC

Assignments at resource group scope, never subscription-wide, always to groups. One recorded exception (ADR 0023): Cost Management Reader for the ingest identity and a role-assignment reader for the pipeline identity, both read-only, on the platform's own subscriptions, for the live feed of the platform's cost. `grp-cloudops-platform-admin` holds Contributor via PIM with 8-hour activation and approval; engineers hold Contributor in non-production and Reader plus namespace-scoped operator in production; security holds Security Reader and Key Vault Reader metadata only; `grp-cloudops-break-glass` holds Owner on the execution subscription via PIM with 1-hour activation, **dual approval** and alerting on activation. **No standing production write access for any human.** Quarterly access recertification.

### Customer environment access

| Cloud | Mechanism | Read role | Action role |
| --- | --- | --- | --- |
| Azure | Lighthouse delegation per customer | Reader, Cost Management Reader, Monitoring Reader | Custom role limited to the operations approved runbooks need |
| AWS | `AssumeRole` with per-customer external ID | `ReadOnlyAccess`, Cost Explorer | Custom policy scoped to SSM session start, specific document ARNs, tagged resources |

The read role is assumed continuously by ingestion. The action role is assumable **only** by `id-cloudops-exec-prd`, only after an approval record exists, and its trust policy requires a session tag carrying the approval reference. An attacker holding the ingestion identity cannot act; one holding the execution identity cannot act without a corresponding approval.

### Vaults and keys

| Store | Holds | Access |
| --- | --- | --- |
| `kv-cloudops-app-prd-weu` | Application config, connector tokens, OAuth secrets | Workload identities, get only |
| `kv-cloudops-exec-prd-weu` | External IDs, execution config | `id-cloudops-exec-prd` only, separate subscription |
| `kv-cloudops-keys-prd-weu` | Runbook signing key, transcript and database encryption keys | Key Vault **Premium**: HSM-backed, non-exportable; sign and wrap only, inside the service |

**Why Premium rather than a dedicated pool.** The signing key is the one credential that, if stolen, would let an attacker author a runbook the platform would execute. Premium provides HSM-backed non-exportable keys as a shared service — signing happens inside the service and the key never exists in application memory. A dedicated managed HSM pool costs several thousand euro a month per pool, and two if DR is covered, for a marginal gain. If assurance later demands one, it is a key migration, not a redesign.

| Key | Type | Rotation |
| --- | --- | --- |
| `key-runbook-signing` | RSA-3072, HSM-backed, non-exportable | 2 years with overlap |
| `key-transcript-cmk` | RSA-3072 | 1 year |
| `key-db-cmk` | RSA-3072 | 1 year |
| `key-token-signing` | EC P-256 | 90 days |

Rotation with overlap: the previous key stays valid for verification for one full period, so existing signed artefacts remain verifiable without re-signing.

**Secret handling.** No secrets in code, config, images, environment variables, logs or LLM prompts — enforced by CI secret scanning, CSI driver file mounts, a logging redaction filter, and gateway-side redaction. Connector credentials rotate at 90 days. Customer credentials are never stored.


![Request lifecycle and tenant scoping](05-request-lifecycle.png){width=6.6in}

### Credential minting sequence

1. The execution workflow presents an approval reference and target set to the broker.
2. The broker verifies the approval in Temporal — not Redis — confirming it has not expired or been consumed.
3. It verifies the runbook signature against the signing vault and that the version matches the one approved.
4. It checks the customer's change freeze and maintenance window.
5. It resolves the target set against tenant scope and rejects anything outside it.
6. It assumes the customer action role, passing the approval reference as a session tag, 15-minute duration.
7. Credentials pass to the worker in memory — never written to disk, never logged.
8. Session output streams to WORM storage as produced, so a crashed session still leaves evidence.
9. On completion or timeout the session is revoked explicitly rather than left to expire.

**Single-use.** The approval reference is consumed at step 6; a replayed approval fails at step 2.

## Database design

### Conventions

Schemas `cloudops`, `staging`, `audit`. `uuid` primary keys for domain entities, `bigserial` for high-volume append tables. `snake_case`, singular table names. `timestamptz` always — no naive timestamps. Money as `numeric(18,6)` paired with a currency column, never `float`. Soft delete with partial indexes. `jsonb` with GIN indexes where queried, never as a substitute for columns.


![Source to canonical mapping](03-data-mapping.png){width=6.6in}
### Row-level security

```sql
ALTER TABLE cloudops.<table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.<table> FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON cloudops.<table>
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
```

`FORCE` matters: without it the table owner bypasses the policy, and migrations run as the owner. Commercial tables carry a second restrictive policy so a customer role inside its own tenant still cannot read margin or rate-card data:

```sql
CREATE POLICY commercial_visibility ON cloudops.sow
  AS RESTRICTIVE
  USING (current_setting('app.role', true) IN
         ('ceo','cfo','finops','delivery_lead','commercial','auditor','platform_admin'));
```

Scope is set per transaction by the `tenancy` package with `SET LOCAL app.tenant_ids` and `SET LOCAL app.role`. `SET LOCAL` rather than `SET` is what makes transaction pooling safe.

### Multi-currency

```sql
CREATE TABLE cloudops.fx_rate (
  id              bigserial PRIMARY KEY,
  from_currency   char(3) NOT NULL,
  to_currency     char(3) NOT NULL,
  rate            numeric(18,8) NOT NULL CHECK (rate > 0),
  rate_type       text NOT NULL CHECK (rate_type IN ('closing','average')),
  effective_date  date NOT NULL,
  source          text NOT NULL,
  UNIQUE (from_currency, to_currency, rate_type, effective_date)
);
```

Amounts are stored in transaction currency with a sibling `*_currency` column. Conversion uses the rate in force on the transaction date, never today's rate on history. Flows use average, balances use closing — `rate_type` is a required argument of the conversion function. Conversion happens at the serving layer; silver tables never store converted values. Views return amount, currency, rate, rate date, converted amount and reporting currency. A missing rate raises rather than returning zero.

**Margin is computed in the contract currency first, then converted.** Converting first and subtracting afterwards bakes in whichever rate applied and makes a currency movement look like a margin movement.

### Partitioning and indexes

`cost_fact` monthly on `usage_date` (7 years), `work_item` quarterly on `opened_at`, `vulnerability` quarterly on `first_detected`, `audit.action_log` monthly (7 years, never dropped), `message` monthly (12 months then summarised). `pg_partman` manages creation and retention nightly and alerts on failure — a missing future partition is an outage at midnight.

| Table | Index |
| --- | --- |
| `tenant_alias` | `(alias_norm)`, `(tenant_id)` |
| `resource` | `(tenant_id) WHERE deleted_at IS NULL`, `(name_norm)`, GIN on `tags` |
| `work_item` | `(tenant_id, kind, opened_at DESC)`, `(resource_id)`, `(tenant_id) WHERE is_workaround` |
| `cost_fact` | `(tenant_id, usage_date)`, `(tenant_id, charge_class, usage_date)` |
| `vulnerability` | `(tenant_id, severity, remediated_at)`, `(tenant_id) WHERE remediated_at IS NULL` |
| `knowledge_chunk` | HNSW on `embedding`, `(tenant_id, source_kind)` |
| `global_knowledge_chunk` | HNSW on `embedding`, `(source_kind)` |
| `audit.action_log` | `(tenant_id, created_at DESC)`, `(actor, created_at DESC)` |

The `tenant_id` leading column is deliberate: with RLS applied the tenant predicate is on every query, so it belongs first.

### Migrations

Expand-then-contract, five steps, never compressed: add nullable or defaulted structure; write to both; backfill in throttled resumable batches; read from new; remove the old in a later release. No `ADD COLUMN NOT NULL` without a default. `CREATE INDEX CONCURRENTLY` always. `lock_timeout = 3s` so a blocked migration fails fast rather than queueing the application behind it. Every new RLS policy ships with its isolation test. Migrations run as a dedicated role, applied by a pipeline job — never by a human or an application startup hook.

## Connectors and execution plane

![Integration map](02-integration-map.png){width=6.6in}

### Connector contract

Authenticate from Key Vault via workload identity; watermark per source and entity, advanced only after a batch commits; fetch with a five-minute overlap to tolerate clock skew; **persist the raw payload to bronze before parsing anything**; normalise, retaining unknown fields in `raw_extra`; resolve identity; commit transactionally with the watermark inside the same transaction; report records, lag, quarantine depth and error rate.

Bronze-before-parsing matters because a normalisation defect is found weeks later, and reprocessing from the raw payload is the only way to fix history without re-querying a source that may have changed. Replay reads a bronze date range and re-runs normalisation without re-fetching; it is idempotent because upserts key on the source's natural key.

| Source | Path | Limit handling | Concurrency |
| --- | --- | --- | --- |
| ServiceNow | Read and act | Honour `X-RateLimit-Remaining`, back off at 20% | 4, 200 records per page |
| Qualys | Read | One report pull at a time per customer | 1 |
| CoreStack | Read | Conservative default until measured | 2 |
| CloudBolt | Act | Conservative default until measured | 2 |
| Resolve Actions Pro | Act | Per gateway limits | 2 |
| Azure Resource Graph | Read and act | Honour `x-ms-user-quota-remaining` | 2, 1000 rows per page |
| AWS Config | Read and act | Exponential backoff on throttling | 4 across accounts |
| Microsoft Graph, SharePoint | Read and act | Honour `Retry-After` absolutely | 2 |

Retry: exponential backoff with jitter, five attempts, then dead-letter with an alert — replayable without re-fetching because the payload is already in bronze.

### Identity resolution

Customer: exact match on `tenant_alias`, then normalised (lowercase, strip punctuation and legal suffixes — `limited`, `ltd`, `bv`, `gmbh`, `sa`, `plc`, `inc`, `group`, `holdings`, `services`, held as configuration not code), then quarantine. Resource: external key, then `name_norm` plus IP, then `name_norm` alone, then quarantine. Tickets link to resources on `configuration_item`; unlinked values are recorded, counted and reported as a CMDB quality metric rather than hidden.

**Quarantine depth is an alerting metric.** A rising quarantine means an upstream naming change nobody announced, and every row in it is a figure missing from a dashboard.


![Remediation workflow and approval state machine](04-remediation-workflow.png){width=6.6in}
### Runbook manifest

A runbook is a signed, versioned artefact. The agent selects and parameterises one; it never composes commands.

```yaml
apiVersion: cloudops/v1
kind: Runbook
metadata:
  id: restart-windows-service
  version: 3
  actionClass: B                    # A | B | C | D | E
  platforms: [azure, aws, resolve]
  resourceTypes: [compute.vm]
spec:
  parameters:
    - name: serviceName
      type: string
      pattern: '^[A-Za-z0-9_.-]{1,64}$'
      required: true
  blastRadius: { maxTargets: 1 }
  preConditions: [service-exists, not-in-change-freeze, resource-tagged-environment]
  steps:    [capture-state, restart]
  rollback: [restore-state]
  postConditions:
    - id: service-running
      timeoutSeconds: 120
signature: { keyId: key-runbook-signing, algorithm: RS256 }
```

Parameters are typed and pattern-bounded, validated before the signature check. No string interpolation into commands — scripts take parameters as native arguments and the validator rejects interpolation syntax. Rollback is required for class C and above. The post-condition is separate from the action: a runbook that cannot prove its own success is not approved. The signature covers the whole spec, so any edit invalidates it.

### Approval state machine

Approval state lives in Temporal; Redis caches it for latency only.

| State | Leaves to |
| --- | --- |
| `planned` | `policy_check` |
| `policy_check` | `awaiting_approval`, or `denied` |
| `awaiting_approval` | `approved`, `rejected`, `expired` |
| `approved` | `executing` (reference minted, single-use) |
| `executing` | `verifying`, `rolling_back` |
| `verifying` | `succeeded`, `rolling_back` |
| `rolling_back` | `rolled_back`, `escalated` |
| `succeeded` | Terminal — ticket updated, evidence written |
| `escalated` | Terminal — human owns it, paging alert raised |

**Timeouts are cancellations.** An expired approval ends the workflow; it never proceeds on timeout and never auto-renews.

Circuit breakers: rollback rate above 10% in an hour suspends automated execution for that customer; any `escalated` outcome suspends that runbook across all customers; verification failure above 20% suspends that runbook.

**Kill switch**, a product feature with a named owner and tested procedure: a flag on the tenant record makes the broker refuse to mint; platform-wide, a feature flag plus scaling `npexec` to zero; emergency, revoking the execution identity's role assumption rights. Tested quarterly within the DR exercise.

Every execution writes an action-log entry (7 years, immutable), a WORM session transcript (7 years), a ServiceNow work note, and for class C and above a change record. Transcripts stream as output is produced, so a crashed session still leaves evidence of what it did.

## AI platform configuration

Three deployments in one Azure OpenAI resource, private endpoint only, managed identity authentication with no API keys, abuse-monitoring opt-out requested so no prompt content is retained, and deployments pinned to an explicit model version.

| Tier | Purpose | Alias |
| --- | --- | --- |
| `small` | Intent classification, extraction, re-ranking, simple answers | `cloudops-small` |
| `large` | Remediation planning, multi-step reasoning, narrative drafting | `cloudops-large` |
| `embed` | Chunk and query embedding | `cloudops-embed` |

Application code names a tier, never a model. Which published model fills each tier is chosen at build from regional availability, recorded in an ADR, and changed only through the evaluation gate. The gateway falls back to the secondary provider on throttling and to `small` if `large` is unavailable.

### Vector store

`vector` extension on the platform database; `knowledge_chunk.embedding vector(N)` with N fixed by the embedding deployment; HNSW index with cosine distance, `m=16`, `ef_construction=64`, `ef_search` tuned against the evaluation set; row-level security identical to every other tenant table, with `tenant_id NOT NULL`; hybrid search fusing vector similarity with full-text by reciprocal rank, re-ranked by the `small` tier.

Each customer chunk carries its citation: the source `document`, its ordinal and the page it starts on. Connectors write chunks with their tenant and no embedding; the knowledge indexer embeds them one tenant's scope at a time and fills only an embedding that is still empty (ADR 0020).

Getronics-wide content (shared runbooks, best practice) lives in `cloudops.global_knowledge_chunk`, which has no `tenant_id`. Every scope can read it, and no application role can write it (ADR 0001). Customer content is never tenant-less. The assistant's user-facing help pages live there too, as `platform_help` (every scope) and `platform_help_staff`; a row-level policy keeps everything but `platform_help` from a customer scope (ADR 0029).

The retrieval query carries the tenant predicate **inside** the SQL, so RLS applies to retrieval exactly as to any other read. Each branch is ordered and limited so it can use its own HNSW index:

```sql
SELECT id, title, body, tenant_id, 1 - distance AS score FROM (
  (SELECT id, title, body, tenant_id, embedding <=> $1 AS distance
   FROM cloudops.knowledge_chunk
   WHERE tenant_id = ANY(cloudops.current_tenant_ids())
     AND source_kind = ANY($2)
   ORDER BY embedding <=> $1 LIMIT 20)
  UNION ALL
  (SELECT id, title, body, NULL::uuid, embedding <=> $1
   FROM cloudops.global_knowledge_chunk
   WHERE source_kind = ANY($2)
   ORDER BY embedding <=> $1 LIMIT 20)
) candidates
ORDER BY distance LIMIT 20;
```

**Azure AI Search fallback:** stand up an index with the same chunk schema and a tenant field, point the gateway's retrieval interface at it, run both in parallel against the evaluation set before switching. The isolation suite gains a case, because AI Search enforces tenant filtering by query construction rather than by row-level security — which is precisely why it is the fallback.

### Gateway controls

Model routing by task; per-user daily and per-tenant monthly token budgets with alert at 80% and throttle at 100%; 20-turn conversation ceiling; **10 tool-call iterations then escalate to a human**; semantic response cache with 1-hour TTL for live-state and 24-hour for static knowledge; rolling context summarisation; and a spend circuit breaker when the rate exceeds a multiple of the trailing average. The loop ceiling and circuit breaker together are what prevent a looping agent producing a five-figure bill over a weekend.

The assistant (ADR 0029) has its own sub-budget inside these: 40% of the tenant's month and a per-user daily cap. Its conversations are kept 90 days, visible to their author only by a restrictive policy on `app.principal_id`, which the `tenancy` package sets with `SET LOCAL` like the tenant ids.

Each tool declares its required role, read or write nature, and tenant scope requirement. The gateway refuses a call whose declared role is not held by the calling principal before it reaches any service. Arguments are JSON Schema validated — an invalid argument is a refusal, not a best-effort call. Tenant scope is resolved server-side, never from a model-supplied argument. Write tools declare their action class, route through the approval machine, and take an idempotency key.

### Evaluation

Runs in CI on every prompt, model, retrieval or tool change; a regression blocks the release.

| Suite | Gate |
| --- | --- |
| Factual accuracy against a golden set | No regression against baseline |
| Groundedness — every claim traceable | No uncited factual claims |
| **Cross-tenant leakage** | **Zero tolerance** |
| Prompt injection via ticket text, tags, documents | Zero successful instruction-following |
| Action safety on ambiguous tickets | Zero unsafe plans |
| Currency correctness | Exact |
| Cost per interaction | Within budget band |

The golden set is versioned alongside the code and grows from production corrections: every engineer override of an agent classification becomes a candidate test case.

## Observability, CI/CD and recovery

![Design methodology and release pipeline](08-delivery-method.png){width=6.6in}

### Observability

Azure Monitor managed Prometheus and Grafana for metrics; Log Analytics with structured JSON, 90 days hot then archive; OpenTelemetry traces at 10% sampling with 100% on errors and all execution workflows; LLM tracing capturing prompt, retrieval set, tool calls, tokens, latency and cost per request; synthetic availability tests per role; Defender for Cloud and Containers, with the action log in Sentinel.

| SLO | Target |
| --- | --- |
| Platform availability | 99.5% initially, 99.9% once on-call is staffed |
| Dashboard p95 | Under 2 s |
| API p95 | Under 500 ms |
| AI first token | Under 3 s |
| Ticket ingest lag | Under 5 min |
| Telemetry ingest lag | Under 2 min |
| Cost reconciliation drift | Under 0.5% monthly |
| Remediation verification success | Above 95% |

Error budget policy: two consecutive months below an availability SLO freezes feature work until reliability is restored.

**Alerts that always page, regardless of hour:** any cross-tenant query attempt, any unsigned runbook execution attempt, any credential mint without a valid approval, any signing-key operation outside the expected pattern, and any break-glass activation. P2 pages cover connector outage beyond three intervals, reconciliation drift, rollback rate breach and the AI spend circuit breaker.

Instrumentation (ADR 0022): services export OTLP to an in-cluster OpenTelemetry Collector, which tail-samples and exports traces and logs to Application Insights as its own workload identity, and metrics to managed Prometheus. **No tenant attribute leaves the database for Azure Monitor**: spans, logs and metrics are allowlisted and scrubbed in each process and again in the collector; per-tenant LLM attribution stays in `llm_call` and `agent_run` under RLS, joined by trace id. A ticket is one trace from first sighting to closure (`work_item.trace_parent`). Alert rules and the six dashboards are Terraform (stacks 03, 03b).

### CI/CD

Monorepo: `apps/web`, `services`, `connectors`, `execution`, `runbooks`, `packages/tenancy`, `packages/contracts`, `packages/ui`, `db/migrations`, `infra/terraform`, `infra/gitops`, `evals`, `docs`.

Pipeline stages, each failing the build: lint and types; unit tests below 80% on calculation and tenancy code; **any data access outside `packages/tenancy`**; any monetary literal outside the rate card module; secret scan; SAST and SCA critical or high; signed image build; container scan; contract tests; integration tests; **the isolation suite**; **the AI evaluation suite**; pricing regression against the reference case; Terraform plan with `conftest` policy checks.

Runbooks follow a stricter path: author, static validation (schema, parameter bounds, no interpolation, rollback present for class C and above), dry-run against sandbox accounts, peer review by someone other than the author, signing by the pipeline identity on the protected branch only, publication with a version. A runbook cannot be signed from a feature branch and a human cannot sign one directly — the vault grants the sign operation to the pipeline identity alone.

Delivery: trunk-based with feature flags; Argo CD pulls from `infra/gitops` and the pipeline never applies to the cluster; Argo Rollouts canary at 10% for 15 minutes with automatic rollback; Test automatic, UAT and Production approval-gated with a change record; migration job before application rollout, expand-only; rollback by reverting the Git commit. The execution plane deploys separately with a security review on every change to broker or validator code.

Infrastructure as code: Terraform with remote state and locking, reusable modules, one stack per environment, GitHub OIDC federation with no service principal secrets, `conftest` at plan time, nightly drift detection reported but never auto-corrected in production. Manual changes are prohibited and the `managedby` tag is audited.

### Backup and recovery

| Asset | Method | Retention | Restore target |
| --- | --- | --- | --- |
| PostgreSQL | Native geo-redundant, continuous WAL | 35 days PITR | Under 2 h |
| Telemetry store | Daily backup, hourly incremental | 30 days | Under 4 h |
| Bronze blob | GZRS with versioning | Lifecycle to 7 years | Immediate |
| Transcripts | WORM, immutable | 7 years, undeletable | Immediate |
| Redis | AOF | 7 days | Rebuild acceptable |
| Terraform state | Versioned blob | 90 days | Immediate |
| Keys and secrets | Soft delete, purge protection | 90 days | Immediate |

Warm standby, not active-active: the recovery objectives do not justify a second live region, and active-active would double the surface that can reach customer estates. **The execution plane is deliberately not failed over automatically** — it stays down until a human confirms the restored environment is intact.

Recovery sequence: declare and confirm the kill switch is engaged; restore PostgreSQL into the paired region; apply Terraform and let Argo CD reconcile; restore the telemetry store and backfill from bronze; switch Front Door origins and validate both hostnames; resume connectors from their watermarks and verify no gap against bronze; reconcile cost and ticket counts against source systems before publishing any figure; then re-enable execution with a staged ramp.

Testing: monthly automated backup restore to a scratch environment; quarterly full DR failover; quarterly kill-switch verification; quarterly immutability verification by attempting a delete and confirming it fails. An untested backup is an assumption.

## Test, capacity and build sequence

### Test levels

| Level | Tooling | Runs |
| --- | --- | --- |
| Unit — logic, pricing, currency, normalisers | pytest, Jest | Every commit |
| Contract — connector shapes | Recorded fixtures | Every commit |
| Integration — real database, cache, queues | Testcontainers | Every commit |
| **Isolation — cross-tenant through every path** | Custom suite | Every commit, release blocker |
| Cloud integration — sandbox Azure and AWS | Scheduled | Nightly |
| Execution safety — runbook dry runs | Custom harness | Every runbook change |
| AI evaluation | Custom harness | Every AI change, release blocker |
| End to end — journeys per role, both audiences | Playwright | Every release |
| Performance at design volume | k6 | Pre-UAT, pre-release |
| Security — SAST, DAST, SCA, container, penetration | Pipeline plus external | Continuous, plus annual |
| Chaos — node loss, failover, connector failure | Game day | Quarterly |
| UAT — persona scripts with sign-off | Manual | Per release train |

### Isolation cases that must exist

| Case | Expected |
| --- | --- |
| API request for another tenant's resource by ID | 404, not 403 — a 403 confirms the resource exists |
| Direct SQL with scope unset | Zero rows, not all rows |
| Vector retrieval naming another customer | No foreign chunks returned |
| Assistant asked to compare two customers by a single-tenant user | Refusal |
| Credential minted for customer A used against customer B | Broker rejection before any cloud call |
| Customer principal requesting a margin or rate-card field | Field absent, enforced by RLS not serialisation |
| Customer principal with a tampered organisation claim | Scope resolved server-side, claim ignored |
| Engineer in tenant scope requesting commercial data | Restrictive policy denies |

Test data: synthetic for dev and test, **across multiple currencies**; a masked pilot subset for UAT with names pseudonymised, amounts scaled and currencies preserved; synthetic at design volume for performance. A single-currency fixture set will pass while a multi-currency production breaks, because the defects live in conversion, rate selection and rounding.

### Capacity and cost control

Scaling: web and API on CPU 65% or requests per pod (3–12); connectors on queue depth, capped by source rate limits; telemetry on consumer lag; AI gateway on concurrent requests; execution workers on job queue depth with **a hard ceiling of one concurrent remediation per customer**.

Review capacity when any of these is reached rather than when something breaks: PostgreSQL CPU 60% over 7 days or storage 70%; connection pool 80%; telemetry disk 65%; Event Hub throughput 70%; Redis memory 70%; a node pool at maximum for more than an hour a day; p95 latency within 20% of SLO.

Cost levers: `schedule: business-hours` tag driving automated shutdown; 3-year reservations on production database and steady nodes once sizing is proven; compute savings plan after three months of stable run rate; **a metric allowlist per resource type** with downsampling beyond 90 days; Log Analytics commitment tier; storage lifecycle tiering; AI routing, caching and quotas; budget alerts at 70, 90 and 100%.

**The metric allowlist is the single largest lever.** Collecting every metric a provider emits at one-minute grain for thirteen months costs several times what collecting the metrics that drive a decision costs. Platform cost scales with telemetry and AI usage, not user count.

### Build order

![Implementation steps and build order](06-build-order.png){width=6.6in}

Each step is a Terraform stack that must apply cleanly before the next: management groups, subscriptions and policy; networking including NAT and egress proxy; Key Vault Premium and managed identities with signing keys created here; observability; data tier; AKS without `npexec`; container registry, OIDC and pipelines; Argo CD and the GitOps structure; telemetry store; Azure OpenAI with private endpoint; Front Door, WAF and both hostnames; finally the execution subscription, `npexec`, broker and transcript storage.

Steps one to eight are the walking skeleton — something deployable should exist by then, with the rest adding capability rather than unblocking it.

### Open items for the build

| Ref | Item | Decide by |
| --- | --- | --- |
| L1 | Telemetry store self-managed or managed PaaS | Before the telemetry step |
| L2 | Dedicated managed HSM, if ever | Only if an assurance requirement demands it |
| L3 | Metric allowlist per resource type — defines the largest cost line | Before the data tier |
| L4 | FX rate provider and ingest method — **decided** (ADR 0019): ECB reference rates, Finance corrections recorded in `fx_restatement` | Before the commercial modules |
| L5 | Reporting currency default per customer | Before the portal |
| L6 | PgBouncer in-cluster or managed pooling — transaction mode mandatory either way | Before the data tier |
| L7 | Sandbox cloud account provisioning | Before pipelines |
| L8 | Customer IdP federation pattern | Before the portal |
| L9 | Runbook signing ceremony and key-compromise procedure | Before the execution step |
| L10 | Session transcript redaction | Before the execution step |
| L11 | Vector store — pgvector or Azure AI Search | Before the knowledge module |
| L12 | Azure OpenAI model per tier | Before the Azure OpenAI step |
| L13 | Evidence-pack signing key: a new HSM-backed `key-evidence-signing`, or reuse of runbook signing (ADR 0020) | Before any evidence pack leaves the platform |

**L10 deserves attention.** Transcripts may capture customer data incidentally — a log line, a config value, a connection string. Redacting before storage protects against that but weakens the evidence value of the transcript. The answer depends on what customers have agreed to, which makes it a contractual question as much as a technical one.

### Definition of done

Every resource created by Terraform with no manual resources; Azure Policy compliance at 100%; mandatory tags everywhere; no public network access on any data service; backup restore tested into a scratch environment; DR failover executed and objectives met; kill switch verified to fail a credential mint; penetration test complete with findings closed or formally accepted; runbooks for every P1 and P2 alert; on-call rota staffed and escalation tested.

This is a living document: every architectural change raises an ADR and updates the affected section in the same change. A section that no longer matches the deployed environment is worse than no section at all.
