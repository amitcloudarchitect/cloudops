# Customer portal: federation and go-live (manual steps)

Step 18, ADR 0018. These are the Entra and Azure steps that no code performs. **They
are yours to do by hand.** Nothing in the repository applies them, and Claude Code
runs neither `terraform apply` nor role assignments.

The platform's side is in the console: **Portal onboarding**, which needs a platform
administrator. Do the Entra steps below first, then record each one there.

Dev values: directory `2fa5e26d-5ad2-415f-88a2-771caca87cbe`, console registration
`app-cloudops-web-dev` (`019f8f28-8c02-4eb7-83ef-f8c80a870ff8`), API registration
`app-cloudops-api-dev`.

## Once per environment

### 1. Create the database role (before migration 0011)

`cloudops_customer` is a server-level role. The Entra administrator of the Flexible
Server re-runs the bootstrap, as in the README's "Azure database bootstrap":

```bash
psql "<Entra admin connection to the postgres database>" -v migrator=<migration role> -f db/bootstrap/roles.sql
```

It is idempotent. It creates `cloudops_customer` (NOLOGIN, NOSUPERUSER, NOBYPASSRLS)
and makes `cloudops_app` a member `WITH INHERIT FALSE, SET TRUE`. Migration 0011 stops
with a clear message if this has not been done.

Customer principals already assigned in dev stop resolving after 0011 until their
tenant is onboarded below. This is deliberate: the gate fails closed.

### 2. Apply the portal hostname (stack 06-edge)

Review the plan, then apply it yourself:

```bash
cd infra/terraform/stacks/06-edge && terraform init && terraform plan
```

The expected plan is **8 to add, 0 to change, 0 to destroy**:

- the portal endpoint, its origin group, origin and two routes;
- its WAF policy and security policy;
- `route-console-static` from ADR 0017, if it has not been applied yet.

Afterwards:

```bash
terraform output portal_hostname
```

### 3. Let the console sign in on the portal host

On **App registrations → app-cloudops-web-dev → Authentication**, under the
**Single-page application** platform:

- add redirect URI `https://<portal_hostname>`, with no path and no trailing slash;
- the same value is the post-logout redirect.

Then add `https://<portal_hostname>` to `CLOUDOPS_WEB_ORIGINS` in
`infra/gitops/overlays/dev/config.yaml`. The web ConfigMap's
`CLOUDOPS_WEB_API_BASE` names one origin. While it is the staff host, a page served
from the portal host calls the API cross-origin, so the API must list the portal
origin. The CSP's `connect-src` already names the API origin.

### 4. Let customers in as guests, approved by their own administrator

Getronics does not administer customer user lists (HLD M2). Use Entra **entitlement
management** so the customer's administrator approves its own people:

1. **Identity Governance → Entitlement management → Connected organizations →
   Add.** Add the customer by its Entra tenant (or by domain for a non-Entra
   provider). Add the customer's portal administrator as an **external sponsor**.
2. **Access packages → New**, in a catalog such as `cloudops-portal`:
   - Resource: the enterprise application **app-cloudops-web-dev**, role *Default
     Access*. It requires assignment (ADR 0017), so only package holders can sign in.
   - Policy: *For users not in your directory*, scoped to that connected organization.
   - Approval: required, first approver **external sponsor**. That is the customer's
     own administrator.
   - Lifecycle: an expiry or an access review the customer sponsor performs (90 days
     is reasonable). Remove the guest when access ends.
3. Send the access package's **My Access portal link** to the customer's
   administrator. Their users request access, the administrator approves, and Entra
   invites the guest.

The Entra package decides who may sign in. The platform decides what they see: until
the customer's portal administrator adds a user in **Users**, that user signs in to
"Your account has no access yet", which shows their object id to send to the
administrator.

### 5. MFA and the home identity provider

- **Customer on Entra ID:** under **External Identities → Cross-tenant access
  settings → Add organization**, trust the customer's **MFA** (and compliant-device
  claims if contracted) inbound, so users are not asked twice. Keep the Conditional
  Access policy that requires MFA for `app-cloudops-web-dev` applied to guests.
- **Customer on another SAML 2.0 or WS-Fed provider:** under **External Identities →
  All identity providers → SAML/WS-Fed → New**, enter the customer's domain, issuer
  URI and metadata URL. The customer adds Getronics' directory as a relying party
  (`https://login.microsoftonline.com/<directory id>/`, the issuer the wizard shows).
- In both cases, do **not** add customer users to any Getronics group. Their Entra
  role is the same read-only default for everyone (ADR 0002, D6), and their platform
  role is data in the platform.

## Per customer: onboarding in the console

As a platform administrator, open **Portal onboarding**, choose the customer and:

1. **Federation.** Choose *Entra* and give the customer's Entra tenant id, or choose
   *SAML* or *WS-Fed* and give the issuer URI. List the verified domains. These are
   identifiers, never a secret.
2. **Role set.** Tick the roles agreed with the customer (CXO, VP or director,
   Architect, Customer engineer, Security officer, Approver, or fewer) and say where
   the agreement is recorded (SOW schedule or ticket).
3. **First customer administrator.** Give the guest's **object id in the Getronics
   directory** (Entra → Users → the guest → Object ID), a name and a role from the
   agreed set. This is the same person as the access package's external sponsor.
4. **Go live.** Their users can now sign in. The customer's administrator adds the
   rest in **Users**. *Suspend portal access* stops every user of that customer on
   their next request and keeps their assignments.

## Acceptance checklist

- [ ] `roles.sql` re-run on the server; migration 0011 applied.
- [ ] Stack 06-edge applied; `portal_hostname` loads the sign-in page and answers
      `/healthz`.
- [ ] Redirect URI added; signing in on the portal host returns to it.
- [ ] A test guest from a connected organization requests the access package and the
      external sponsor approves it.
- [ ] Before onboarding, that guest sees "no access yet" with its object id.
- [ ] After go-live and being added in **Users**, it lands on its role's view and sees
      only its own organisation.
- [ ] Sixty rapid requests to `/v1/me` from one address are fine. Several hundred in a
      minute get `403` from the WAF, which is the rate limit.
