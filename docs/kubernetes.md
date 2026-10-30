# Kubernetes and GitOps

What runs in the cluster and how it gets there. Decisions: ADR 0008; Temporal's
mutual TLS, ADR 0010; the Gateway API ingress replacing ingress-nginx, ADR 0011
(phase A: both run, Front Door still sends everything to nginx).

```
infra/gitops/
  applications/<env>/   Argo CD Applications (cert-manager, Rollouts, Prometheus,
                        platform, Temporal, workers)
  base/                 Kustomize bases: namespaces, internal-ca, api, migrate,
                        temporal-secrets, monitoring, connector-servicenow, worker-ai
  overlays/<env>/       Per-environment values and image tags (dev is the worked example);
                        overlays/dev/workers is the workers Application's
```

`make check` builds every overlay and checks the pod controls
(`tools/ci/tests/test_gitops.py`).

## First deployment in dev, in order

Plan each stack and check the plan before applying. All are in place: nothing is
replaced or destroyed.

1. **Stack 01** adds the dev-only devbox-to-Key-Vault rule.
2. **Stack 04** turns on password login and `BTREE_GIN`. The break-glass password is
   generated, used once and not kept:
   ```bash
   TF_VAR_postgres_breakglass_password_wo="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" terraform apply -var-file=../../env/dev/common.tfvars -var-file=../../env/dev/04-data.tfvars
   ```
3. **Stack 05** adds the KEDA add-on and workload identity federation.
4. **Stack 04b** grants the workloads read access to the app vault, and you write
   access in dev.
5. **The database and its logins**, from the repository root on the devbox. This
   creates the databases, roles and extensions, and puts the connection strings in
   Key Vault:
   ```bash
   uv run python scripts/db_dev_logins.py
   ```
6. **Mirror the third-party images** into ACR: the pause image that holds Temporal's
   secret mount, and every chart image at the digest its values pin (ADR 0008). For
   example:
   ```bash
   az acr import -n crcloudopsdevweu --source registry.k8s.io/pause:3.10.1 --image pause:3.10.1
   ```
   ```bash
   az acr import -n crcloudopsdevweu --source quay.io/argoproj/argocd@sha256:8e5cc5bb008df8a09dba439462e58c94c55b7735888d3ebde0c25fc05e4e5335 --image argoproj/argocd:v3.5.6
   ```
   Docker Hub and ECR Public sometimes answer ACR with HTTP 429: wait and retry.
7. **Push the migrate image**, set its tag in `overlays/dev`, then commit and push.
8. **Re-run the bootstrap.** It points the root app at `applications/dev`, adds Argo CD's
   Application health check, and turns on ingress-nginx metrics:
   ```bash
   scripts/bootstrap-cluster.sh --repo-url https://github.com/amitcloudarchitect/cloudops.git --env dev
   ```

Argo CD then syncs, in order:
1. cert-manager;
2. Rollouts;
3. Prometheus;
4. the platform: namespaces and policies, the internal CA and Temporal's
   certificates, the migration job (Argo CD waits for it to complete; a failure stops
   the sync), then the API canary;
5. Temporal, with mutual TLS on every connection;
6. the workers: `connector-servicenow`, then its schedule job (5 minutes per entity).

## Rotating dev database passwords

```bash
uv run python scripts/db_dev_logins.py --rotate
```

Then restart the pods that read them: `kubectl -n platform-api rollout restart rollout api`,
and the `temporal-secrets` and Temporal deployments.

## The Gateway API ingress (ADR 0011)

```bash
kubectl -n platform-edge get gateway api
```

```bash
kubectl -n platform-api get httproute api -o jsonpath='{.status.parents[0].conditions}'
```

## Certificates

cert-manager renews every certificate 30 days before it expires, with a new key.
Temporal re-reads its files within the hour. A worker notices within five minutes,
finishes what it is doing, and restarts with the new certificate.

```bash
kubectl get certificates -A
```

To force a renewal (for example after a suspected key leak), with the
[cmctl](https://cert-manager.io/docs/reference/cmctl/) plugin:
```bash
cmctl renew -n platform-ingest connector-servicenow-temporal
```

## Looking at things

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443
```

```bash
kubectl -n temporal port-forward svc/temporal-web 8081:8080
```

```bash
kubectl argo rollouts -n platform-api get rollout api
```
(The last needs the Argo Rollouts kubectl plugin.)
