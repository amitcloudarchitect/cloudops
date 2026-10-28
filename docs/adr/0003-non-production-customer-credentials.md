# ADR 0003: Stored customer credentials, non-production only

- **Status:** Accepted as a time-boxed exception, 2026-10-28
- **Decided by:** Amit Kumar
- **Departs from:** CLAUDE.md rule 2 and LLD, Customer environment access
- **Expires:** before the first production deployment, which this ADR blocks

## Decision

Until production deployment, customer environments may be reached with **stored
credentials**:

- **Azure:** an app registration (client id and secret) per customer.
- **AWS:** an IAM access key pair per customer.

**Production uses the LLD design, with no exceptions.** Azure goes through Lighthouse
delegation, or GDAP through Partner Center since customers are under CSP. AWS goes
through `AssumeRole` with a per-customer external ID. In both cases the broker mints
short-lived credentials and no customer credential is stored.

## Guardrails while the exception lasts

The exception covers *where the credential comes from*, nothing else.

1. **Read-only.** Azure: Reader, Cost Management Reader and Monitoring Reader only,
   scoped to the customer's subscriptions. AWS: `ReadOnlyAccess` and Cost Explorer read
   only. No action permissions: the execution plane stays unbuilt until the security
   review, as CLAUDE.md requires.
2. **Never in code, config, logs or prompts.** Each credential is a Key Vault secret,
   read through the `SecretStore` (CSI-mounted in Azure, `.env.local` on a laptop).
   Names follow one pattern per tenant, for example `customer-<short-code>-azure-secret`.
3. **The secret expires in 90 days or less,** set on the app registration and rotated
   by the same schedule for AWS keys (LLD: connector credentials rotate at 90 days).
4. **The code refuses it in production.** Step 05 adds the credential source as an
   explicit setting (`stored_key` or `delegated`). The configuration layer refuses
   `stored_key` when the deployment environment is `prd`, and a test proves the
   refusal. Production cannot reach this path by accident.
5. **One credential, one tenant.** A stored credential is bound to its tenant at
   onboarding. The broker-style check from the LLD still applies: a target outside the
   tenant's scope is rejected before any cloud call.
6. **Inventory.** Every stored customer credential is listed, with tenant, owner,
   creation date and expiry, so each one can be found and revoked at cut-over.

## Exit

Before the first production deployment:

- move each customer to Lighthouse or GDAP (Azure) or `AssumeRole` with an external ID
  (AWS);
- revoke and delete every stored credential, and confirm against the inventory;
- delete the `stored_key` code path. Until then it stays refused in `prd`.

## Consequences

Until exit, a stolen ingestion identity, or a leaked Key Vault secret, gives read
access to a customer estate. The guardrails limit that to read-only, to one tenant,
and to 90 days. They do not remove it, and that risk is accepted here for
non-production only.
