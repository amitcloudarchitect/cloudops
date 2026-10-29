# ADR 0008: Kubernetes deployment, GitOps, and dev access to data and Temporal

- **Status:** Accepted, 2026-10-29
- **Decided by:** Amit Kumar (step 12)
- **Implements:** LLD, Compute (pod controls, placement), Delivery (Argo CD pulls,
  canary, migrations before rollout). Code in `infra/gitops`, `infra/cluster`,
  `scripts/db_dev_logins.py`, `db/migrations/env.py`, stacks 01, 04, 04b and 05.

## Decisions

1. **App of apps, per environment.** The root Application reads
   `infra/gitops/applications/<env>`. Its children, in sync-wave order:
   - Argo Rollouts (-2);
   - Prometheus (-1);
   - the platform Kustomize overlay (0), whose PreSync hook runs the migrations;
   - Temporal (1).

   Argo CD assesses child Application health, so each wave waits for the last. The
   pipeline (step 13) changes image tags in `overlays/<env>` and nothing else;
   nothing applies to the cluster from outside.
2. **Every workload we own runs restricted.**
   - Pod and container settings: non-root, read-only root filesystem, all
     capabilities dropped, `RuntimeDefault` seccomp, no service account token.
   - Requests, plus a memory limit always; no CPU limit on the API.
   - Required node affinity to its LLD pool; a PDB where it can have more than one
     replica.
   - Its own workload identity, with Key Vault secrets as CSI-mounted files and
     never environment variables.
   - Every namespace is default-deny in and out, with named allows.
   - `tools/ci/tests/test_gitops.py` checks each of these on the built manifests.
3. **Dev database logins are passwords** (decided by Amit Kumar, over Entra token
   authentication).
   - The dev server enables password authentication alongside Entra. That needs an
     administrator login, `breakglass_dev`, whose password is write-only, generated
     at apply time and not kept.
   - `scripts/db_dev_logins.py`, run by the Entra administrator, does three things:
     - creates `cloudops_app_azure`, `cloudops_ingest_azure` and `temporal`, never
       able to bypass RLS;
     - sends each password only as a SCRAM verifier;
     - stores the connection strings in Key Vault, which needs one dev-only firewall
       rule, devbox to the vault subnet.
   - Migrations still run as the Entra identity that owns the schema
     (`id-cloudops-migrate-dev`), with a workload identity token.
   - UAT and production use Entra tokens for every login. That needs
     `packages/tenancy` to fetch a token per connection: open, and required before
     UAT.
4. **Temporal is self-hosted in AKS** (decided by Amit Kumar).
   - It uses the official chart on `npai`, persisting to `temporal` and
     `temporal_visibility` on the platform's PostgreSQL server, over verified TLS.
   - Its password reaches the chart as a Kubernetes Secret synced from Key Vault by
     the CSI driver. This is the one Secret object in the platform, because the chart
     reads its SQL password from nothing else.
   - Its pods run under the namespace's baseline Pod Security, reported against
     restricted.
   - Inside the cluster the frontend is plaintext gRPC, reachable only from the
     worker namespaces and KEDA (NetworkPolicy). **Open:** mTLS between workers and
     the frontend, and Temporal authorization, before production.
5. **Canary analysis uses an in-cluster Prometheus** (decided by Amit Kumar).
   - Prometheus scrapes ingress-nginx's request metrics for the canary Service only.
   - A rollout fails on a 5xx rate of 2% or a p95 above 500 ms. With no traffic
     there is nothing to measure, so the rollout passes.
   - It is not the observability stack (step 19).
6. **Workload identity federation lives in stack 05**, next to the OIDC issuer it
   trusts, so a replaced cluster re-federates on apply. A test keeps the federated
   service accounts equal to the ones the manifests declare.
7. **Workers are written but not deployed in dev.**
   - `connector-servicenow` needs the ServiceNow read account.
   - `worker-ai` needs the Azure OpenAI tiers (LLD L12).
   - Their bases (Deployment, KEDA Temporal scaler, PDB, identity, policy) build,
     validate and pass the same tests.
8. **The repository is public** (decided by Amit Kumar), so Argo CD needs no
   repository credential. It holds no secret; it does name the dev subscription and
   tenant.

## Open items

- **Third-party images** (Temporal, Prometheus, Argo Rollouts, ingress-nginx, Argo CD)
  still pull from public registries, against the LLD's "images only from the platform
  registry". Mirror them into ACR with `az acr import` and pin digests before UAT.
  Our own images, and the pause image, already come from ACR.
- Entra-token database logins (decision 3), and Temporal mTLS and authorization
  (decision 4).
- Signed-image verification on the execution namespace (LLD) comes with the
  execution plane, after the security review.
