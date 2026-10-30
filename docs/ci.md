# CI/CD setup

The pipelines are in `.github/workflows`; the decisions are ADR 0013. This page is
what a person sets up once, in Azure and in GitHub, in this order. Nothing here is a
secret except the release bot's private key.

## 1. The CI identity (Terraform, you apply)

```bash
cd infra/terraform/stacks/02-security && terraform apply -var-file=../../env/dev/common.tfvars
```

That creates `id-cloudops-cicd-dev` and its two GitHub federations (`main`, pull
requests). Its client ID:

```bash
terraform output -raw cicd_client_id
```

Then grant it push and read (04b looks the identity up, so 02 comes first):

```bash
cd ../04b-rbac && terraform apply -var-file=../../env/dev/common.tfvars -var-file=../../env/dev/04b-rbac.tfvars
```

## 2. The release bot (a GitHub App)

GitHub, your profile → Settings → Developer settings → GitHub Apps → New GitHub App:

- Name: `cloudops-release-bot` (any unique name); Homepage URL: the repository.
- Webhook: **off**.
- Repository permissions: **Contents: Read and write**, **Pull requests: Read and
  write**, Metadata: Read-only. Nothing else.
- Where can it be installed: **Only on this account**.

Create it, then on its page: note the **Client ID**, and **Generate a private key**
(a `.pem` downloads). Install the App on `amitcloudarchitect/cloudops` only.

## 3. Repository variables and the one secret

Repository → Settings → Secrets and variables → Actions.

**Variables** (identifiers, not secrets):

| Name | Value |
|---|---|
| `AZURE_CLIENT_ID` | `cicd_client_id` from step 1 |
| `AZURE_TENANT_ID` | `2fa5e26d-5ad2-415f-88a2-771caca87cbe` |
| `AZURE_SUBSCRIPTION_ID` | the dev subscription |
| `ACR_LOGIN_SERVER` | `crcloudopsdevweu.azurecr.io` |
| `RELEASE_APP_CLIENT_ID` | the App's Client ID |
| `TF_DEV_BACKEND_HCL` | the contents of `infra/terraform/env/dev/backend.hcl` |
| `TF_DEV_COMMON_TFVARS` | the contents of `env/dev/common.tfvars` |
| `TF_DEV_04_DATA_TFVARS` | the contents of `env/dev/04-data.tfvars` |
| `TF_DEV_04B_RBAC_TFVARS` | the contents of `env/dev/04b-rbac.tfvars` |
| `TF_DEV_05_AKS_TFVARS` | the contents of `env/dev/05-aks.tfvars` |

The tfvars hold IDs and sizes, nothing secret; they are gitignored only so each
environment keeps its own. Until `TF_DEV_COMMON_TFVARS` is set, the plan job is
skipped. `AZURE_*` and `ACR_LOGIN_SERVER` fall back to the dev values when unset:
they are identifiers, and Entra trusts the CI identity only from this repository.

Set these on the **repository** (Settings → Secrets and variables → Actions), not on
an environment: a job sees an environment's variables only when it deploys to that
environment, and the release job deploys to none.

**Secret:** `RELEASE_APP_PRIVATE_KEY`, the whole `.pem`. Delete the downloaded file
afterwards. Rotate it by generating a new key on the App and deleting the old one.

## 4. Repository settings

Settings → General → Pull Requests: **Allow auto-merge** on; **Automatically delete
head branches** on.

Settings → Actions → General → Workflow permissions: **Read repository contents**
(the default token stays read-only; jobs ask for more themselves).

## 5. Protect main (a ruleset)

Settings → Rules → Rulesets → New branch ruleset, target `main`, **Active**:

- **Restrict deletions**, **Block force pushes**, **Require linear history**.
- **Require a pull request before merging**: 1 approval; **Dismiss stale approvals**;
  **Require review from Code Owners**; **Require approval of the most recent push**.
- **Require status checks to pass**, with "Require branches to be up to date". Add
  each check after it has run once on a pull request:
  `data access, rate card`, `secret-scan`, `lint-and-types`,
  `unit, contract, integration, isolation`, `AI evaluation (release gate)`,
  `pricing-regression`, `SAST (CodeQL, high and critical fail)`,
  `SCA (dependencies, high and critical fail)`, `container build, scan, SBOM`,
  `terraform fmt, validate, policy tests`, `console (contracts, types, build, journeys)`,
  and each `terraform plan (…)`.
- **Bypass list**:
  - the release bot App, **Always** (its tag bumps merge after their checks without a
    human review);
  - Repository admin, **For pull requests only**, while you are the only person: GitHub
    never lets an author approve their own pull request. Remove it when a second
    reviewer joins.

From then on changes reach `main` by pull request; `git push origin HEAD:main` is
refused.

## 6. Environments

Settings → Environments:

- `uat`: Required reviewers (you, for now); Deployment branches: `main` only.
- `prd`: Required reviewers; **Prevent self-review**; Deployment branches: `main` only.

Promotion (`promote.yml`, Actions → promote → Run workflow) waits on these.

## What runs when

| Event | Workflow | Does |
|---|---|---|
| Pull request | `ci` (+ `runbooks` if runbooks change) | every gate |
| Merge to `main` | `main` | gates, then build, scan, push, sign, and the bot's dev bump |
| Bot's bump merges | none (paths ignored) | Argo CD rolls dev |
| By hand, from `main` | `promote` | verify signatures, environment approval, overlay PR |
