# Onboarding a customer's cloud, read-only (dev and test only)

ADR 0027, under ADR 0003. A customer's Azure subscriptions are read with an app
registration's client secret, an AWS account with an IAM user's access key: read-only,
one credential per customer, the secret expiring within 90 days. **Production never
does this**: the configuration refuses `stored_key` where the deployment is `prd`, and
production uses Lighthouse, GDAP or `AssumeRole`.

The platform stores **identifiers only**. The secret goes into Key Vault out of band,
under a name the platform gives you. **Never paste a secret into the console, a ticket,
an email or a chat with Claude.** Send ids only.

The first customer is "Azure Sponsorship", short code `AZSPONSOR`, subscription
`cd12743c-f76e-4284-aa62-eb8bd576ac91` (the whole subscription). Its secret is
`customer-azsponsor-azure-secret` in `kv-cloudops-app-dev-weu`.

## 0. Before you start

- **The internal tenant is marked internal** (`docs/platform-cost-feed.md`, step 3.2).
  The dev subscription is also the platform's own; the database lets the internal
  tenant and one customer share it, and refuses the customer's check
  (`account_held_by_another_tenant`) if the platform's copy is not the internal tenant's.
- **Key Vault access.** `kv-cloudops-app-dev-weu` has no public network access: set the
  secret from wherever you set `platform-admin-oid` (ADR 0017), as a member of
  `dev_secret_writer_ids` (Key Vault Secrets Officer).
- **Order.** Create the Key Vault secret **before** this change is synced to dev. The
  dev overlay lists it in the `connector-azure` SecretProviderClass; the CSI driver does
  not mount a missing secret, and the whole pod, the platform's own cost feed included,
  then fails to start (accepted, ADR 0027 decision 3).

## 1. Azure: an app registration with three read roles

Run as someone who may create app registrations in the directory and role assignments
on the subscription (Owner or User Access Administrator there). Nothing here is done
by the platform or by Claude.

```bash
SUB=cd12743c-f76e-4284-aa62-eb8bd576ac91
APP=$(az ad app create --display-name app-cloudops-cust-azsponsor-dev --sign-in-audience AzureADMyOrg --query appId -o tsv)
SP=$(az ad sp create --id "$APP" --query id -o tsv)
for ROLE in "Reader" "Cost Management Reader" "Monitoring Reader"; do
  az role assignment create --assignee-object-id "$SP" --assignee-principal-type ServicePrincipal --role "$ROLE" --scope "/subscriptions/$SUB"
done
```

- **These three roles only**, on this subscription only. The check refuses any other
  role on the subscription (inherited from a group or a management group too), and
  refuses the credential if it can see any subscription besides the onboarded ones.
- Give the app registration **no API permissions** and no directory roles. The check
  cannot see those with read roles, so this is on you.

**The client secret, at most 90 days, straight into Key Vault** (never shown on screen).
Pick an end date at most 90 days from today; 2027-01-28 is 89 days from 2026-10-31:

```bash
az ad app credential reset --id "$APP" --append --display-name cloudops-dev --end-date 2027-01-28 --query password -o tsv | az keyvault secret set --vault-name kv-cloudops-app-dev-weu --name customer-azsponsor-azure-secret --file /dev/stdin --encoding utf-8 --expires 2027-01-28T00:00:00Z -o none
```

Then note, to send or to type into the console: the **directory (tenant) id**
(`az account show --query tenantId -o tsv`), the **application (client) id** (`$APP`),
the **subscription id**, and the **end date**.

## 2. AWS: an IAM user with ReadOnlyAccess and Cost Explorer read

In the customer's account, as an IAM administrator. `<code>` is the customer's short
code in lower case.

```bash
USER=cloudops-read-<code>
aws iam create-user --user-name "$USER"
aws iam attach-user-policy --user-name "$USER" --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
aws iam attach-user-policy --user-name "$USER" --policy-arn arn:aws:iam::aws:policy/AWSBillingReadOnlyAccess
```

- No console password, no inline policy, no other policy (the check refuses them), and
  never the root user's keys. Instead of `AWSBillingReadOnlyAccess` you may attach a
  customer-managed policy allowing only `ce:Get*`, `ce:Describe*` and `ce:List*`.
- Cost Explorer must be enabled in the account (open it once in the console); a member
  account needs its management account to allow member access to Cost Explorer.

**The access key, straight into Key Vault** (the key id is not secret, but it goes in
too: the worker checks it is the onboarded one). Rotate it within 90 days:

```bash
aws iam create-access-key --user-name "$USER" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | { read -r ID KEY; printf '%s' "$ID" | az keyvault secret set --vault-name kv-cloudops-app-dev-weu --name customer-<code>-aws-access-key-id --file /dev/stdin --encoding utf-8 -o none; printf '%s' "$KEY" | az keyvault secret set --vault-name kv-cloudops-app-dev-weu --name customer-<code>-aws-secret-access-key --file /dev/stdin --encoding utf-8 -o none; echo "access key id: $ID"; }
```

Note: the **account id** and the **access key id** (`AKIA…`), and the date you will
rotate it by.

## 3. Let the worker mount it (GitOps)

The connector-azure pod mounts customer secrets by name. `azsponsor`'s is already in
`infra/gitops/overlays/dev/workers/connector-azure.yaml`. For any other customer, add
its names to that file's `objects` list (the PR for it can only be synced once the
secrets exist):

```yaml
        - |
          objectName: customer-<code>-azure-secret
          objectType: secret
```

No Terraform change is needed: the ingest identity already reads the app vault (04b),
and the pod's egress (NAT gateway, `443`) reaches ARM, Entra and AWS.

## 4. Onboard it in the console

**Cloud onboarding** (`/cloud-onboarding`, platform administrators only):

1. Choose the customer, or **A new customer…** (name *Azure Sponsorship*, short code
   `AZSPONSOR`). The short code names the secrets. If the simulation dataset (ADR
   0025) was loaded, *Azure Sponsorship* already exists: choose it.
2. Choose **Azure** and give the directory id, the client id and the subscription ids;
   or **AWS** and the account id and the access key id. **No secret.**
3. Give the date the secret expires (or the key will be rotated by): at most 90 days.
4. **Record and check.** The page names the Key Vault secret(s) to create, if you have
   not yet, and waits.

Within a minute of the secret being mounted, the connector worker checks it:

| Outcome | Meaning | What to do |
| --- | --- | --- |
| Active, `read_only` | Works, sees exactly these accounts, read roles only. The first sync has started. | Nothing. Resources and cost appear after the first runs. |
| `secret_not_in_key_vault` | Not mounted yet. | Create it (step 1 or 2), sync, then **Check again**. |
| `role_not_allowed`, `policy_not_allowed`, `inline_policy` | Something beyond read. The role or policy is named. | Remove it, then **Check again**. |
| `scope_wider_than_tenant` | It can see other subscriptions. | Remove those role assignments. |
| `account_held_by_another_tenant` | Another customer holds the account. | Stop: a wrong mapping. Ask. |
| `credential_rejected`, `*_mismatch` | Wrong secret, key or ids. | Fix the secret or record the right ids. |

The check finds the platform's own subscription shared: the credential shows
**Shared with the platform**, and FinOps marks the account. Its cost appears under
`azsponsor` and under the internal tenant, each reconciled on its own figures; never
add the two (ADR 0023, amended).

## 5. Rotate, revoke, exit

- **Rotate** before the expiry: put the new secret in Key Vault under the same name
  (step 1's or 2's command, with a new end date), wait for the CSI driver to refresh
  the mount, then **Record rotation** with the new date. For AWS, deactivate and delete
  the old key afterwards. An expired credential stops being used on its own.
- **Revoke**: **Revoke** in the console stops it at once. Then delete the Key Vault
  secret and the cloud-side secret or access key, and remove its names from the
  SecretProviderClass. The row stays in the inventory as the record.
- **Exit (ADR 0003)**: before production, revoke every credential in the inventory and
  confirm each against the cloud. Production uses delegated access only.

## Locally

The worker runs from a laptop too, the secret in `.env.local` playing Key Vault:
`customer-azsponsor-azure-secret` is the line `CUSTOMER_AZSPONSOR_AZURE_SECRET=...`.

```bash
CLOUDOPS_ENV=local CLOUDOPS_CUSTOMER_CREDENTIALS=stored_key uv run python -m cloudops_connectors.customer check
```

`check` runs the waiting checks once and prints them, and starts nothing. Run `worker`
instead (it needs Temporal) for a passed check to start the first sync, and to keep
each active credential's schedules.
