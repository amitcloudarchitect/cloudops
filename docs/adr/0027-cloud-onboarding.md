# ADR 0027: Onboarding a customer's cloud with a stored, read-only credential

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Implements:** ADR 0003 (stored customer credentials, non-production only), for Azure
  app registrations and AWS access keys. Migrations `0021_cloud_onboarding` and
  `0022_cloud_account_per_tenant` (contract), `packages/config` (`CUSTOMER_ACCESS`),
  `connectors/src/cloudops_connectors/customer/`, `aws/sigv4.py`, `aws/iam.py`, the
  binding in `core/identity.py` and the Azure and AWS sources,
  `services/api/src/cloudops_api/cloud_onboarding.py`, the console's `/cloud-onboarding`,
  the dev overlay of `infra/gitops/.../connector-azure`, and `docs/cloud-onboarding.md`.
- **Amends:** ADR 0016, decision 5 (a cloud account resolves through the alias map),
  for runs made with a customer's own credential; ADR 0023 (the platform's dev
  subscription is also a customer's; see its amendment).
- **Amended by:** ADR 0031 (2026-11-01): decisions 8 and 9 (the shared subscription
  and its provenance) are withdrawn. A subscription has one owner, and a credential
  runs no cost entity for an account the platform's own feed reads (see the amendment
  at the end).

## Context

The step 15 Azure and AWS connectors ran on recorded fixtures and refused to start
without them, because no customer credential existed (CLAUDE.md rule 2). ADR 0003
allows stored credentials until production under six guardrails. The user wants an
end-to-end simulation with a test customer, "Azure Sponsorship" (`azsponsor`), whose
estate is the platform's own dev subscription `cd12743c-f76e-4284-aa62-eb8bd576ac91`,
onboarded whole. That subscription already feeds the internal tenant's live cost
(ADR 0023) through its alias.

Four things were not specified and were asked (2026-10-31):

| # | Question | Decision (user) |
| --- | --- | --- |
| 1 | `cloud_account` and `tenant_alias` are unique on (provider, id): one subscription, one tenant. How does `cd12743c` also belong to `azsponsor`? | **The internal tenant and at most one customer.** `cloud_account` is unique per tenant; a database rule admits the same id in the internal tenant and one customer, never two customers. A customer credential's runs take their tenant from the credential, never from the alias. |
| 2 | The API has no customer secret and no Temporal client. Where do the check and the first sync run? | **The worker claims a request** (ADR 0015's re-process pattern). The API records it; the worker holding the secret checks, binds and starts the first sync; the console polls. |
| 3 | Where do the customer-credential workers run in AKS? | **In the existing `connector-azure` pod**, as a second container. A missing Key Vault secret then stops the platform cost feed too: accepted. |
| 4 | What does a live AWS account read? | **Cost Explorer totals** (`cost_total`) for the bound account, after the check. Config and the CUR stay on fixtures until a customer names an aggregator or a bucket. |

## Decisions

1. **The credential source is explicit and refused in production** (guardrail 4).
   `CLOUDOPS_CUSTOMER_CREDENTIALS` is `stored_key` or `delegated`, never defaulted.
   `stored_key` is refused where `CLOUDOPS_DEPLOYMENT` is `prd`, and in the Azure
   profile also where the deployment is unnamed. `delegated` (the LLD's broker) is not
   built, so it refuses to start rather than pretend. Tests:
   `packages/config/tests/test_config_settings.py`. Only the dev overlay sets
   `stored_key` and runs the worker that reads it (`tools/ci/tests/test_gitops.py`).

2. **Secrets by name, through the `SecretStore` only** (guardrail 2). Key Vault names:
   `customer-<code>-azure-secret`; for AWS `customer-<code>-aws-access-key-id` and
   `customer-<code>-aws-secret-access-key`, `<code>` the tenant's short code in lower
   case. In Azure they are CSI-mounted files (the connector-azure SecretProviderClass
   lists them; a GitOps test keeps every other workload from mounting one); locally the
   `CUSTOMER_<CODE>_...` lines of `.env.local`. They are read at each use, so a
   rotation needs no restart. No API, table, log, bronze page or prompt holds a secret;
   the onboarding API refuses a body carrying any field it does not know.

3. **The inventory is a table** (guardrail 6): `cloudops.customer_credential`, one row per
   stored credential: tenant, provider, `credential_source` (only `stored_key` is
   admitted), the identifiers (directory and client id; or access key id), the accounts
   it is bound to, the secret names, owner (the onboarding administrator's object id),
   created, when the secret was set, and an expiry **at most 90 days after it was set**
   (guardrail 3, a table constraint and the functions). Rows are never deleted, revoked
   ones included: it is the list to confirm against at exit. FORCE RLS,
   `tenant_isolation`, `staff_only`; the application only reads it.

4. **One credential, one tenant** (guardrail 5). One live credential per tenant and
   provider, and an app registration or access key bound to one tenant (partial unique
   indexes on unrevoked rows). The **target check** is in two places, both before any
   call: the sources only queue work for the credential's own accounts
   (`identity.admit`), and the Azure client is built with `allowed` (the credential's
   subscriptions) and refuses any call naming another (`TargetOutOfScopeError`), as the
   AWS billing source refuses another payer. The subscription listing is the one call
   that names none; what it shows beyond the credential's subscriptions is quarantined
   (CLAUDE.md rule 6) and never read.

5. **The check** (`customer/check.py`), every call a read:
   - **Azure.** A token from the client secret; its claims must name the onboarded
     directory and client. The listing must show every onboarded subscription **and
     nothing else**: a credential that can see more than its tenant's subscriptions is
     refused (`scope_wider_than_tenant`), so it cannot reach another customer. On each
     subscription, every role assignment the principal holds (directly or through a
     group, at, above or below it) must be one of ADR 0003's three built-in roles,
     matched by their fixed definition ids: Reader, Cost Management Reader, Monitoring
     Reader. Anything else is refused and named (`role_not_allowed`); Reader is
     required. Two of the allowed built-ins carry non-read actions
     (`Microsoft.Support/*`, a Log Analytics search): ADR 0003 names those roles, and
     no connector calls either.
   - **AWS.** The key id in Key Vault must be the onboarded one; `GetCallerIdentity` must
     name the onboarded account and an IAM user (never the root user's key). The user's
     and its groups' policies must be `ReadOnlyAccess`, optionally
     `AWSBillingReadOnlyAccess`, or a customer-managed policy allowing only
     `ce:Get*`, `ce:Describe*`, `ce:List*`. Any inline policy, or any other policy, is
     refused. `ReadOnlyAccess` is required. SigV4 is ours (`aws/sigv4.py`), checked
     against AWS's published example; no SDK.
   - Not checkable with read roles, so left to the instructions: Entra directory roles
     and Graph permissions on the app registration; AWS permission boundaries and SCPs.

6. **The flow** (decision 2 above). `POST /v1/cloud-onboarding/credentials`
   (`access.manage`, staff only) records the identifiers and queues a check
   (`cloudops.credential_check`, forward-only, never deleted). The response names the
   secrets to create. The worker (`python -m cloudops_connectors.customer worker`)
   claims the oldest check with no scope, reads the credential under its tenant's
   scope, refuses an expired or revoked one, fails one whose secret is not mounted (a
   person fixes it and checks again), runs the check, and on a pass creates the
   credential's `cloud_account` rows in its tenant; an account another customer holds
   refuses it (`account_held_by_another_tenant`). A passed onboarding or rotation
   creates the credential's schedules and triggers them: the first sync. The tenant is
   created or chosen with the mapping endpoints (ADR 0015), which platform
   administrators already hold.

7. **One Temporal worker per credential, the ordinary workflow.** Each active,
   unexpired credential whose secrets are mounted gets a worker on its own task queue
   (`connectors-<provider>-customer-<code>`) running `ConnectorIngest` over a source
   bound to it (`identity.Binding`): its accounts resolve to its tenant without the
   alias map, its `instance` names the credential (its own watermarks; a replay skips
   any other instance's page), and its daily-cost statements supersede only its own
   tenant's (`AzureDaily._statement`). Schedules are `customer-<provider>-<code>-<entity>`;
   a revoked, expired or unmounted credential loses its worker and schedules on the
   next pass. Azure: `resource` (15 min), `cost_daily` and `cost_total` (daily). AWS:
   `cost_total` (daily). The check runs in the worker's loop, not as a workflow: one
   bounded step whose state machine is the forward-only table.

8. **The shared subscription** (decision 1 above; ADR 0023 amended). `cloud_account`
   is unique on (tenant, provider, id) (0025); the old global constraint is dropped in
   0026, a contract shipped with its expand as 0015 was (an old connector pod's upsert
   fails and is retried until replaced). A SECURITY DEFINER trigger admits a second
   tenant on the same id only when one of the two is the internal tenant, refuses a
   second customer (`CAOWN`, naming no tenant), marks both rows
   `shared_with_platform`, and stops the internal tenant giving up being internal
   while it shares. `identity.known_accounts` picks each id's row by its tenant, and
   raises rather than guess when two tenants' rows are in scope.

9. **Provenance.** The credential view lists the shared accounts; FinOps' per-account
   reconciliation marks them and says the cost is in both tenants, each reconciled on
   its own figures; the staff cost summary carries `shared_with_platform`, the part of
   each line in such accounts, because a total across tenants counts it twice.
   Reconciliation is per account and so per tenant; a test runs the platform's and
   the customer's feeds of one subscription and checks each tenant's ledger is its own
   rows only (`db/tests/test_connector_customer.py`).

## Consequences

- **Nothing here acts on a customer environment.** Every call is a read; the roles are
  checked to be read-only before the first sync. No action role, no execution plane, no
  ServiceNow write-back: those wait for the security review.
- **Every workload identity can read every secret in the app vault** (04b grants
  Key Vault Secrets User at vault scope to api, ingest, ai and workflow). Only the
  connector-azure pod mounts a customer secret, but a compromised API or AI pod could
  read one from Key Vault directly. Accepted for dev under ADR 0003's consequences;
  before a second customer, move customer secrets to their own vault, or give those
  identities per-secret grants (a 04b change, planned and applied by the user).
- A missing customer secret stops the whole `connector-azure` pod, the platform's own
  cost feed included (decision 3). Create the secret before syncing the overlay that
  lists it (`docs/cloud-onboarding.md`).
- Connector health shows a customer credential's runs under the same source and entity
  as the platform's (`azure` / `cost_daily`): the run log has no instance column.
- Customer bronze pages land in the connector's bronze container (`azure`), AWS ones too,
  under their source's prefix, marked with the credential's instance.
- The fixtures for the check are in the documented shapes with synthetic ids, not
  recorded from a customer: replace them after the first real onboarding.
- **Exit (ADR 0003):** before production, revoke every row in the inventory, delete the
  Key Vault and cloud-side secrets, move each customer to Lighthouse, GDAP or
  `AssumeRole`, and delete `cloudops_connectors.customer` and the `stored_key` path.

## Not decided here

- AWS Config and the CUR from a customer's own account (an aggregator name, a bucket).
- Azure Monitor metrics with the customer's Monitoring Reader: the Azure telemetry
  store is still LLD L1.
- Removing a subscription from a credential, or adding one: today, revoke and onboard
  again (a new check).

## Amendment (ADR 0031): no shared subscriptions

The user decided on 2026-11-01 that a tenant, internal or a customer, owns its
subscriptions alone, and that many tenants may be internal.

- **Decision 1 and decision 8 are withdrawn.** `cloud_account` is unique on
  `(provider, native_id)` again (migration 0030). The sharing trigger, its tenant
  trigger and their policies are gone. A credential bound to an account another tenant
  holds, internal or not, is refused at the check (`account_held_by_another_tenant`).
- **Decision 9 is withdrawn.** No `shared_with_platform` in the API, the console or
  FinOps. The column stays, always false, until a later contract drops it.
- **Decision 7, one change.** A credential runs no cost entity (`cost_daily`,
  `cost_total`) for an account whose cost ADR 0023's feed reads, that is, an account
  with an alias of the provider in the credential's own tenant. Its resources are still
  read with the credential.
- **Azure Sponsorship** is an internal tenant that hosts the platform and is
  `cd12743c`'s single owner. Migration 0029 folded the dev pair into it.
- **Still open from the consequences.** Every workload identity can read every secret
  in the app vault. The user chose (2026-11-01) a separate customer vault: the API
  writes the secret typed into the console, and a dedicated identity reads it by name
  at runtime. That change has its own ADR.
