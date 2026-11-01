# ADR 0038: Cloud Services, a continuous read-only audit of customers' cloud accounts

- **Status:** Proposed, 2026-11-01 (accepted on merge)
- **Decided by:** the coordinating session's brief (scope, data sources, roles, cadence);
  decisions 4 and 5 are modelled and open to the user's revision
- **Implements:** the user's request for continuous audit of cloud accounts and their
  resources (backup, configuration, policy, cost per CI, best-practice recommendations,
  drill-down). HLD M3 (inventory) and M13 (posture), for what a customer's own Reader
  credential can read. `connectors/src/cloudops_connectors/azure/audit.py`,
  `audit_source.py`, the client's `graph` and `assessments`, the customer source's
  cadence; migration `0036_cloud_services`; `services/api/src/cloudops_api/cloud_services.py`;
  `apps/web/src/screens/CloudServices.tsx` and `src/app/cloud-services/`; the seed's
  `cloudops_devdata.cloud_audit`.
- **Builds on:** ADR 0003 and ADR 0027 (stored read-only customer credentials, non-prod),
  ADR 0016 (the connector contract), ADR 0018 (customer roles and the agreed role set),
  ADR 0021 (a customer sees no provider cost), ADR 0032 (the customer selector), ADR 0037
  (six-hourly sync, once on onboarding).

## Context

A customer's Azure subscription is onboarded with an app registration holding Reader,
Cost Management Reader and Monitoring Reader (ADR 0027). The platform read only its
resource inventory and cost. The user wants each account audited continuously: is each
VM or database backed up, how are resources configured, which policies do they fail,
what does each resource or CI cost, and what do Azure's own best-practice engines
recommend, with a drill-down from customer to resource.

Checked live on 2026-11-01 against the dev test customer (Azure Sponsorship), with its
own credential, before any code was written:

| Read | Answer with Reader |
| --- | --- |
| Resource Graph `recoveryservicesresources` (protected items, policies) | 200: 93 protected items |
| Resource Graph `policyresources` (policy states, assignments) | 200: 3,002 states |
| Resource Graph `advisorresources` (recommendations) | 200: 1,150 recommendations |
| Resource Graph `securityresources` (Defender assessments) | 200, **no rows** |
| ARM `GET .../providers/Microsoft.Security/assessments` | 200: 655 assessments |
| Resource Graph `Resources` (per-type settings) | 200: 226 resources of the audited types |

**No role beyond Reader is needed.** Nothing was granted or asked for.

## Decisions

1. **Five audit entities on the customer credential's Azure source.** `backup_item`,
   `policy_state`, `advisor` and `resource_config` are fixed Resource Graph queries
   (`audit.QUERIES`) over the credential's own subscriptions; `defender` is Defender
   for Cloud's assessments listing per subscription, with metadata, read from ARM
   because Resource Graph's `securityresources` answered no rows where ARM listed 655.
   They run only on a source bound to a credential (`AzureSource` refuses them
   otherwise), so the client's `allowed` keeps every request to the customer's own
   subscriptions (ADR 0027, decision 4). The connector contract holds: each page is in
   bronze before it is parsed; a malformed row is rejected, counted and left there; a
   subscription outside the binding is quarantined; drift is recorded per entity.
2. **Each run is a snapshot.** On the run's last page, only when it advances and in that
   page's transaction, every open check of the kind not seen since the run's first page
   was fetched is resolved, and the watermark moves. A run that fails part-way resolves
   nothing; a replay changes nothing (a page older than what a check holds is skipped).
3. **One table of checks and one of history.** `cloudops.cloud_check` holds the current
   state of each check (unique on account, kind and the provider's id): the thing checked
   (lowercased ARM id, linked to `cloudops.resource` where inventory holds it), a state
   (`ok`, `issue`, `not_applicable`), a severity (`high`, `medium`, `low`, `info`), the
   provider's own status, and a `detail` of fixed keys. `cloudops.cloud_check_event`
   records each change: `opened` (first seen as an issue), `changed` (naming the fields,
   `settings.vmSize` for a configuration), `resolved` and `reopened`. Both are tenant
   data with FORCE RLS, `tenant_isolation`, the restrictive `account_scope`, isolation
   tests and the schema classification checks. Readers get every column but `raw_ref`.
   CoreStack's `security_finding` (ADR 0028) stays as it is: it is a different source,
   read only where a customer's CoreStack tenant is mapped.
4. **Severity is mapped, and marked as the platform's.** Advisor's impact and Defender's
   severity map one to one. A failed or stopped backup is `high`, a protected item never
   backed up `medium`. A non-compliant policy state is `medium` when its effect is deny,
   deployIfNotExists or modify, `low` for audit. These are the platform's policy, not the
   provider's, and are open to revision.
5. **Configuration rules are modelled** (`audit.RULES`): one projected setting each, at a
   fixed severity, for VMs (public IP, encryption at host, boot diagnostics), storage
   (public blob access, HTTPS only, TLS 1.2, open network, shared key), SQL servers and
   databases (TLS, public network, backup redundancy), PostgreSQL and MySQL flexible
   servers (public network, retention under 7 days, geo-redundant backup), key vaults
   (soft delete, purge protection, open network), AKS (RBAC, public API), web apps
   (HTTPS only), disks (unattached, open export) and public IPs (unattached). Each
   resource also gets one `config.settings` check holding every projected setting, so a
   change to any setting is in its history. A rule that cannot read its setting is
   `not_applicable`, never a pass.
6. **Backup coverage means Azure Backup for VMs.** A VM in an audited account with no
   open protected item is "without backup". PaaS databases back themselves up; their
   retention and redundancy are configuration rules (decision 5).
7. **Cadence: every six hours, and once on onboarding** (ADR 0037). The five entities are
   in the customer credential's schedule at `sync.CUSTOMER_SYNC`; a passed onboarding or
   rotation triggers every schedule of the credential, these included. Connector health
   expects the same interval (`staging.connector_schedule`).
8. **Who sees it.** A new permission, `cloud_services.read`, gates the API and the console
   area. Granted to every system staff role that reads inventory (CEO, CFO, FinOps,
   SecOps, Delivery lead, Auditor, Platform administrator, Engineer), and to two customer
   roles, **Architect** and **Security officer**, for their own tenant only. A customer
   principal resolves only while its role is in the tenant's agreed role set (ADR 0018),
   so a customer whose agreement leaves those roles out sees nothing. The other customer
   roles do not hold it; an administrator can change the matrix.
9. **Cost per resource is staff only.** A resource's, group's, account's and customer's
   cost over the chosen dates and the period of the same length before it, per currency,
   unconverted, from `cost_fact` (`resource_id` where the feed allocates it), shown to
   staff holding `cost.read`. A customer scope cannot read cost columns (ADR 0021), so a
   customer sees no provider cost here and the screen says so. The CMDB link is the
   resource's ServiceNow CI (`servicenow_ci_sys_id`), staff only.
10. **The console.** A new navigation area, **Cloud Services**, with Overview and Findings.
    Overview: per customer and per account, open findings by impact and source, backup
    coverage, policy compliance and cost, the highest-impact open findings, and findings
    opened and resolved against the previous period. An account groups by resource group
    or region; a group lists its resources, most findings first; a resource shows its
    settings, every check, its backup, its cost by day, its CI and its history. The date
    range is in the address. Every list follows the customer selector (`ScopeDep`); an id
    of another tenant is 404.
11. **Ingested text is data.** Resource names, tags, policy and assignment names and
    recommendation text are attacker-influenceable: stored verbatim and bounded (2,000
    characters for free text, 120 for a setting, scalars only), shown as text, never sent
    to a model, never read as instruction. A test files a recommendation whose text is an
    instruction and checks it is a row like any other.
12. **Fixtures are made up.** `azure/fixtures/audit/` uses the shapes Resource Graph and ARM
    returned in dev, with invented resources on the synthetic estate's subscription; a
    test refuses the dev subscription's id in them. Nothing from the lab is in git.

## AWS, by the same design

Not built: the AWS customer credential reads only Cost Explorer totals (ADR 0027,
decision 4). The same table takes AWS kinds without a schema change: AWS Backup
(`ListProtectedResources`, `ListRecoveryPointsByResource`) as `backup`, AWS Config rule
compliance (`DescribeComplianceByResource`) as `policy`, Trusted Advisor
(`DescribeTrustedAdvisorChecks`, needs Business support) and Security Hub
(`GetFindings`) as `advisor` and `defender`. `ReadOnlyAccess` covers the reads. They
need an account with those services enabled to record fixtures from.

## Consequences

- **Volume.** The dev subscription yields 5,611 checks per sync, read live with the
  final queries on 2026-11-01: policy 3,002 (2,581 non-compliant), Advisor 1,150 (453
  high), resource configuration 711 (307 issues, 16 high), Defender 655 (257 unhealthy,
  79 high) and backup 93 (22 failed or stopped, 27 never backed up). 83 of the 88 VM
  protected items name a VM the subscription no longer holds: they stay as checks, not
  linked to inventory. The first sync records an `opened` event
  for every issue it finds; that date is when the platform first saw it, not when the
  provider raised it (`observed_at` keeps the provider's own time where it gives one).
- **Not read: storage soft delete and SQL auditing.** Both are child resources Resource
  Graph does not index. Where the customer's policy or Defender covers them, they appear
  as those checks. Reading them directly is one ARM call per resource: a follow-up.
- Six-hourly Resource Graph queries stay inside its quota (15 requests per 5 seconds per
  user); a large estate pages at 1,000 rows.
- Customer-side change: none. Every call is a read with the roles ADR 0027 checks.

## Not decided here

- Whether customers other than Architect and Security officer should see the area, and
  whether a customer should see its findings' cost impact (Advisor's savings are shown;
  provider cost is not).
- Severity weights for an account score, as ADR 0036 has for posture.
- Ticket or ServiceNow follow-up for a finding: nothing writes outside the platform.
